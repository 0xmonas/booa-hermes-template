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

import json
import os
import subprocess
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
    return os.environ.get("BOOA_ONCHAIN_WRITES", "").lower() in ("1", "true", "yes")


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
# the loop, so these are the real backstop for autonomous actions. All are
# opt-in via env; the OWS policy remains the cryptographic gate underneath.
#   BOOA_SEND_ALLOWLIST  destinations writes may target (wallets/contracts)
#   BOOA_MAX_TX_ETH      per-transaction native cap
#   BOOA_DAILY_CAP_ETH   general rolling-day native cap (tracked in a ledger)
_SPEND_LEDGER = os.path.join(HERMES_HOME, "onchain-spend.json")


def _allowlist() -> set:
    return {a.strip().lower() for a in os.environ.get("BOOA_SEND_ALLOWLIST", "").split(",") if a.strip()}


def _cap(name: str) -> Decimal:
    try:
        return Decimal(os.environ.get(name, "0") or "0")
    except Exception:
        return Decimal(0)


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _spent_today() -> Decimal:
    try:
        d = json.load(open(_SPEND_LEDGER))
        return Decimal(str(d.get("spent", "0"))) if d.get("date") == _today() else Decimal(0)
    except Exception:
        return Decimal(0)


def _record_spend(native_eth: Decimal) -> None:
    if native_eth <= 0:
        return
    try:
        with open(_SPEND_LEDGER, "w") as f:
            json.dump({"date": _today(), "spent": str(_spent_today() + native_eth)}, f)
    except Exception:
        pass


def _guard_info(dest: str, native_eth: Decimal) -> dict:
    al, per, daily = _allowlist(), _cap("BOOA_MAX_TX_ETH"), _cap("BOOA_DAILY_CAP_ETH")
    info = {
        "allowlist_active": bool(al),
        "destination_allowed": (not al) or (to_checksum_address(dest).lower() in al),
        "native_moved_eth": str(native_eth),
        "per_tx_cap_eth": str(per) if per > 0 else None,
    }
    if daily > 0:
        info["daily_cap_eth"] = str(daily)
        info["daily_remaining_eth"] = str(daily - _spent_today())
    return info


def _guard(dest: str, native_eth: Decimal) -> None:
    al = _allowlist()
    if al and to_checksum_address(dest).lower() not in al:
        raise PermissionError(f"{to_checksum_address(dest)} is not in BOOA_SEND_ALLOWLIST. Add it to allow this destination.")
    per = _cap("BOOA_MAX_TX_ETH")
    if per > 0 and native_eth > per:
        raise PermissionError(f"Moves {native_eth} ETH, over the per-tx limit {per} (BOOA_MAX_TX_ETH).")
    daily = _cap("BOOA_DAILY_CAP_ETH")
    if daily > 0:
        remaining = daily - _spent_today()
        if native_eth > remaining:
            raise PermissionError(f"Moves {native_eth} ETH but only {remaining} ETH left in today's general limit ({daily} BOOA_DAILY_CAP_ETH).")


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
                cap = os.environ.get("BOOA_MAX_TX_ETH", "0") or "0"
                if Decimal(cap) > 0 and Decimal(amount) > Decimal(cap):
                    return {"ok": False, "error": f"Native amount {amount} exceeds BOOA_MAX_TX_ETH cap ({cap}). Raise the cap to proceed."}
            native_eth = Decimal(0) if token else Decimal(amount)
            unsigned, meta = _build_unsigned_1559(chain, w["address"], target, value, data)
            preview = {"action": "send", "chain": chain, "from": to_checksum_address(w["address"]), "summary": summary, **meta, "guardrails": _guard_info(to, native_eth)}
            if not confirm:
                return {"ok": True, "preview": preview, "note": "Nothing sent. Show this to the operator, then call again with confirm=true to broadcast."}
            _guard(to, native_eth)
            txh = _ows_send(chain, w["name"], unsigned)
            _record_spend(native_eth)
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
            _record_spend(native_eth)
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
            preview["guardrails"] = _guard_info(router, native_eth)
            if not confirm:
                return {"ok": True, "preview": preview, "note": "Nothing swapped. confirm=true approves the exact amount (if needed) and executes."}
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
            _record_spend(native_eth)
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


if __name__ == "__main__":
    mcp.run()
