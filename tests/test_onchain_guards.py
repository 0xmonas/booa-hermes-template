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
