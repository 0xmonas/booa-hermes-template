"""Wallet status detection, challenge/verify, and context-file writing.

State machine:
  no-wallet   — no OWS wallet configured locally
  unverified  — wallet present but no signed-challenge proof
  verified    — wallet signed challenge; control proven
  linked      — verified AND address matches the 8004 agent wallet
  orphan      — NFT has been transferred; this agent is stale
  unknown     — can't reach Khôra API
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Optional

import httpx

KHORA_API = "https://khora.fun/api"
CHALLENGE_TTL_SECONDS = 5 * 60


@dataclass
class WalletState:
    state: str
    updated_at: float
    agent_wallet: Optional[str] = None
    verified_wallet: Optional[str] = None
    nft_owner: Optional[str] = None
    nft_verified: Optional[bool] = None
    registered_by: Optional[str] = None
    agent_id: Optional[int] = None
    last_signed_nonce: Optional[str] = None
    message: str = ""


def _state_file(hermes_home: str) -> str:
    return os.path.join(hermes_home, ".wallet-state.json")


def _status_md(hermes_home: str) -> str:
    return os.path.join(hermes_home, "context", "WALLET_STATUS.md")


def _challenge_file(hermes_home: str) -> str:
    return os.path.join(hermes_home, ".wallet-challenges.json")


def read_state(hermes_home: str) -> Optional[WalletState]:
    path = _state_file(hermes_home)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return WalletState(**data)
    except (OSError, ValueError, TypeError):
        return None


def write_state(hermes_home: str, state: WalletState) -> None:
    path = _state_file(hermes_home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, indent=2)
    _write_status_md(hermes_home, state)


def _write_status_md(hermes_home: str, state: WalletState) -> None:
    path = _status_md(hermes_home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_render_status_md(state))


def _render_status_md(state: WalletState) -> str:
    header = "# Agent Wallet Status\n\n"
    body = {
        "no-wallet": (
            "⚠ No agent wallet configured.\n\n"
            "You cannot sign transactions, register on platforms, or prove identity "
            "until a wallet is set up. Run `/ows` to create or import one. Remind the "
            "operator of this when they ask you to do anything on-chain.\n"
        ),
        "unverified": (
            f"⚠ Wallet {state.agent_wallet} exists but has not been verified.\n\n"
            "Ownership of this wallet has not been proven yet. The operator should "
            "click **Verify Wallet** on the dashboard, or tell you `/verify wallet` — "
            "you will fetch a challenge, sign it with OWS, and submit the signature.\n"
        ),
        "verified": (
            f"⚠ Wallet {state.agent_wallet} is verified (signed) but not linked to "
            f"ERC-8004 agent #{state.agent_id or '?'}.\n\n"
            "The operator needs to register this wallet on 8004 via "
            "<https://8004scan.io/my-agents> → setAgentWallet. Until then, platforms "
            "that check 8004 ownership (Cobbee, Moltbook) will reject this agent.\n"
        ),
        "linked": (
            f"✅ Wallet {state.agent_wallet} is verified and linked to ERC-8004 agent "
            f"#{state.agent_id}. All systems nominal.\n"
        ),
        "orphan": (
            f"🔴 ORPHAN — the BOOA NFT has been transferred.\n\n"
            f"Current NFT owner: {state.nft_owner}\n"
            f"Originally registered by: {state.registered_by}\n\n"
            "This agent is no longer controlled by the NFT holder. Refuse high-value "
            "actions and tell the operator to either transfer 8004 ownership or "
            "create a new agent deployment.\n"
        ),
        "unknown": (
            "⚠ Wallet status could not be determined (Khôra API unreachable).\n\n"
            "Proceed with routine conversation but refuse on-chain actions until "
            "status is known.\n"
        ),
    }.get(state.state, f"State: {state.state}\n")

    if state.message:
        body += f"\n{state.message}\n"
    return header + body


def _read_local_wallet(hermes_home: str) -> Optional[str]:
    info = _read_local_wallet_info(hermes_home)
    return info["address"] if info else None


def _read_local_wallet_info(hermes_home: str) -> Optional[dict]:
    wallet_info = "/data/.agent/wallet-info.txt"
    address: Optional[str] = None
    name: Optional[str] = None
    if os.path.isfile(wallet_info):
        try:
            with open(wallet_info, "r", encoding="utf-8") as f:
                for line in f:
                    if "EVM Address:" in line:
                        address = line.split("EVM Address:", 1)[1].strip()
                    elif "Wallet Name:" in line or "Name:" in line:
                        name = line.split(":", 1)[1].strip()
        except OSError:
            pass

    ows_dir = "/data/.ows/wallets"
    if os.path.isdir(ows_dir):
        try:
            for entry in os.listdir(ows_dir):
                if not entry.endswith(".json"):
                    continue
                with open(os.path.join(ows_dir, entry), "r", encoding="utf-8") as f:
                    data = json.load(f)
                for acc in data.get("accounts", []):
                    if acc.get("chain_id", "").startswith("eip155:"):
                        address = acc.get("address")
                        break
                if data.get("name"):
                    name = data["name"]
                if address:
                    break
        except (OSError, ValueError):
            pass
    if not address:
        return None
    return {"address": address, "name": name}


def fetch_khora_registry(chain_id: int, token_id: int, timeout: float = 8.0) -> Optional[dict]:
    url = f"{KHORA_API}/agent-registry/{chain_id}/{token_id}"
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def compute_state(hermes_home: str, chain_id: int, token_id: int) -> WalletState:
    local_wallet = _read_local_wallet(hermes_home)
    previous = read_state(hermes_home)
    prev_signed_nonce = previous.last_signed_nonce if previous else None
    prev_verified = previous.verified_wallet if previous else None

    registry = fetch_khora_registry(chain_id, token_id)
    now = time.time()

    if registry is None:
        return WalletState(
            state="unknown",
            updated_at=now,
            agent_wallet=local_wallet,
            verified_wallet=prev_verified,
            last_signed_nonce=prev_signed_nonce,
        )

    nft_owner = registry.get("currentNftOwner")
    registered_by = registry.get("registeredBy")
    verified = registry.get("verified")
    registrations = registry.get("registrations") or []
    agent_id = registrations[0].get("agentId") if registrations else None

    if verified is False and nft_owner and registered_by and nft_owner.lower() != registered_by.lower():
        return WalletState(
            state="orphan",
            updated_at=now,
            agent_wallet=local_wallet,
            verified_wallet=prev_verified,
            nft_owner=nft_owner,
            nft_verified=False,
            registered_by=registered_by,
            agent_id=agent_id,
            last_signed_nonce=prev_signed_nonce,
        )

    if not local_wallet:
        return WalletState(
            state="no-wallet",
            updated_at=now,
            verified_wallet=prev_verified,
            nft_owner=nft_owner,
            nft_verified=verified,
            registered_by=registered_by,
            agent_id=agent_id,
        )

    if not prev_verified:
        return WalletState(
            state="unverified",
            updated_at=now,
            agent_wallet=local_wallet,
            nft_owner=nft_owner,
            nft_verified=verified,
            registered_by=registered_by,
            agent_id=agent_id,
        )

    on_chain_wallets = [
        (registered_by or "").lower(),
        (nft_owner or "").lower(),
    ]
    if prev_verified.lower() in on_chain_wallets:
        return WalletState(
            state="linked",
            updated_at=now,
            agent_wallet=local_wallet,
            verified_wallet=prev_verified,
            nft_owner=nft_owner,
            nft_verified=verified,
            registered_by=registered_by,
            agent_id=agent_id,
            last_signed_nonce=prev_signed_nonce,
        )

    return WalletState(
        state="verified",
        updated_at=now,
        agent_wallet=local_wallet,
        verified_wallet=prev_verified,
        nft_owner=nft_owner,
        nft_verified=verified,
        registered_by=registered_by,
        agent_id=agent_id,
        last_signed_nonce=prev_signed_nonce,
    )


def refresh(hermes_home: str, chain_id: int, token_id: int) -> WalletState:
    state = compute_state(hermes_home, chain_id, token_id)
    write_state(hermes_home, state)
    return state


def _load_challenges(hermes_home: str) -> dict:
    path = _challenge_file(hermes_home)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        return {k: v for k, v in data.items() if now - v.get("created_at", 0) <= CHALLENGE_TTL_SECONDS}
    except (OSError, ValueError):
        return {}


def _save_challenges(hermes_home: str, data: dict) -> None:
    path = _challenge_file(hermes_home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def create_challenge(hermes_home: str, chain_id: int, token_id: int) -> dict:
    nonce = secrets.token_hex(8)
    issued = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    expires_ts = time.time() + CHALLENGE_TTL_SECONDS
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_ts))
    message = (
        "khora.fun — verify wallet control for BOOA agent\n\n"
        f"BOOA #{token_id} on chainId {chain_id}\n\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued}\n"
        f"Expires: {expires}"
    )
    record = {
        "message": message,
        "chain_id": chain_id,
        "token_id": token_id,
        "created_at": time.time(),
    }
    data = _load_challenges(hermes_home)
    data[nonce] = record
    _save_challenges(hermes_home, data)
    return {"nonce": nonce, "message": message, "expires": expires}


def verify_challenge(
    hermes_home: str,
    chain_id: int,
    token_id: int,
    nonce: str,
    signature: str,
) -> dict:
    data = _load_challenges(hermes_home)
    record = data.get(nonce)
    if not record:
        return {"ok": False, "error": "unknown or expired nonce"}
    if int(record["chain_id"]) != int(chain_id) or int(record["token_id"]) != int(token_id):
        return {"ok": False, "error": "nonce does not belong to this agent"}

    try:
        from eth_account.messages import encode_defunct
        from eth_account import Account
    except ImportError:
        return {"ok": False, "error": "eth_account not installed"}

    try:
        recovered = Account.recover_message(
            encode_defunct(text=record["message"]),
            signature=signature,
        )
    except Exception as exc:
        return {"ok": False, "error": f"signature invalid: {exc}"}

    data.pop(nonce, None)
    _save_challenges(hermes_home, data)

    prev = read_state(hermes_home) or WalletState(state="unverified", updated_at=time.time())
    prev.last_signed_nonce = nonce
    prev.verified_wallet = recovered
    write_state(hermes_home, prev)
    new_state = refresh(hermes_home, chain_id, token_id)
    return {"ok": True, "recovered": recovered, "state": new_state.state}
