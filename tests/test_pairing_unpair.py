"""Unit tests for revoking an approved pairing.

Run:
    python -m unittest tests.test_pairing_unpair
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp())
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pw")
os.environ.setdefault("BOOA_INSECURE_COOKIES", "1")
os.environ.setdefault("BOOA_LOGIN_THROTTLE_SECONDS", "0")

import yaml
from starlette.testclient import TestClient

import server


class PairingUnpairTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(server.HERMES_HOME)
        self.pairing = self.home / "pairing"
        self.pairing.mkdir(parents=True, exist_ok=True)
        (self.pairing / "telegram-approved.json").write_text(json.dumps({
            "111": {"user_name": "keeper"},
            "222": {"user_name": "leaver"},
        }))
        (self.home / ".env").write_text(
            "OPENROUTER_API_KEY=sk-test\nTELEGRAM_ALLOWED_USERS=111,222\n",
        )
        (self.home / "config.yaml").write_text(yaml.dump({
            "gateway": {"platforms": {"telegram": {"enabled": True, "allowed_users": "111,222"}}},
        }))
        self.client = TestClient(server.app)
        self.client.post("/login", data={"username": "admin", "password": os.environ["ADMIN_PASSWORD"]})

    def tearDown(self):
        for p in (self.pairing / "telegram-approved.json", self.home / ".env", self.home / "config.yaml"):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def _unpair(self, user_id, platform="telegram"):
        return self.client.post("/pairing/unpair", json={"platform": platform, "user_id": user_id})

    def test_requires_auth(self):
        res = TestClient(server.app).post("/pairing/unpair", json={"platform": "telegram", "user_id": "222"})
        self.assertEqual(res.status_code, 401)

    def test_unpair_scrubs_store_env_and_config(self):
        res = self._unpair("222")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["removed"])
        self.assertTrue(body["restart_needed"])

        approved = json.loads((self.pairing / "telegram-approved.json").read_text())
        self.assertEqual(list(approved), ["111"])
        self.assertIn("TELEGRAM_ALLOWED_USERS=111", (self.home / ".env").read_text())
        cfg = yaml.safe_load((self.home / "config.yaml").read_text())
        self.assertEqual(cfg["gateway"]["platforms"]["telegram"]["allowed_users"], "111")

    def test_last_user_removes_the_allowlist_entirely(self):
        self._unpair("222")
        res = self._unpair("111")
        self.assertEqual(res.status_code, 200, res.text)
        env_text = (self.home / ".env").read_text()
        self.assertNotIn("TELEGRAM_ALLOWED_USERS", env_text)
        self.assertIn("OPENROUTER_API_KEY=sk-test", env_text)
        cfg = yaml.safe_load((self.home / "config.yaml").read_text())
        self.assertNotIn("allowed_users", cfg["gateway"]["platforms"]["telegram"])

    def test_store_only_user_needs_no_restart(self):
        (self.pairing / "telegram-approved.json").write_text(json.dumps({"333": {"user_name": "x"}}))
        (self.home / ".env").write_text("OPENROUTER_API_KEY=sk-test\n")
        res = self._unpair("333")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["removed"])
        self.assertFalse(res.json()["restart_needed"])

    def test_unknown_user_404(self):
        self.assertEqual(self._unpair("999").status_code, 404)

    def test_garbage_input_rejected(self):
        self.assertEqual(self._unpair("2Ģ2", platform="telegram").status_code, 400)
        self.assertEqual(self._unpair("222", platform="../etc").status_code, 400)


if __name__ == "__main__":
    unittest.main()
