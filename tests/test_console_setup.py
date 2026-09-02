"""Unit tests for booa.app-driven setup: /console/bootstrap and /console/setup.

Run:
    python -m unittest tests.test_console_setup
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp())
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pw")
os.environ.setdefault("BOOA_INSECURE_COOKIES", "1")
os.environ.setdefault("BOOA_LOGIN_THROTTLE_SECONDS", "0")

import yaml
from starlette.testclient import TestClient

import server
from booa import console_auth

PW = os.environ["ADMIN_PASSWORD"]

IDENTITY = {
    "token_id": 1496, "name": "Test BOOA", "creature": "a test creature", "vibe": "calm",
    "emoji": "*", "soul_md": "# SOUL\n", "identity_md": "# IDENTITY\n", "avatar_svg": "<svg/>",
}


async def _fake_identity(token_id):
    if token_id == 404:
        raise server.TokenNotFound()
    return dict(IDENTITY, token_id=token_id)


async def _fake_skills():
    return {}


async def _fake_start():
    return True


class ConsoleSetupTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(server.HERMES_HOME)
        self.client = TestClient(server.app)
        server.auth_limiter._failures.clear()
        server.login_limiter._failures.clear()
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        console_auth.set_console_enabled(server.HERMES_HOME, False)
        for name in (".setup-complete", "config.yaml", ".env", "SOUL.md"):
            try:
                (self.home / name).unlink()
            except FileNotFoundError:
                pass
        server.wizard_data.clear()

    def _bootstrap(self, pw=PW):
        return self.client.post("/console/bootstrap", json={"admin_password": pw})

    def _setup(self, **overrides):
        body = {"admin_password": PW, "token_id": 1496, "api_key": "sk-or-test-key-1234",
                "model": "anthropic/claude-sonnet-5", "owner_name": "Mert", "language": "Turkish"}
        body.update(overrides)
        with mock.patch.object(server, "fetch_booa_identity", _fake_identity), \
             mock.patch.object(server, "fetch_skills", _fake_skills), \
             mock.patch.object(server.gateway, "start", _fake_start), \
             mock.patch.object(server.wallet_status, "refresh", lambda *a, **k: None):
            return self.client.post("/console/setup", json=body)

    # bootstrap

    def test_bootstrap_rejects_missing_or_wrong_password(self):
        self.assertEqual(self.client.post("/console/bootstrap", json={}).status_code, 403)
        self.assertEqual(self._bootstrap("nope").status_code, 403)
        self.assertFalse(console_auth.console_enabled(server.HERMES_HOME))

    def test_bootstrap_enables_console_and_returns_key(self):
        res = self._bootstrap()
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["console_key"].startswith("booa_ck_"))
        self.assertFalse(body["setup_complete"])
        self.assertTrue(console_auth.console_enabled(server.HERMES_HOME))
        self.assertEqual(body["console_key"], console_auth.get_or_create_console_key(server.HERMES_HOME))

    def test_bootstrap_works_before_the_console_is_enabled(self):
        # The regular console routes 403 while disabled; bootstrap must not.
        self.assertEqual(self.client.get("/console/meta").status_code, 403)
        self.assertEqual(self._bootstrap().status_code, 200)

    def test_oversized_body_rejected(self):
        res = self.client.post("/console/bootstrap", content=b"x" * (server._SETUP_MAX_BODY + 1),
                               headers={"Content-Type": "application/json"})
        self.assertEqual(res.status_code, 400)

    # setup

    def test_setup_requires_password(self):
        self.assertEqual(self._setup(admin_password="nope").status_code, 403)
        self.assertFalse((self.home / ".setup-complete").exists())

    def test_setup_validates_inputs(self):
        self.assertEqual(self._setup(token_id="abc").status_code, 400)
        self.assertEqual(self._setup(api_key="").status_code, 400)
        self.assertEqual(self._setup(model="bad model id").status_code, 400)
        self.assertEqual(self._setup(telegram_token="not-a-token").status_code, 400)
        self.assertFalse((self.home / ".setup-complete").exists())

    def test_unknown_token_404(self):
        self.assertEqual(self._setup(token_id=404).status_code, 404)

    def test_setup_runs_the_whole_wizard(self):
        res = self._setup()
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["setup_complete"])
        self.assertTrue(body["console_key"].startswith("booa_ck_"))
        self.assertEqual(body["agent_name"], "Test BOOA")
        self.assertTrue((self.home / ".setup-complete").exists())
        cfg = yaml.safe_load((self.home / "config.yaml").read_text())
        self.assertEqual(cfg["model"]["default"], "anthropic/claude-sonnet-5")
        self.assertIn("OPENROUTER_API_KEY=sk-or-test-key-1234", (self.home / ".env").read_text())
        self.assertNotIn("TELEGRAM_BOT_TOKEN", (self.home / ".env").read_text())
        self.assertTrue(console_auth.console_enabled(server.HERMES_HOME))

    def test_second_setup_is_refused(self):
        self.assertEqual(self._setup().status_code, 200)
        res = self._setup()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["error"], "already_set_up")

    def test_console_models_needs_console_key(self):
        self.assertEqual(self.client.get("/console/models").status_code, 403)
        key = self._bootstrap().json()["console_key"]
        with mock.patch.object(server, "_models_cache", {"ts": 9e12, "data": [{"id": "a/b"}]}):
            res = self.client.get("/console/models", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["models"][0]["id"], "a/b")


if __name__ == "__main__":
    unittest.main()
