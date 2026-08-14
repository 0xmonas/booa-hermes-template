"""Unit tests for the onchain guardrails in booa.onchain_mcp.

These gates are what stands between an agent that reads untrusted content and a
funded wallet, so each one is pinned against the exact bypass it exists to stop.

Run:
    python -m unittest tests.test_onchain_guards
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from decimal import Decimal

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp())

# The MCP SDK isn't needed to exercise the guards; stub the decorator surface.
if "mcp.server.fastmcp" not in sys.modules:
    fastmcp = types.ModuleType("mcp.server.fastmcp")

    class _FastMCP:
        def __init__(self, *a, **k): pass
        def tool(self, *a, **k):
            def deco(fn): return fn
            return deco
        def run(self, *a, **k): pass

    fastmcp.FastMCP = _FastMCP
    server_mod = sys.modules.setdefault("mcp.server", types.ModuleType("mcp.server"))
    mcp_mod = sys.modules.setdefault("mcp", types.ModuleType("mcp"))
    mcp_mod.server = server_mod
    server_mod.fastmcp = fastmcp
    sys.modules["mcp.server.fastmcp"] = fastmcp

from booa import onchain_mcp as m

ALLOWED = "0x1111111111111111111111111111111111111111"
ATTACKER = "0x2222222222222222222222222222222222222222"


class TypedDataGuardTests(unittest.TestCase):
    def setUp(self):
        os.environ["BOOA_SEND_ALLOWLIST"] = ALLOWED

    def tearDown(self):
        os.environ.pop("BOOA_SEND_ALLOWLIST", None)

    def check(self, payload):
        m._check_typed_data_safety(json.dumps(payload))

    def test_unlimited_permit_to_attacker_refused(self):
        with self.assertRaises(PermissionError):
            self.check({"primaryType": "Permit",
                        "message": {"spender": ATTACKER, "value": str(2 ** 256 - 1)}})

    def test_permit_to_unlisted_spender_refused(self):
        with self.assertRaises(PermissionError):
            self.check({"primaryType": "Permit", "message": {"spender": ATTACKER, "value": "1000"}})

    def test_unlimited_permit_refused_even_when_allowlisted(self):
        with self.assertRaises(PermissionError):
            self.check({"primaryType": "Permit",
                        "message": {"spender": ALLOWED, "value": str(2 ** 256 - 1)}})

    def test_permit2_details_shape_refused(self):
        with self.assertRaises(PermissionError):
            self.check({"primaryType": "PermitSingle",
                        "message": {"details": {"spender": ATTACKER, "amount": str(2 ** 255)}}})

    def test_reasonable_permit_to_allowlisted_spender_passes(self):
        self.check({"primaryType": "Permit", "message": {"spender": ALLOWED, "value": "1000"}})

    def test_marketplace_order_refused(self):
        with self.assertRaises(PermissionError):
            self.check({"primaryType": "OrderComponents", "message": {"offerer": ATTACKER}})

    def test_login_payload_still_signable(self):
        self.check({"primaryType": "SIWA", "message": {"statement": "sign in"}})

    def test_malformed_json_refused(self):
        with self.assertRaises(PermissionError):
            m._check_typed_data_safety("not json")


class SpendLedgerTests(unittest.TestCase):
    """The daily cap is only a limit if concurrent callers cannot both pass it."""

    def setUp(self):
        os.environ["BOOA_DAILY_CAP_ETH"] = "1.0"
        for p in (m._SPEND_LEDGER, m._SPEND_LOCK):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    def tearDown(self):
        os.environ.pop("BOOA_DAILY_CAP_ETH", None)
        os.environ.pop("BOOA_MAX_TX_ETH", None)

    def test_concurrent_reservations_cannot_both_pass(self):
        import threading
        results = []
        barrier = threading.Barrier(2)

        def reserve():
            barrier.wait()
            try:
                m._record_spend(Decimal("0.6"))
                results.append("ok")
            except PermissionError:
                results.append("blocked")

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["blocked", "ok"], "0.6 + 0.6 must not both clear a 1.0 cap")
        self.assertEqual(m._spent_today(), Decimal("0.6"))

    def test_window_is_rolling_not_calendar_day(self):
        # An entry just under 24h old still counts; the old code reset at UTC midnight,
        # so the full cap was spendable at 23:59 and again at 00:01.
        now = time.time()
        m._atomic_write_json(m._SPEND_LEDGER, {"entries": [{"ts": now - 60, "amount": "0.9"}]})
        self.assertEqual(m._spent_today(), Decimal("0.9"))
        with self.assertRaises(PermissionError):
            m._record_spend(Decimal("0.5"))

    def test_entries_older_than_the_window_drop_out(self):
        now = time.time()
        m._atomic_write_json(m._SPEND_LEDGER, {"entries": [{"ts": now - (25 * 3600), "amount": "0.9"}]})
        self.assertEqual(m._spent_today(), Decimal(0))
        m._record_spend(Decimal("0.5"))

    def test_unwritable_ledger_blocks_the_spend(self):
        # A read-only or full volume used to silently reset the ledger to zero,
        # which quietly removed the cap.
        original = m._atomic_write_json
        m._atomic_write_json = lambda *a, **k: (_ for _ in ()).throw(OSError("read-only volume"))
        try:
            with self.assertRaises(PermissionError):
                m._record_spend(Decimal("0.1"))
        finally:
            m._atomic_write_json = original

    def test_no_daily_cap_configured_is_a_no_op(self):
        os.environ["BOOA_DAILY_CAP_ETH"] = ""
        m._record_spend(Decimal("100"))
        self.assertEqual(m._spent_today(), Decimal(0))


class TokenTransferGuardTests(unittest.TestCase):
    """ETH caps cannot bound an unpriced token amount, so an empty allowlist must not
    silently mean 'unlimited USDC' when the operator has set caps."""

    def tearDown(self):
        for k in ("BOOA_SEND_ALLOWLIST", "BOOA_MAX_TX_ETH", "BOOA_DAILY_CAP_ETH"):
            os.environ.pop(k, None)

    def test_token_move_refused_when_caps_set_but_no_allowlist(self):
        os.environ["BOOA_MAX_TX_ETH"] = "0.01"
        with self.assertRaises(PermissionError):
            m._guard(ATTACKER, Decimal(0), non_native=True)

    def test_token_move_allowed_to_allowlisted_destination(self):
        os.environ["BOOA_MAX_TX_ETH"] = "0.01"
        os.environ["BOOA_SEND_ALLOWLIST"] = ALLOWED
        m._guard(ALLOWED, Decimal(0), non_native=True)

    def test_native_send_unaffected(self):
        os.environ["BOOA_MAX_TX_ETH"] = "0.01"
        m._guard(ATTACKER, Decimal("0.005"))

    def test_fully_opt_out_config_still_permissive(self):
        m._guard(ATTACKER, Decimal(0), non_native=True)


class CapParsingTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("BOOA_MAX_TX_ETH", None)

    def test_unparseable_cap_blocks_instead_of_meaning_unlimited(self):
        os.environ["BOOA_MAX_TX_ETH"] = "0.1 ETH"
        with self.assertRaises(PermissionError):
            m._cap("BOOA_MAX_TX_ETH")

    def test_negative_cap_blocks(self):
        os.environ["BOOA_MAX_TX_ETH"] = "-5"
        with self.assertRaises(PermissionError):
            m._cap("BOOA_MAX_TX_ETH")

    def test_unset_cap_means_no_limit(self):
        os.environ["BOOA_MAX_TX_ETH"] = ""
        self.assertEqual(m._cap("BOOA_MAX_TX_ETH"), Decimal(0))

    def test_valid_cap_parses(self):
        os.environ["BOOA_MAX_TX_ETH"] = "0.05"
        self.assertEqual(m._cap("BOOA_MAX_TX_ETH"), Decimal("0.05"))


if __name__ == "__main__":
    unittest.main()
