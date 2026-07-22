"""Build the setAgentWallet link payload the holder pastes into the BOOA Bridge.

Awakening binds a BOOA to an onchain ERC-8004 agent via Adapter8004. To give that
agent an operating wallet, the agent's OWN wallet (OWS) must consent through an
EIP-712 signature the identity registry recovers — nobody can point an agent at a
wallet they don't control. We build that typed data, sign it with OWS, and return
a compact base64 blob the Bridge decodes and submits via adapter.setAgentWallet.

The EIP-712 scheme mirrors src/lib/contracts/agent-wallet.ts in the booa.app repo;
the digest must match byte-for-byte or the registry rejects the signature.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from typing import Optional
from urllib.parse import quote

BOOA_APP = os.environ.get("BOOA_APP_URL", "https://booa.app").rstrip("/")
BOOA_API = f"{BOOA_APP}/api"
# ERC-8004 Identity Registry — deterministic CREATE2, same address on every chain.
REGISTRY_ADDRESS = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
DOMAIN_NAME = "ERC8004IdentityRegistry"
DOMAIN_VERSION = "1"
DEFAULT_DEADLINE_SECONDS = 3600  # generous copy-paste window


def _fetch_registry(chain_id: int, token_id: int, timeout: float = 8.0) -> Optional[dict]:
    import httpx
    try:
        r = httpx.get(
            f"{BOOA_API}/agent-registry/{chain_id}/{token_id}",
            timeout=timeout, follow_redirects=True,
        )
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def build_typed_data(chain_id: int, agent_id: int, new_wallet: str, owner: str, deadline: int) -> dict:
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "AgentWalletSet": [
                {"name": "agentId", "type": "uint256"},
                {"name": "newWallet", "type": "address"},
                {"name": "owner", "type": "address"},
                {"name": "deadline", "type": "uint256"},
            ],
        },
        "primaryType": "AgentWalletSet",
        "domain": {
            "name": DOMAIN_NAME,
            "version": DOMAIN_VERSION,
            "chainId": chain_id,
            "verifyingContract": REGISTRY_ADDRESS,
        },
        "message": {
            "agentId": int(agent_id),
            "newWallet": new_wallet,
            "owner": owner,
            "deadline": int(deadline),
        },
    }


def typed_data_digest(typed: dict) -> str:
    """EIP-712 digest: keccak(0x1901 || domainSeparator || hashStruct). For tests."""
    from eth_account.messages import encode_typed_data
    from eth_utils import keccak

    signable = encode_typed_data(full_message=typed)
    return "0x" + keccak(b"\x19" + signable.version + signable.header + signable.body).hex()


def encode_blob(chain_id: int, agent_id: int, wallet: str, deadline: int, signature: str) -> str:
    payload = {
        "v": 1,
        "chainId": int(chain_id),
        "agentId": str(agent_id),
        "wallet": wallet,
        "deadline": str(deadline),
        "signature": signature,
    }
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def _ows_sign_typed_data(wallet_name: str, typed: dict) -> Optional[str]:
    """Sign EIP-712 typed data with the agent's OWS wallet. Returns a 0x signature."""
    try:
        proc = subprocess.run(
            ["ows", "sign", "message", "--wallet", wallet_name, "--chain", "evm",
             "--message", "", "--typed-data", json.dumps(typed), "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        out = json.loads(proc.stdout)
        sig = out.get("signature")
        return sig if isinstance(sig, str) and sig.startswith("0x") else None
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        return None


def build_link_blob(
    chain_id: int,
    token_id: int,
    wallet_name: str,
    wallet_address: str,
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
) -> dict:
    """Resolve the agent + adapter, sign the consent with OWS, return the paste blob."""
    reg = _fetch_registry(chain_id, token_id)
    if not reg:
        return {"ok": False, "error": "Could not read the agent registry."}
    if not reg.get("bound"):
        return {"ok": False, "error": "This BOOA is not awakened yet. Awaken it first at booa.app/studio/awaken."}

    regs = reg.get("registrations") or []
    agent_id = regs[0].get("agentId") if regs else None
    owner = reg.get("bindingContract")  # adapter = ownerOf(agentId) for bound agents
    if agent_id is None or not owner:
        return {"ok": False, "error": "Missing agentId or adapter for this agent."}

    deadline = int(time.time()) + int(deadline_seconds)
    typed = build_typed_data(chain_id, int(agent_id), wallet_address, owner, deadline)
    signature = _ows_sign_typed_data(wallet_name, typed)
    if not signature:
        return {"ok": False, "error": "OWS could not sign. Is a wallet configured (ows wallet list)?"}

    blob = encode_blob(chain_id, int(agent_id), wallet_address, deadline, signature)
    return {
        "ok": True,
        "blob": blob,
        "url": f"{BOOA_APP}/bridge?link={quote(blob, safe='')}",
        "agentId": int(agent_id),
        "deadline": deadline,
    }


def _resolve_from_config() -> Optional[tuple[int, int, str, str]]:
    """Read (chain_id, token_id, wallet_name, wallet_address) from the agent's saved setup."""
    from . import wallet_status

    home = os.environ.get("HERMES_HOME", "/data/hermes")
    chain_id = int(os.environ.get("BOOA_CHAIN_ID", "1"))
    agent_json = os.path.join(home, "context", "agent.json")
    token_id = None
    try:
        with open(agent_json) as f:
            token_id = json.load(f).get("token_id")
    except (OSError, ValueError):
        pass
    if not token_id:
        return None
    info = wallet_status._read_local_wallet_info(home)
    if not info or not info.get("address"):
        return None
    return chain_id, int(token_id), info.get("name") or "my-agent", info["address"]


def main() -> int:
    """Print the deep-link the operator opens to set this agent's wallet onchain."""
    resolved = _resolve_from_config()
    if not resolved:
        print("No setup or no agent wallet yet. Create a wallet with OWS first.")
        return 1
    result = build_link_blob(*resolved)
    if not result.get("ok"):
        print(f"Could not build link: {result.get('error')}")
        return 1
    print(result["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
