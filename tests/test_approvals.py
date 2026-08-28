"""Unit tests for booa.approvals — the operator-approval gate on trade tools.

Run:
    python -m unittest tests.test_approvals
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp())

from eth_account import Account
from eth_account.messages import encode_typed_data

from booa import approvals


ACTION = {"tool": "opensea_list", "chain": "ethereum",
          "contract": "0x" + "ab" * 20, "token_id": "42", "price_eth": "0.5"}


def _fresh_home():
    home = tempfile.mkdtemp()
    os.makedirs(os.path.join(home, "context"), exist_ok=True)
    with open(os.path.join(home, "context", "agent.json"), "w") as f:
        json.dump({"token_id": 1496}, f)
    return home


def _sign(record, key):
    typed = approvals.build_typed_data(
        record["token_id"], record["chain_id"], record["action_hash"],
        record["nonce"], record["deadline"],
    )
    return Account.sign_message(encode_typed_data(full_message=typed), key).signature.hex()


class ApprovalsTests(unittest.TestCase):
    def setUp(self):
        self.home = _fresh_home()
        self.acct = Account.create()
        patches = [
            mock.patch.object(approvals, "HERMES_HOME", self.home),
            mock.patch.object(approvals, "_STORE", os.path.join(self.home, ".approvals.json")),
            mock.patch.object(approvals, "_LOCK", os.path.join(self.home, ".approvals.lock")),
            mock.patch.object(approvals, "resolve_controller", lambda: self.acct.address.lower()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("BOOA_REQUIRE_APPROVAL", None)

    def _pending(self):
        res = approvals.gate(ACTION)
        self.assertTrue(res["approval_required"])
        return next(r for r in approvals.list_records() if r["id"] == res["approval_id"])

    def test_required_by_default(self):
        self.assertTrue(approvals.approvals_required())
        os.environ["BOOA_REQUIRE_APPROVAL"] = "0"
        try:
            self.assertFalse(approvals.approvals_required())
            self.assertIsNone(approvals.gate(ACTION))
        finally:
            os.environ.pop("BOOA_REQUIRE_APPROVAL")

    def test_gate_creates_then_reports_pending(self):
        first = approvals.gate(ACTION)
        self.assertEqual(first["status"], "created")
        second = approvals.gate(ACTION)
        self.assertEqual(second["status"], "pending")
        self.assertEqual(second["approval_id"], first["approval_id"])

    def test_canonical_hash_is_stable_and_order_free(self):
        reordered = dict(reversed(list(ACTION.items())))
        self.assertEqual(approvals.action_hash(ACTION), approvals.action_hash(reordered))
        self.assertNotEqual(approvals.action_hash(ACTION), approvals.action_hash({**ACTION, "price_eth": "5"}))

    def test_controller_signature_approves_and_consumes_once(self):
        rec = self._pending()
        res = approvals.approve(rec["id"], _sign(rec, self.acct.key))
        self.assertTrue(res["ok"], res)
        self.assertIsNone(approvals.gate(ACTION))          # consumed → proceed
        again = approvals.gate(ACTION)                     # one-shot: next call re-parks
        self.assertTrue(again["approval_required"])

    def test_wrong_wallet_is_refused(self):
        rec = self._pending()
        res = approvals.approve(rec["id"], _sign(rec, Account.create().key))
        self.assertFalse(res["ok"])
        self.assertIn("controller", res["error"])

    def test_signature_over_different_action_is_refused(self):
        rec = self._pending()
        tampered = {**rec, "action_hash": approvals.action_hash({**ACTION, "price_eth": "0.01"})}
        res = approvals.approve(rec["id"], _sign(tampered, self.acct.key))
        self.assertFalse(res["ok"])

    def test_expired_approval_cannot_be_signed(self):
        rec = self._pending()
        with mock.patch("time.time", return_value=rec["deadline"] + 1):
            res = approvals.approve(rec["id"], _sign(rec, self.acct.key))
        self.assertFalse(res["ok"])
        self.assertIn("expired", res["error"].lower())


if __name__ == "__main__":
    unittest.main()
