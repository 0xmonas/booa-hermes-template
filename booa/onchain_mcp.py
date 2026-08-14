"""booa-onchain — multi-chain onchain tools for the BOOA agent (MCP server).

Phase 1 is read-only: wallet, balances, token balances, generic contract reads,
gas. Signing and money-moving writes (send / swap / write_contract / x402) are
deliberately NOT here yet — they require OWS transaction signing plus
spending-limit and operator-approval gates, and land in a later phase.

The agent's wallet layer stays OWS: this server only reads chain state over
public JSON-RPC. It never touches keys.

Run: python -m booa.onchain_mcp  (Hermes launches it as a stdio MCP server)
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import time
from decimal import Decimal
from typing import Any, Optional

import httpx
import rlp
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_utils import keccak, to_bytes, to_checksum_address
from mcp.server.fastmcp import FastMCP

from . import wallet_status

MAX_UINT256 = (1 << 256) - 1
# OWS addresses EVM chains by CAIP-2 id; it has RPCs configured for both.
OWS_CHAIN = {"ethereum": "eip155:1", "base": "eip155:8453"}
OWS_BIN = os.environ.get("OWS_BIN") or "ows"

HERMES_HOME = os.environ.get("HERMES_HOME", "/data/hermes")


def _settings() -> dict:
    """Operator-set onchain settings (dashboard-written), overriding env. Read live per call
    so limit/allowlist/slippage changes take effect immediately, without a restart."""
    try:
        with open(os.path.join(HERMES_HOME, "onchain-settings.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def _cfg(key: str, default: str = "") -> str:
    v = _settings().get(key)
    return str(v) if v not in (None, "") else os.environ.get(key, default)

CHAINS: dict[str, dict] = {
    "ethereum": {
        "id": 1,
        "rpc": os.environ.get("ETH_RPC") or "https://ethereum-rpc.publicnode.com",
        "explorer": "https://etherscan.io",
    },
    "base": {
        "id": 8453,
        "rpc": os.environ.get("BASE_RPC") or "https://base-rpc.publicnode.com",
        "explorer": "https://basescan.org",
    },
}

# Convenience tokens surfaced in get_balances. Any other ERC-20 works via token_balance.
TOKENS: dict[str, dict[str, str]] = {
    "ethereum": {
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    },
    "base": {
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "WETH": "0x4200000000000000000000000000000000000006",
    },
}

mcp = FastMCP("booa-onchain")


def _rpc(chain: str, method: str, params: list) -> Any:
    c = CHAINS.get(chain)
    if not c:
        raise ValueError(f"Unknown chain '{chain}'. Supported: {', '.join(CHAINS)}")
    r = httpx.post(
        c["rpc"],
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"user-agent": "booa-onchain/1.0"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "rpc error"))
    return data["result"]


def _selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def _parse_types(sig: str) -> list[str]:
    inside = sig[sig.index("(") + 1: sig.rindex(")")].strip()
    return [t.strip() for t in inside.split(",")] if inside else []


def _eth_call(chain: str, to: str, data_hex: str) -> str:
    return _rpc(chain, "eth_call", [{"to": to_checksum_address(to), "data": data_hex}, "latest"])


def _agent_wallet() -> dict:
    info = wallet_status._read_local_wallet_info(HERMES_HOME) or {}
    return {"address": info.get("address"), "name": info.get("name") or "my-agent"}


def _format_units(raw: int, decimals: int) -> str:
    if decimals <= 0:
        return str(raw)
    s = str(raw).rjust(decimals + 1, "0")
    whole, frac = s[:-decimals], s[-decimals:].rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def _token_balance_raw(chain: str, token: str, holder: str) -> int:
    data = "0x" + _selector("balanceOf(address)").hex() + abi_encode(["address"], [to_checksum_address(holder)]).hex()
    return int(_eth_call(chain, token, data), 16)


def _token_decimals(chain: str, token: str) -> int:
    res = _eth_call(chain, token, "0x" + _selector("decimals()").hex())
    return int(res, 16) if res and res != "0x" else 18


def _token_symbol(chain: str, token: str) -> str:
    try:
        res = _eth_call(chain, token, "0x" + _selector("symbol()").hex())
        return abi_decode(["string"], bytes.fromhex(res[2:]))[0]
    except Exception:
        return "?"


@mcp.tool()
def supported_chains() -> dict:
    """List the chains this tool can read, with chain IDs and block explorers."""
    return {"ok": True, "chains": {k: {"chainId": v["id"], "explorer": v["explorer"]} for k, v in CHAINS.items()}}


@mcp.tool()
def get_wallet() -> dict:
    """Return the agent's own OWS wallet address and name."""
    w = _agent_wallet()
    if not w.get("address"):
        return {"ok": False, "error": "No agent wallet yet. Ask the operator to run 'set up my wallet'."}
    return {"ok": True, "address": to_checksum_address(w["address"]), "name": w["name"]}


@mcp.tool()
def get_balances(chains: str = "ethereum,base", address: str = "") -> dict:
    """Native + USDC/WETH balances for an address (defaults to the agent's own wallet) across comma-separated chains."""
    addr = address or (_agent_wallet().get("address") or "")
    if not addr:
        return {"ok": False, "error": "No address given and no agent wallet set."}
    out: dict[str, Any] = {}
    for chain in [c.strip() for c in chains.split(",") if c.strip()]:
        if chain not in CHAINS:
            out[chain] = {"error": "unsupported chain"}
            continue
        try:
            wei = int(_rpc(chain, "eth_getBalance", [to_checksum_address(addr), "latest"]), 16)
            entry: dict[str, Any] = {
                "native": {"symbol": "ETH", "amount": _format_units(wei, 18), "raw": str(wei)},
                "tokens": {},
            }
            for sym, taddr in TOKENS.get(chain, {}).items():
                try:
                    entry["tokens"][sym] = _format_units(_token_balance_raw(chain, taddr, addr), _token_decimals(chain, taddr))
                except Exception:
                    pass
            out[chain] = entry
        except Exception as e:
            out[chain] = {"error": str(e)}
    return {"ok": True, "address": to_checksum_address(addr), "balances": out}


@mcp.tool()
def token_balance(chain: str, token: str, address: str = "") -> dict:
    """ERC-20 balance (with symbol and decimals) for any token contract on a chain. Address defaults to the agent's wallet."""
    addr = address or (_agent_wallet().get("address") or "")
    if not addr:
        return {"ok": False, "error": "No address given and no agent wallet set."}
    try:
        dec = _token_decimals(chain, token)
        raw = _token_balance_raw(chain, token, addr)
        return {
            "ok": True, "chain": chain, "token": to_checksum_address(token),
            "symbol": _token_symbol(chain, token), "decimals": dec,
            "amount": _format_units(raw, dec), "raw": str(raw),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def read_contract(chain: str, address: str, signature: str, args: Optional[list] = None, returns: str = "") -> dict:
    """Call a read-only (view/pure) contract function.

    signature: e.g. 'balanceOf(address)'. args: the inputs. returns: optional
    output types like 'uint256' or 'address,uint256' so the result is decoded.
    """
    args = args or []
    try:
        types = _parse_types(signature)
        data = "0x" + _selector(signature).hex() + (abi_encode(types, args).hex() if types else "")
        res = _eth_call(chain, address, data)
        decoded = None
        if returns:
            out_types = [t.strip() for t in returns.split(",") if t.strip()]
            vals = abi_decode(out_types, bytes.fromhex(res[2:]))
            decoded = [v.hex() if isinstance(v, (bytes, bytearray)) else (str(v) if isinstance(v, int) else v) for v in vals]
        return {"ok": True, "chain": chain, "raw": res, "decoded": decoded}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def gas_price(chain: str = "ethereum") -> dict:
    """Current gas price on the chain, in gwei."""
    try:
        wei = int(_rpc(chain, "eth_gasPrice", []), 16)
        return {"ok": True, "chain": chain, "gwei": round(wei / 1e9, 3), "wei": str(wei)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── write layer (opt-in: BOOA_ONCHAIN_WRITES=1) ─────────────────────────────
# Every write follows preview → confirm: without confirm=True the tool returns
# what it WOULD do and signs nothing. Signing/broadcast is delegated to OWS
# (`ows sign send-tx`); this server never sees a private key. The real spending
# gate is an OWS policy attached to the agent's API key — this layer adds two
# backstops: the preview step and a refusal of unlimited ERC-20 approvals.

def _writes_enabled() -> bool:
    return _cfg("BOOA_ONCHAIN_WRITES").lower() in ("1", "true", "yes")


def _fees(chain: str) -> tuple[int, int]:
    base = int(_rpc(chain, "eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)
    try:
        prio = int(_rpc(chain, "eth_maxPriorityFeePerGas", []), 16)
    except Exception:
        prio = 10 ** 9
    return prio, base * 2 + prio


def _build_unsigned_1559(chain: str, frm: str, to: str, value: int, data_hex: str) -> tuple[str, dict]:
    cid = CHAINS[chain]["id"]
    frm, to = to_checksum_address(frm), to_checksum_address(to)
    data_hex = data_hex or "0x"
    nonce = int(_rpc(chain, "eth_getTransactionCount", [frm, "pending"]), 16)
    gas = int(_rpc(chain, "eth_estimateGas", [{"from": frm, "to": to, "value": hex(value), "data": data_hex}]), 16)
    gas = gas + gas // 5  # 20% buffer
    prio, maxfee = _fees(chain)
    fields = [cid, nonce, prio, maxfee, gas, to_bytes(hexstr=to), value, to_bytes(hexstr=data_hex), []]
    unsigned = b"\x02" + rlp.encode(fields)
    meta = {"nonce": nonce, "gas": gas, "maxFeePerGas": maxfee, "maxPriorityFeePerGas": prio,
            "gas_gwei": round(maxfee / 1e9, 2), "chainId": cid}
    return "0x" + unsigned.hex(), meta


def _ows_send(chain: str, wallet: str, unsigned_hex: str) -> str:
    cmd = [OWS_BIN, "sign", "send-tx", "--chain", OWS_CHAIN[chain], "--wallet", wallet,
           "--tx", unsigned_hex, "--json", "--rpc-url", CHAINS[chain]["rpc"]]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=os.environ.copy())
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "ows sign send-tx failed").strip())
    try:
        j = json.loads(out.stdout)
        return j.get("hash") or j.get("txHash") or j.get("transaction_hash") or out.stdout.strip()
    except Exception:
        return out.stdout.strip()


def _wait_receipt(chain: str, txh: str, timeout: int = 150) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _rpc(chain, "eth_getTransactionReceipt", [txh])
        if r:
            return r
        time.sleep(3)
    raise RuntimeError(f"tx {txh} not mined within {timeout}s")


def _ows_sign_message(wallet: str, chain: str, *, message: str = "", typed_data: str = "") -> str:
    cmd = [OWS_BIN, "sign", "message", "--chain", OWS_CHAIN.get(chain, "eip155:1"), "--wallet", wallet, "--json"]
    cmd += ["--typed-data", typed_data] if typed_data else ["--message", message]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=os.environ.copy())
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "ows sign message failed").strip())
    try:
        return json.loads(out.stdout).get("signature", out.stdout.strip())
    except Exception:
        return out.stdout.strip()


# ── autonomy guardrails: allowlist + two ETH caps ───────────────────────────
# Preview → confirm protects interactive use, but a cron fires with no human in
# the loop, so these are the backstop for autonomous actions. All are opt-in.
# These gates bind tool calls only: the agent also has a shell, so a spend rule
# in the OWS policy is what actually constrains a wallet key it can reach.
#   BOOA_SEND_ALLOWLIST  destinations writes may target (wallets/contracts)
#   BOOA_MAX_TX_ETH      per-transaction native cap
#   BOOA_DAILY_CAP_ETH   general rolling-day native cap (tracked in a ledger)
_SPEND_LEDGER = os.path.join(HERMES_HOME, "onchain-spend.json")
_SPEND_LOCK = os.path.join(HERMES_HOME, ".onchain-spend.lock")
_WINDOW_SECONDS = 24 * 60 * 60


def _allowlist() -> set:
    return {a.strip().lower() for a in _cfg("BOOA_SEND_ALLOWLIST").split(",") if a.strip()}


def _cap(name: str) -> Decimal:
    raw = (_cfg(name, "0") or "0").strip()
    if not raw:
        return Decimal(0)
    try:
        value = Decimal(raw)
    except Exception:
        # A typo like "0.1 ETH" used to parse as 0, which means "no limit" — the
        # operator would believe a cap was active while nothing enforced it.
        raise PermissionError(
            f"{name} is set to {raw!r}, which is not a number. Writes are blocked until it is fixed."
        )
    if value < 0:
        raise PermissionError(f"{name} is negative ({raw}). Writes are blocked until it is fixed.")
    return value


def _atomic_write_json(path: str, obj: dict) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".spend-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _prune(entries: Any, now: float) -> tuple[list, Decimal]:
    cutoff = now - _WINDOW_SECONDS
    kept: list = []
    total = Decimal(0)
    if isinstance(entries, list):
        for e in entries:
            try:
                ts = float(e["ts"])
                amt = Decimal(str(e["amount"]))
            except Exception:
                continue
            if amt > 0 and ts >= cutoff:
                kept.append({"ts": ts, "amount": str(amt)})
                total += amt
    return kept, total


def _spent_today() -> Decimal:
    """Native ETH reserved in the trailing 24h. Read-only, for previews — the
    authoritative check-and-reserve happens in _record_spend under a lock."""
    try:
        with open(_SPEND_LOCK, "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_SH)
            try:
                try:
                    with open(_SPEND_LEDGER) as f:
                        entries = json.load(f).get("entries", [])
                except FileNotFoundError:
                    entries = []
                return _prune(entries, time.time())[1]
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)
    except Exception:
        return Decimal(0)


def _record_spend(native_eth: Decimal) -> None:
    """Check the daily cap and reserve against it in one locked step, before the tx is
    broadcast. Two concurrent callers used to both read the old total and both pass, and
    a calendar-day reset let the full cap go out at 23:59 and again at 00:01. A ledger
    that cannot be written blocks the spend rather than silently allowing it — a failed
    broadcast therefore still consumes its reservation, which is the safe direction."""
    if native_eth <= 0:
        return
    daily = _cap("BOOA_DAILY_CAP_ETH")
    if daily <= 0:
        return
    try:
        lk = open(_SPEND_LOCK, "a")
    except OSError as e:
        raise PermissionError(
            f"Cannot open the spend-ledger lock ({e}); blocking the spend so the daily "
            "cap (BOOA_DAILY_CAP_ETH) cannot be bypassed."
        )
    try:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            with open(_SPEND_LEDGER) as f:
                entries = json.load(f).get("entries", [])
        except FileNotFoundError:
            entries = []
        except Exception:
            entries = []
        now = time.time()
        kept, total = _prune(entries, now)
        remaining = daily - total
        if native_eth > remaining:
            raise PermissionError(
                f"Moves {native_eth} ETH but only {remaining} ETH left in the rolling "
                f"24h limit ({daily} BOOA_DAILY_CAP_ETH)."
            )
        kept.append({"ts": now, "amount": str(native_eth)})
        try:
            _atomic_write_json(_SPEND_LEDGER, {"entries": kept})
        except Exception as e:
            raise PermissionError(
                f"Could not persist the spend ledger ({e}); blocking the spend so the "
                "daily cap cannot be bypassed by a read-only or full volume."
            )
    finally:
        fcntl.flock(lk, fcntl.LOCK_UN)
        lk.close()


def _guard_info(dest: str, native_eth: Decimal, non_native: bool = False) -> dict:
    al, per, daily = _allowlist(), _cap("BOOA_MAX_TX_ETH"), _cap("BOOA_DAILY_CAP_ETH")
    info = {
        "allowlist_active": bool(al),
        "destination_allowed": (not al) or (to_checksum_address(dest).lower() in al),
        "native_moved_eth": str(native_eth),
        "per_tx_cap_eth": str(per) if per > 0 else None,
    }
    if non_native:
        info["token_amount_uncapped"] = "ETH caps do not bound token amounts; the allowlist is the gate."
    if daily > 0:
        info["daily_cap_eth"] = str(daily)
        info["daily_remaining_eth"] = str(daily - _spent_today())
    return info


def _check_caps(native_eth: Decimal) -> None:
    per = _cap("BOOA_MAX_TX_ETH")
    if per > 0 and native_eth > per:
        raise PermissionError(f"Moves {native_eth} ETH, over the per-tx limit {per} (BOOA_MAX_TX_ETH).")
    # Checking the daily cap and reserving against it must be one step, or two
    # concurrent calls both read the same remaining balance and both proceed.
    _record_spend(native_eth)


def _guard(dest: str, native_eth: Decimal, non_native: bool = False) -> None:
    al = _allowlist()
    # The ETH caps bound native value only. A token or NFT amount is not priced, so it
    # reaches here as 0 and slips past them — an operator who set caps would be moving
    # unlimited USDC without knowing. With no allowlist to gate the destination there is
    # nothing left holding it, so refuse rather than pass it through at value 0.
    if non_native and not al and (_cap("BOOA_MAX_TX_ETH") > 0 or _cap("BOOA_DAILY_CAP_ETH") > 0):
        raise PermissionError(
            "BOOA_MAX_TX_ETH / BOOA_DAILY_CAP_ETH bound native ETH only, and a token amount "
            "cannot be priced here. Set BOOA_SEND_ALLOWLIST to the destinations tokens may go "
            "to — token transfers are refused while caps are set but the allowlist is empty."
        )
    if al and to_checksum_address(dest).lower() not in al:
        raise PermissionError(f"{to_checksum_address(dest)} is not in BOOA_SEND_ALLOWLIST. Add it to allow this destination.")
    _check_caps(native_eth)


# A signature can move value without ever sending a transaction, so the ETH caps
# are blind to it: one Permit hands an attacker the whole token balance. These
# types get the same destination allowlist a transfer would.
_SPENDER_TYPES = {"permit", "permitsingle", "permitbatch", "permittransferfrom", "permitwitnesstransferfrom"}
_ORDER_TYPES = {"ordercomponents", "order", "seaportorder"}
_UNLIMITED = 2 ** 255


def _check_typed_data_safety(typed_data_json: str) -> None:
    try:
        data = json.loads(typed_data_json)
    except Exception:
        raise PermissionError("typed_data must be valid JSON.")
    primary = str(data.get("primaryType", "")).lower()
    message = data.get("message") or {}
    al = _allowlist()

    if primary in _SPENDER_TYPES:
        spender = message.get("spender") or message.get("operator") or ""
        details = message.get("details")
        if not spender and isinstance(details, dict):
            spender = details.get("spender", "")
        if al:
            if not spender:
                raise PermissionError("Refusing to sign an approval with no spender while BOOA_SEND_ALLOWLIST is set.")
            if to_checksum_address(spender).lower() not in al:
                raise PermissionError(
                    f"{to_checksum_address(spender)} is not in BOOA_SEND_ALLOWLIST. "
                    "Signing this approval would let it spend the agent's tokens."
                )
        for key in ("value", "amount", "allowance"):
            raw = message.get(key)
            if raw is None and isinstance(details, dict):
                raw = details.get(key)
            try:
                if raw is not None and int(str(raw), 0) >= _UNLIMITED:
                    raise PermissionError("Refusing to sign an unlimited token approval.")
            except (TypeError, ValueError):
                continue

    if primary in _ORDER_TYPES and al:
        raise PermissionError(
            "Refusing to sign a marketplace order directly. Use opensea_list, which prices and gates the sale."
        )


# Honeypot guard: the agent may only swap INTO a token that is known-safe
# (native, USDC, WETH) or one the operator explicitly trusts via
# BOOA_SWAP_TOKEN_ALLOWLIST. Buying an arbitrary token is the classic drain
# (a honeypot lets you buy but never sell), so this is refuse-by-default.
def _swap_token_allowed(chain: str, token: str) -> bool:
    t = token.lower()
    if t == "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee":  # native ETH sentinel
        return True
    safe = {a.lower() for a in TOKENS.get(chain, {}).values()}
    allow = {a.strip().lower() for a in _cfg("BOOA_SWAP_TOKEN_ALLOWLIST").split(",") if a.strip()}
    return t in safe or t in allow


# ── OpenSea execution helpers ────────────────────────────────────────────────
# OpenSea's REST endpoints return a transaction as function + decoded input_data
# + a calldata_suffix tag (Seaport). We encode it ourselves, then ALWAYS simulate
# via eth_call before signing — a wrong encoding or an invalid order reverts in
# simulation and never reaches the chain.
OPENSEA_CHAIN = {"ethereum": "ethereum", "base": "base"}
SEAPORT_16 = "0x0000000000000068F116a894984e2DB1123eB395"
OPENSEA_CONDUIT = "0x1E0049783F008A0085193E00003D00cd54003c71"


def _is_approved_for_all(chain: str, contract: str, owner: str, operator: str) -> bool:
    data = "0x" + _selector("isApprovedForAll(address,address)").hex() + abi_encode(
        ["address", "address"], [to_checksum_address(owner), to_checksum_address(operator)]).hex()
    return int(_eth_call(chain, contract, data), 16) == 1


def _opensea_api(path: str, method: str = "GET", body: Optional[dict] = None) -> dict:
    key = _cfg("OPENSEA_API_KEY")
    if not key:
        raise RuntimeError("OPENSEA_API_KEY not set — OpenSea actions are unavailable.")
    r = httpx.request(method, "https://api.opensea.io/api/v2" + path,
                      headers={"X-API-KEY": key, "accept": "application/json"},
                      json=body, timeout=25)
    r.raise_for_status()
    return r.json()


def _seaport_types(type_str: str) -> list[str]:
    inner = type_str[1:-1]
    out, depth, cur = [], 0, ""
    for ch in inner:
        if ch == "(":
            depth += 1; cur += ch
        elif ch == ")":
            depth -= 1; cur += ch
        elif ch == "," and depth == 0:
            out.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [t.strip() for t in out]


def _seaport_coerce(typ: str, val):
    if typ.endswith("[]"):
        return [_seaport_coerce(typ[:-2], v) for v in val]
    if typ.startswith("(") and typ.endswith(")"):
        subs = _seaport_types(typ)
        vals = list(val.values()) if isinstance(val, dict) else val
        return tuple(_seaport_coerce(t, x) for t, x in zip(subs, vals))
    if typ.startswith("address"):
        return to_checksum_address(val)
    if typ.startswith("uint") or typ.startswith("int"):
        return int(val)
    if typ.startswith("bytes"):
        return to_bytes(hexstr=val)
    if typ == "bool":
        return bool(val)
    return val


def _encode_tx_calldata(function: str, input_data: dict, calldata_suffix: str) -> str:
    arg_types = _seaport_types(function[function.index("("):])
    selector = keccak(text=function)[:4]
    coerced = [_seaport_coerce(t, v) for t, v in zip(arg_types, list(input_data.values()))]
    suffix = to_bytes(hexstr=calldata_suffix) if calldata_suffix else b""
    return "0x" + (selector + abi_encode(arg_types, coerced) + suffix).hex()


def _simulate(chain: str, frm: str, to: str, value: int, data: str) -> Optional[str]:
    try:
        _rpc(chain, "eth_call", [{"from": to_checksum_address(frm), "to": to_checksum_address(to),
                                  "value": hex(value), "data": data}, "latest"])
        return None
    except Exception as e:
        return str(e)


def _opensea_collection_verified(chain: str, contract: str):
    """Return (is_verified, slug, status) for the NFT contract, or None if unknown."""
    try:
        c = _opensea_api(f"/chain/{OPENSEA_CHAIN[chain]}/contract/{to_checksum_address(contract)}")
        slug = c.get("collection")
        if not slug:
            return None
        col = _opensea_api(f"/collections/{slug}")
        status = col.get("safelist_request_status") or col.get("safelist_status") or ""
        return (status in ("verified", "approved"), slug, status)
    except Exception:
        return None


if _writes_enabled():

    @mcp.tool()
    def send(chain: str, to: str, amount: str, token: str = "", confirm: bool = False) -> dict:
        """Send native ETH or an ERC-20 token. amount is human units (e.g. '0.5'). token: contract address, or empty for native ETH. Returns a preview unless confirm=True, then it signs via OWS and broadcasts."""
        w = _agent_wallet()
        if not w.get("address"):
            return {"ok": False, "error": "No agent wallet set."}
        try:
            if token:
                dec = _token_decimals(chain, token)
                raw = int(Decimal(amount) * (10 ** dec))
                data = "0x" + _selector("transfer(address,uint256)").hex() + abi_encode(["address", "uint256"], [to_checksum_address(to), raw]).hex()
                target, value = token, 0
                summary = f"{amount} {_token_symbol(chain, token)} → {to} on {chain}"
            else:
                value = int(Decimal(amount) * (10 ** 18))
                data, target = "0x", to
                summary = f"{amount} ETH → {to} on {chain}"
                cap = _cfg("BOOA_MAX_TX_ETH", "0") or "0"
                if Decimal(cap) > 0 and Decimal(amount) > Decimal(cap):
                    return {"ok": False, "error": f"Native amount {amount} exceeds BOOA_MAX_TX_ETH cap ({cap}). Raise the cap to proceed."}
            native_eth = Decimal(0) if token else Decimal(amount)
            unsigned, meta = _build_unsigned_1559(chain, w["address"], target, value, data)
            preview = {"action": "send", "chain": chain, "from": to_checksum_address(w["address"]), "summary": summary, **meta, "guardrails": _guard_info(to, native_eth, non_native=bool(token))}
            if not confirm:
                return {"ok": True, "preview": preview, "note": "Nothing sent. Show this to the operator, then call again with confirm=true to broadcast."}
            _guard(to, native_eth, non_native=bool(token))
            txh = _ows_send(chain, w["name"], unsigned)
            return {"ok": True, "sent": True, "tx": txh, "explorer": f"{CHAINS[chain]['explorer']}/tx/{txh}", "summary": summary}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def write_contract(chain: str, address: str, signature: str, args: Optional[list] = None, value: str = "0", confirm: bool = False) -> dict:
        """Call a state-changing contract function. signature like 'transfer(address,uint256)', args the inputs, value native wei to attach. Preview unless confirm=True. Refuses unlimited ERC-20 approvals."""
        args = args or []
        w = _agent_wallet()
        if not w.get("address"):
            return {"ok": False, "error": "No agent wallet set."}
        try:
            if signature.strip().startswith("approve(") and len(args) >= 2 and int(args[1]) >= (1 << 255):
                return {"ok": False, "error": "Refusing an unlimited/near-max approval. Approve only the exact amount needed."}
            types = _parse_types(signature)
            data = "0x" + _selector(signature).hex() + (abi_encode(types, args).hex() if types else "")
            val = int(value)
            native_eth = Decimal(val) / Decimal(10 ** 18)
            unsigned, meta = _build_unsigned_1559(chain, w["address"], address, val, data)
            preview = {"action": "write_contract", "chain": chain, "to": to_checksum_address(address), "function": signature, "args": [str(a) for a in args], "value": str(val), **meta, "guardrails": _guard_info(address, native_eth)}
            if not confirm:
                return {"ok": True, "preview": preview, "note": "Nothing sent. confirm=true to broadcast."}
            _guard(address, native_eth)
            txh = _ows_send(chain, w["name"], unsigned)
            return {"ok": True, "sent": True, "tx": txh, "explorer": f"{CHAINS[chain]['explorer']}/tx/{txh}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    KYBER = {"ethereum": "ethereum", "base": "base"}
    ETH_SENTINEL = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

    @mcp.tool()
    def swap(chain: str, sell_token: str, buy_token: str, sell_amount: str, slippage_bps: int = 50, confirm: bool = False) -> dict:
        """Swap tokens via the KyberSwap aggregator. sell_token/buy_token: contract address or 'ETH' for native. sell_amount in human units. slippage_bps: max slippage (50 = 0.5%). Preview shows the quote; confirm=True approves the exact amount (ERC-20 sells only) and executes the swap."""
        w = _agent_wallet()
        if not w.get("address"):
            return {"ok": False, "error": "No agent wallet set."}
        if chain not in KYBER:
            return {"ok": False, "error": f"Swap is not wired for {chain}."}
        try:
            owner = to_checksum_address(w["address"])
            sell = ETH_SENTINEL if sell_token.upper() == "ETH" else to_checksum_address(sell_token)
            buy = ETH_SENTINEL if buy_token.upper() == "ETH" else to_checksum_address(buy_token)
            is_native = sell.lower() == ETH_SENTINEL.lower()
            dec = 18 if is_native else _token_decimals(chain, sell)
            amount_in = int(Decimal(sell_amount) * (10 ** dec))
            max_slip = int(_cfg("BOOA_MAX_SLIPPAGE_BPS", "300") or "300")
            if int(slippage_bps) > max_slip:
                return {"ok": False, "error": f"Slippage {slippage_bps} bps exceeds the cap {max_slip} (BOOA_MAX_SLIPPAGE_BPS)."}
            if not _swap_token_allowed(chain, buy):
                return {"ok": False, "error": f"Buy-side token {buy} is not verified/allowlisted. Only native, USDC, WETH, or a token in BOOA_SWAP_TOKEN_ALLOWLIST can be bought — this blocks honeypots."}
            base = f"https://aggregator-api.kyberswap.com/{KYBER[chain]}/api/v1"
            hdr = {"x-client-id": "booa-onchain"}
            rt = httpx.get(f"{base}/routes", params={"tokenIn": sell, "tokenOut": buy, "amountIn": str(amount_in)}, headers=hdr, timeout=25).json()
            summ = (rt.get("data") or {}).get("routeSummary")
            router = (rt.get("data") or {}).get("routerAddress")
            if not summ or not router:
                return {"ok": False, "error": f"No route found: {rt.get('message', 'unknown')}"}
            router = to_checksum_address(router)
            bd = httpx.post(f"{base}/route/build", json={"routeSummary": summ, "sender": owner, "recipient": owner, "slippageTolerance": int(slippage_bps)}, headers=hdr, timeout=25).json()
            built = bd.get("data") or {}
            calldata, amount_out = built.get("data"), built.get("amountOut")
            if not calldata:
                return {"ok": False, "error": f"Route build failed: {bd.get('message', 'unknown')}"}
            out_dec = 18 if buy.lower() == ETH_SENTINEL.lower() else _token_decimals(chain, buy)
            needs_approval = False
            if not is_native:
                allow_data = "0x" + _selector("allowance(address,address)").hex() + abi_encode(["address", "address"], [owner, router]).hex()
                needs_approval = int(_eth_call(chain, sell, allow_data), 16) < amount_in
            preview = {
                "action": "swap", "chain": chain,
                "sell": f"{sell_amount} {'ETH' if is_native else _token_symbol(chain, sell)}",
                "buy_estimate": _format_units(int(amount_out), out_dec) if amount_out else "?",
                "router": router, "slippage_bps": slippage_bps, "needs_approval": needs_approval,
            }
            native_eth = Decimal(sell_amount) if is_native else Decimal(0)
            per = _cap("BOOA_MAX_TX_ETH"); daily = _cap("BOOA_DAILY_CAP_ETH")
            preview["guardrails"] = {
                "native_moved_eth": str(native_eth),
                "per_tx_cap_eth": str(per) if per > 0 else None,
                "daily_remaining_eth": str(daily - _spent_today()) if daily > 0 else None,
                "buy_token_verified": True,
                "slippage_cap_bps": max_slip,
            }
            if not confirm:
                return {"ok": True, "preview": preview, "note": "Nothing swapped. confirm=true approves the exact amount (if needed) and executes."}
            # The router is chosen by the aggregator response, so it must clear the
            # same allowlist as any other destination we hand value to.
            _guard(router, native_eth)
            txs = []
            if needs_approval:
                appr = "0x" + _selector("approve(address,uint256)").hex() + abi_encode(["address", "uint256"], [router, amount_in]).hex()
                u1, _ = _build_unsigned_1559(chain, owner, sell, 0, appr)
                approve_tx = _ows_send(chain, w["name"], u1)
                _wait_receipt(chain, approve_tx)  # swap reverts if allowance isn't mined yet
                txs.append({"approve": approve_tx})
            u2, _ = _build_unsigned_1559(chain, owner, router, amount_in if is_native else 0, calldata)
            txs.append({"swap": _ows_send(chain, w["name"], u2)})
            return {"ok": True, "sent": True, "txs": txs, "summary": preview}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def sign_message(message: str, chain: str = "ethereum") -> dict:
        """Sign a plain text message (EIP-191) with the agent's OWS wallet."""
        w = _agent_wallet()
        if not w.get("address"):
            return {"ok": False, "error": "No agent wallet set."}
        try:
            return {"ok": True, "signature": _ows_sign_message(w["name"], chain, message=message)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def sign_typed_data(typed_data_json: str, chain: str = "ethereum") -> dict:
        """Sign EIP-712 typed data (a JSON string) with the agent's OWS wallet. Use for SIWA, x402 auth, and onchain consents."""
        w = _agent_wallet()
        if not w.get("address"):
            return {"ok": False, "error": "No agent wallet set."}
        try:
            _check_typed_data_safety(typed_data_json)
            return {"ok": True, "signature": _ows_sign_message(w["name"], chain, typed_data=typed_data_json)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def x402_pay(url: str, method: str = "GET", body: str = "", confirm: bool = False) -> dict:
        """Pay for an x402-enabled API call through OWS. Preview unless confirm=True, then it pays and returns the response."""
        w = _agent_wallet()
        if not w.get("address"):
            return {"ok": False, "error": "No agent wallet set."}
        if not confirm:
            return {"ok": True, "preview": {"action": "x402_pay", "url": url, "method": method}, "note": "Not paid. confirm=true to pay and fetch."}
        try:
            cmd = [OWS_BIN, "pay", "request", "--wallet", w["name"], "--method", method]
            if body:
                cmd += ["--body", body]
            cmd.append(url)
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=os.environ.copy())
            if out.returncode != 0:
                raise RuntimeError((out.stderr or out.stdout or "ows pay failed").strip())
            return {"ok": True, "paid": True, "response": out.stdout.strip()[:6000]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def opensea_buy(chain: str, order_hash: str, protocol_address: str = SEAPORT_16, confirm: bool = False) -> dict:
        """Buy an NFT on OpenSea by fulfilling a listing. Get order_hash (and protocol_address) from the OpenSea search/listings tools. The transaction is fetched from OpenSea, encoded, and SIMULATED before signing — a bad order, wrong encoding, or unaffordable price is refused. Previews unless confirm=True."""
        w = _agent_wallet()
        if not w.get("address"):
            return {"ok": False, "error": "No agent wallet set."}
        if chain not in OPENSEA_CHAIN:
            return {"ok": False, "error": f"OpenSea buy is not wired for {chain}."}
        try:
            owner = to_checksum_address(w["address"])
            fd = _opensea_api("/listings/fulfillment_data", "POST", {
                "listing": {"hash": order_hash, "chain": OPENSEA_CHAIN[chain], "protocol_address": to_checksum_address(protocol_address)},
                "fulfiller": {"address": owner},
            })
            tx = fd["fulfillment_data"]["transaction"]
            to = to_checksum_address(tx["to"])
            value = int(tx["value"])
            data = _encode_tx_calldata(tx["function"], tx["input_data"], tx.get("calldata_suffix", ""))
            native_eth = Decimal(value) / Decimal(10 ** 18)
            params = tx["input_data"].get("parameters", {})
            nft_contract = params.get("offerToken", "")
            nft = f"{nft_contract} #{params.get('offerIdentifier', '?')}"

            # verified-collection guard (scam / impersonation protection)
            require_verified = _cfg("BOOA_OPENSEA_REQUIRE_VERIFIED", "1").lower() in ("1", "true", "yes")
            ver = _opensea_collection_verified(chain, nft_contract) if nft_contract else None
            verified, slug = (ver[0], ver[1]) if ver else (None, None)
            if require_verified and verified is not True:
                return {"ok": False, "error": f"Collection {slug or nft_contract} is not OpenSea-verified (status: {ver[2] if ver else 'unknown'}). Set BOOA_OPENSEA_REQUIRE_VERIFIED=0 to allow unverified collections."}

            # simulate before signing: proves encoding + order validity + affordability
            sim_err = _simulate(chain, owner, to, value, data)
            per, daily = _cap("BOOA_MAX_TX_ETH"), _cap("BOOA_DAILY_CAP_ETH")
            preview = {
                "action": "opensea_buy", "chain": chain, "nft": nft, "collection": slug,
                "verified": verified, "price_eth": str(native_eth), "seaport": to,
                "simulation": "ok" if sim_err is None else f"would revert: {sim_err[:160]}",
                "guardrails": {"per_tx_cap_eth": str(per) if per > 0 else None,
                               "daily_remaining_eth": str(daily - _spent_today()) if daily > 0 else None},
            }
            if sim_err is not None:
                return {"ok": False, "error": f"Simulation failed, not buying: {sim_err[:200]}", "preview": preview}
            if not confirm:
                return {"ok": True, "preview": preview, "note": "Simulated OK, nothing bought. Show this to the operator, then confirm=true to buy."}
            _check_caps(native_eth)
            unsigned, _ = _build_unsigned_1559(chain, owner, to, value, data)
            txh = _ows_send(chain, w["name"], unsigned)
            return {"ok": True, "bought": True, "nft": nft, "tx": txh, "explorer": f"{CHAINS[chain]['explorer']}/tx/{txh}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def opensea_list(chain: str, contract: str, token_id: str, price_eth: str, confirm: bool = False) -> dict:
        """List an NFT you own for sale on OpenSea at price_eth (native ETH). Fetches the order from OpenSea, runs any approval it requires (approving the collection to OpenSea's conduit) via OWS, signs the Seaport order with OWS, and publishes it. Previews unless confirm=True. This SELLS your NFT at the price you set — a too-low price can be bought instantly, so set it carefully."""
        w = _agent_wallet()
        if not w.get("address"):
            return {"ok": False, "error": "No agent wallet set."}
        if chain not in OPENSEA_CHAIN:
            return {"ok": False, "error": f"OpenSea listing is not wired for {chain}."}
        try:
            owner = to_checksum_address(w["address"])
            price_wei = int(Decimal(price_eth) * (10 ** 18))
            actions = _opensea_api("/listings/actions", "POST", {
                "address": owner,
                "items": [{"chain": OPENSEA_CHAIN[chain], "contract": to_checksum_address(contract),
                           "token_id": str(token_id), "quantity": 1,
                           "price": {"amount": str(price_wei), "currency": "0x0000000000000000000000000000000000000000"}}],
                "use_creator_fee": True,
            })
            tx_steps, sign_msg = [], None
            for s in actions.get("steps", []):
                for name, action in s.items():
                    if not isinstance(action, dict):
                        continue
                    if "transaction" in action:
                        t = action["transaction"]
                        unwrap = lambda x: x["value"] if isinstance(x, dict) else x
                        tx_steps.append({"name": name, "to": to_checksum_address(unwrap(t["to"])),
                                         "data": unwrap(t["data"]), "value": int(unwrap(t.get("value")) or 0)})
                    if "signatureRequest" in action:
                        sign_msg = action["signatureRequest"]["message"]
            if not sign_msg:
                return {"ok": False, "error": "OpenSea returned no order to sign for this item."}
            typed = json.loads(sign_msg) if isinstance(sign_msg, str) else sign_msg
            proto = typed.get("domain", {}).get("verifyingContract", SEAPORT_16)
            preview = {"action": "opensea_list", "chain": chain,
                       "nft": f"{to_checksum_address(contract)} #{token_id}", "price_eth": str(price_eth),
                       "onchain_steps": [x["name"] for x in tx_steps],
                       "approves_collection_to_conduit": any("approval" in x["name"].lower() for x in tx_steps)}
            if not confirm:
                return {"ok": True, "preview": preview, "note": "Nothing listed. This SELLS your NFT at this price — show it to the operator, then confirm=true to approve (if needed), sign, and publish."}
            done = []
            for x in tx_steps:  # approval / cancel — ready calldata, OWS-signed
                unsigned, _ = _build_unsigned_1559(chain, owner, x["to"], x["value"], x["data"])
                txh = _ows_send(chain, w["name"], unsigned)
                _wait_receipt(chain, txh)
                done.append({x["name"]: txh})
            sig = _ows_sign_message(w["name"], chain, typed_data=json.dumps(typed))
            msg = typed["message"]
            params = {**msg, "totalOriginalConsiderationItems": len(msg.get("consideration", []))}
            res = _opensea_api(f"/orders/{OPENSEA_CHAIN[chain]}/seaport/listings", "POST",
                               {"parameters": params, "signature": sig, "protocol_address": proto})
            order = res.get("order", res)
            return {"ok": True, "listed": True, "nft": f"{to_checksum_address(contract)} #{token_id}",
                    "price_eth": str(price_eth), "onchain_steps": done,
                    "order_hash": order.get("order_hash") if isinstance(order, dict) else None}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def accept_offer(chain: str, order_hash: str, contract: str, token_id: str, protocol_address: str = SEAPORT_16, confirm: bool = False) -> dict:
        """Sell an NFT you own by accepting an existing OpenSea offer/bid. Get order_hash from the OpenSea offer tools. Approves the collection to OpenSea's conduit if needed, then fulfills the offer via OWS (you receive the offer amount, the buyer gets the NFT). Simulates before signing. Previews unless confirm=True. Verify the offer amount first — accepting a lowball offer sells your NFT cheap."""
        w = _agent_wallet()
        if not w.get("address"):
            return {"ok": False, "error": "No agent wallet set."}
        if chain not in OPENSEA_CHAIN:
            return {"ok": False, "error": f"OpenSea offers are not wired for {chain}."}
        try:
            owner = to_checksum_address(w["address"])
            nft_c = to_checksum_address(contract)
            fd = _opensea_api("/offers/fulfillment_data", "POST", {
                "offer": {"hash": order_hash, "chain": OPENSEA_CHAIN[chain], "protocol_address": to_checksum_address(protocol_address)},
                "fulfiller": {"address": owner},
                "consideration": {"asset_contract_address": nft_c, "token_id": str(token_id)},
            })
            tx = fd["fulfillment_data"]["transaction"]
            to = to_checksum_address(tx["to"])
            value = int(tx["value"])
            data = _encode_tx_calldata(tx["function"], tx["input_data"], tx.get("calldata_suffix", ""))
            approved = _is_approved_for_all(chain, nft_c, owner, OPENSEA_CONDUIT)
            sim = _simulate(chain, owner, to, value, data) if approved else None
            preview = {
                "action": "accept_offer", "chain": chain, "nft": f"{nft_c} #{token_id}",
                "offer_hash": order_hash, "needs_approval": not approved,
                "simulation": ("ok" if sim is None else f"would revert: {sim[:140]}") if approved else "runs after approval",
            }
            if not confirm:
                return {"ok": True, "preview": preview, "note": "Nothing sold. Verify the offer amount you are accepting, then confirm=true to approve (if needed) and accept."}
            done = []
            if not approved:
                appr = "0x" + _selector("setApprovalForAll(address,bool)").hex() + abi_encode(["address", "bool"], [OPENSEA_CONDUIT, True]).hex()
                u, _ = _build_unsigned_1559(chain, owner, nft_c, 0, appr)
                ath = _ows_send(chain, w["name"], u)
                _wait_receipt(chain, ath)
                done.append({"setApprovalForAll": ath})
            sim = _simulate(chain, owner, to, value, data)  # final gate, post-approval
            if sim is not None:
                return {"ok": False, "error": f"Simulation failed, not selling: {sim[:200]}", "onchain_steps": done}
            unsigned, _ = _build_unsigned_1559(chain, owner, to, value, data)
            txh = _ows_send(chain, w["name"], unsigned)
            done.append({"fulfill": txh})
            return {"ok": True, "sold": True, "nft": f"{nft_c} #{token_id}", "onchain_steps": done,
                    "tx": txh, "explorer": f"{CHAINS[chain]['explorer']}/tx/{txh}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run()
