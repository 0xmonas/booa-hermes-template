"""Operator approvals for trade-class onchain tools.

A tool call that would spend via x402 or trade on OpenSea does not execute on
the model's say-so: it parks a pending approval, the operator signs an EIP-712
ConsoleApproval with the controller wallet in the booa.app console, the
instance verifies the signer against the agent's current onchain controller,
and only then does a retry of the same call go through — once.

The signature binds tokenId + a keccak hash of the exact action JSON + a
one-shot nonce + a deadline, so an approval can never be replayed onto a
different action, a different agent, or a later day.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
import time
from typing import Any, Optional

HERMES_HOME = os.environ.get("HERMES_HOME", "/data/hermes")
_STORE = os.path.join(HERMES_HOME, ".approvals.json")
_LOCK = os.path.join(HERMES_HOME, ".approvals.lock")

APPROVAL_TTL_SECONDS = 600
RETENTION_SECONDS = 3600
DOMAIN_NAME = "BOOA Console"
DOMAIN_VERSION = "1"


def approvals_required() -> bool:
    """Default ON: turning the gate off is an explicit operator decision."""
    return os.environ.get("BOOA_REQUIRE_APPROVAL", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def canonical_action(action: dict) -> str:
    return json.dumps(action, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def action_hash(action: dict) -> str:
    from eth_utils import keccak

    return "0x" + keccak(canonical_action(action).encode()).hex()


def _token_chain() -> tuple[Optional[int], int]:
    chain_id = int(os.environ.get("BOOA_CHAIN_ID", "1"))
    try:
        with open(os.path.join(HERMES_HOME, "context", "agent.json")) as f:
            token_id = json.load(f).get("token_id")
        return (int(token_id) if token_id is not None else None), chain_id
    except (OSError, ValueError):
        return None, chain_id


def build_typed_data(token_id: int, chain_id: int, act_hash: str, nonce: int, deadline: int) -> dict:
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ConsoleApproval": [
                {"name": "tokenId", "type": "uint256"},
                {"name": "actionHash", "type": "bytes32"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        },
        "primaryType": "ConsoleApproval",
        "domain": {"name": DOMAIN_NAME, "version": DOMAIN_VERSION, "chainId": int(chain_id)},
        "message": {
            "tokenId": int(token_id),
            "actionHash": act_hash,
            "nonce": int(nonce),
            "deadline": int(deadline),
        },
    }


def recover_signer(typed: dict, signature: str) -> str:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    return Account.recover_message(encode_typed_data(full_message=typed), signature=signature).lower()


def resolve_controller() -> Optional[str]:
    """The agent's current controller (NFT owner of the bound BOOA), via the
    booa.app registry — which derives it from live onchain reads. The instance
    already trusts booa.app for identity and skill sync; a direct-RPC ownerOf
    read is the planned upgrade, not a change in trust model."""
    from . import agent_wallet_link

    token_id, chain_id = _token_chain()
    if token_id is None:
        return None
    reg = agent_wallet_link._fetch_registry(chain_id, token_id)
    if not reg or not reg.get("bound"):
        return None
    controller = reg.get("controller")
    return controller.lower() if isinstance(controller, str) and controller.startswith("0x") else None


def _hand_to_agent(path: str) -> None:
    """The store is written by both the root admin server (approve) and the
    agent-uid MCP process (gate/consume). A root write replaces the file
    root-owned, which would lock the MCP out — hand it back every time."""
    if os.geteuid() != 0:
        return
    try:
        import pwd
        rec = pwd.getpwnam("agent")
        os.chown(path, rec.pw_uid, rec.pw_gid)
        os.chmod(path, 0o600)
    except (ImportError, KeyError, OSError):
        pass


def _atomic_write(obj: dict) -> None:
    d = os.path.dirname(_STORE) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".approvals-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _STORE)
        _hand_to_agent(_STORE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _locked(fn):
    existed = os.path.exists(_LOCK)
    with open(_LOCK, "a") as lk:
        if not existed:
            _hand_to_agent(_LOCK)
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            try:
                with open(_STORE) as f:
                    records = json.load(f).get("records", [])
            except (OSError, ValueError):
                records = []
            now = time.time()
            records = [r for r in records if now - float(r.get("created_at", 0)) < RETENTION_SECONDS]
            result, records = fn(records, now)
            _atomic_write({"records": records})
            return result
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def gate(action: dict) -> Optional[dict]:
    """Called by a trade tool at its confirm=True moment. None means proceed
    (approvals off, or a matching approval was consumed just now). Anything
    else is the refusal payload the tool returns verbatim."""
    if not approvals_required():
        return None
    token_id, chain_id = _token_chain()
    if token_id is None:
        return {"ok": False, "error": "Approvals are required but this instance has no token identity yet."}
    ah = action_hash(action)

    def tx(records: list, now: float):
        for r in records:
            if r.get("action_hash") == ah and r.get("status") == "approved":
                if now > float(r.get("deadline", 0)):
                    r["status"] = "expired"
                    continue
                r["status"] = "consumed"
                r["consumed_at"] = now
                return None, records
        for r in records:
            if r.get("action_hash") == ah and r.get("status") == "pending" and now <= float(r.get("deadline", 0)):
                return {
                    "ok": False,
                    "approval_required": True,
                    "approval_id": r["id"],
                    "status": "pending",
                    "note": "Waiting for the operator to approve this action in the booa.app console (Data tab).",
                }, records
        rec = {
            "id": secrets.token_urlsafe(12),
            "action": action,
            "action_hash": ah,
            "token_id": int(token_id),
            "chain_id": int(chain_id),
            # String, not int: a 128-bit number would lose precision the moment
            # JavaScript JSON.parse turns it into a double, and the operator's
            # signature would never verify.
            "nonce": str(secrets.randbits(128)),
            "created_at": now,
            "deadline": int(now) + APPROVAL_TTL_SECONDS,
            "status": "pending",
        }
        records.append(rec)
        return {
            "ok": False,
            "approval_required": True,
            "approval_id": rec["id"],
            "status": "created",
            "note": "This action needs the operator's wallet approval. Ask them to approve it in the "
                    "booa.app console (Data tab), then call this tool again with the same arguments.",
        }, records

    return _locked(tx)


def list_records() -> list[dict]:
    def tx(records: list, now: float):
        out = []
        for r in records:
            if r.get("status") == "pending" and now > float(r.get("deadline", 0)):
                r["status"] = "expired"
            out.append({k: r.get(k) for k in (
                "id", "action", "action_hash", "token_id", "chain_id",
                "nonce", "created_at", "deadline", "status", "signer",
            )})
        return out, records

    return _locked(tx)


def approve(approval_id: str, signature: str) -> dict:
    controller = resolve_controller()
    if not controller:
        return {"ok": False, "error": "Could not resolve the agent's onchain controller."}

    def tx(records: list, now: float):
        for r in records:
            if r.get("id") != approval_id:
                continue
            if r.get("status") != "pending":
                return {"ok": False, "error": f"Approval is {r.get('status')}, not pending."}, records
            if now > float(r.get("deadline", 0)):
                r["status"] = "expired"
                return {"ok": False, "error": "Approval expired — ask the agent to request it again."}, records
            typed = build_typed_data(
                r["token_id"], r["chain_id"], r["action_hash"], r["nonce"], r["deadline"],
            )
            try:
                signer = recover_signer(typed, signature)
            except Exception:
                return {"ok": False, "error": "Invalid signature."}, records
            if signer != controller:
                return {"ok": False, "error": "Signer is not this agent's controller."}, records
            r["status"] = "approved"
            r["approved_at"] = now
            r["signer"] = signer
            return {"ok": True, "status": "approved"}, records
        return {"ok": False, "error": "Unknown approval id."}, records

    return _locked(tx)
