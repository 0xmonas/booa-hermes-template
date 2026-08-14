"""Tests for managing onchain guardrails from the web console.

The console key is deliberately weaker than the admin password. If it could widen
spending limits it would become spend authority, so only tightening is free —
that asymmetry is what these tests pin.

Run:
    python -m unittest tests.test_onchain_settings_api
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp())
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pw")
os.environ.setdefault("BOOA_INSECURE_COOKIES", "1")
os.environ.setdefault("BOOA_LOGIN_THROTTLE_SECONDS", "0")

from starlette.testclient import TestClient

import server
from booa import console_auth

TIGHT = {
    "BOOA_ONCHAIN_WRITES": "1",
    "BOOA_MAX_TX_ETH": "0.05",
    "BOOA_DAILY_CAP_ETH": "0.2",
    "BOOA_SEND_ALLOWLIST": "0x1111111111111111111111111111111111111111",
    "BOOA_MAX_SLIPPAGE_BPS": "300",
    "BOOA_OPENSEA_REQUIRE_VERIFIED": "1",
}


class ClassificationTests(unittest.TestCase):
    def loosens(self, change, current=None):
        return server.onchain_change_loosens(dict(current or TIGHT), change)

    def test_raising_a_cap_loosens(self):
        self.assertTrue(self.loosens({"BOOA_MAX_TX_ETH": "5"}))

    def test_lowering_a_cap_tightens(self):
        self.assertFalse(self.loosens({"BOOA_MAX_TX_ETH": "0.01"}))

    def test_clearing_a_cap_loosens_because_empty_means_unlimited(self):
        self.assertTrue(self.loosens({"BOOA_DAILY_CAP_ETH": ""}))

    def test_setting_a_cap_where_there_was_none_tightens(self):
        self.assertFalse(self.loosens({"BOOA_MAX_TX_ETH": "0.01"}, current={"BOOA_MAX_TX_ETH": ""}))

    def test_adding_an_allowlist_entry_loosens(self):
        self.assertTrue(self.loosens({
            "BOOA_SEND_ALLOWLIST": TIGHT["BOOA_SEND_ALLOWLIST"] + ",0x2222222222222222222222222222222222222222"}))

    def test_removing_an_allowlist_entry_tightens(self):
        self.assertFalse(self.loosens({"BOOA_SEND_ALLOWLIST": TIGHT["BOOA_SEND_ALLOWLIST"]},
                                      current={"BOOA_SEND_ALLOWLIST": TIGHT["BOOA_SEND_ALLOWLIST"] + ",0x2222222222222222222222222222222222222222"}))

    def test_emptying_the_allowlist_loosens(self):
        self.assertTrue(self.loosens({"BOOA_SEND_ALLOWLIST": ""}))

    def test_enabling_writes_loosens(self):
        self.assertTrue(self.loosens({"BOOA_ONCHAIN_WRITES": "1"}, current={"BOOA_ONCHAIN_WRITES": "0"}))

    def test_disabling_writes_tightens(self):
        self.assertFalse(self.loosens({"BOOA_ONCHAIN_WRITES": "0"}))

    def test_dropping_verified_only_loosens(self):
        self.assertTrue(self.loosens({"BOOA_OPENSEA_REQUIRE_VERIFIED": "0"}))

    def test_raising_slippage_loosens(self):
        self.assertTrue(self.loosens({"BOOA_MAX_SLIPPAGE_BPS": "900"}))


class ValidationTests(unittest.TestCase):
    def test_cap_with_units_rejected(self):
        self.assertIsNotNone(server.validate_onchain_settings({"BOOA_MAX_TX_ETH": "0.1 ETH"}))

    def test_negative_cap_rejected(self):
        self.assertIsNotNone(server.validate_onchain_settings({"BOOA_DAILY_CAP_ETH": "-1"}))

    def test_bad_address_rejected(self):
        self.assertIsNotNone(server.validate_onchain_settings({"BOOA_SEND_ALLOWLIST": "not-an-address"}))

    def test_slippage_out_of_range_rejected(self):
        self.assertIsNotNone(server.validate_onchain_settings({"BOOA_MAX_SLIPPAGE_BPS": "20000"}))

    def test_valid_settings_pass(self):
        self.assertIsNone(server.validate_onchain_settings(TIGHT))


class ConsoleEndpointTests(unittest.TestCase):
    def setUp(self):
        home = os.environ["HERMES_HOME"]
        console_auth.set_console_enabled(home, True)
        self.key = console_auth.get_or_create_console_key(home)
        self.auth = {"Authorization": f"Bearer {self.key}"}
        self.client = TestClient(server.app)
        with open(server._ONCHAIN_PATH, "w") as f:
            json.dump(dict(TIGHT), f)

    def tearDown(self):
        # Other modules share this HERMES_HOME and assert the console is off by
        # default, so leave it as we found it.
        console_auth.set_console_enabled(os.environ["HERMES_HOME"], False)
        try:
            os.unlink(server._ONCHAIN_PATH)
        except FileNotFoundError:
            pass

    def post(self, body):
        return self.client.post("/console/onchain-settings", json=body, headers=self.auth)

    def test_reading_requires_the_console_key(self):
        self.assertEqual(self.client.get("/console/onchain-settings").status_code, 401)
        self.assertEqual(self.client.get("/console/onchain-settings", headers=self.auth).status_code, 200)

    def test_console_key_alone_can_pull_the_brake(self):
        res = self.post({"BOOA_ONCHAIN_WRITES": "0"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["settings"]["BOOA_ONCHAIN_WRITES"], "0")

    def test_console_key_alone_cannot_raise_a_cap(self):
        res = self.post({"BOOA_MAX_TX_ETH": "10"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"], "admin_password_required")

    def test_console_key_alone_cannot_add_a_destination(self):
        res = self.post({"BOOA_SEND_ALLOWLIST": TIGHT["BOOA_SEND_ALLOWLIST"] + ",0x2222222222222222222222222222222222222222"})
        self.assertEqual(res.status_code, 403)

    def test_admin_password_authorizes_a_widening_change(self):
        res = self.post({"BOOA_MAX_TX_ETH": "10", "admin_password": os.environ["ADMIN_PASSWORD"]})
        self.assertEqual(res.status_code, 200, res.text)

    def test_wrong_admin_password_refused(self):
        res = self.post({"BOOA_MAX_TX_ETH": "10", "admin_password": "nope"})
        self.assertEqual(res.status_code, 403)

    def test_invalid_value_rejected_before_anything_is_written(self):
        res = self.post({"BOOA_MAX_TX_ETH": "0.1 ETH", "admin_password": os.environ["ADMIN_PASSWORD"]})
        self.assertEqual(res.status_code, 400)
        with open(server._ONCHAIN_PATH) as f:
            self.assertEqual(json.load(f)["BOOA_MAX_TX_ETH"], "0.05")

    def test_partial_save_does_not_blank_other_settings(self):
        self.post({"BOOA_ONCHAIN_WRITES": "0"})
        with open(server._ONCHAIN_PATH) as f:
            saved = json.load(f)
        self.assertEqual(saved["BOOA_SEND_ALLOWLIST"], TIGHT["BOOA_SEND_ALLOWLIST"])
        self.assertEqual(saved["BOOA_DAILY_CAP_ETH"], "0.2")


if __name__ == "__main__":
    unittest.main()
