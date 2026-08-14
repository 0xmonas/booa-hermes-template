"""Regression tests for server.py auth/route fixes.

Run:
    python -m unittest tests.test_server_fixes
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp())
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pw")
# The test client speaks http, so Secure cookies would never come back.
os.environ.setdefault("BOOA_INSECURE_COOKIES", "1")
os.environ.setdefault("BOOA_LOGIN_THROTTLE_SECONDS", "0")

from starlette.testclient import TestClient

import server
from booa.writer import TEMPLATE_VERSION


class ServerFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app, raise_server_exceptions=True)

    def test_health_minimal(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok"})

    def test_wallet_status_requires_auth(self):
        res = self.client.get("/api/wallet/status")
        self.assertEqual(res.status_code, 401)

    def test_gateway_status_requires_auth(self):
        res = self.client.get("/gateway/status")
        self.assertEqual(res.status_code, 401)

    def test_download_get_gone(self):
        res = self.client.get("/download")
        self.assertEqual(res.status_code, 405)

    def test_download_post_requires_auth(self):
        res = self.client.post("/download", data={"password": "x"})
        self.assertEqual(res.status_code, 401)

    def test_import_requires_auth(self):
        res = self.client.post("/import", data={"admin_password": "x"})
        self.assertEqual(res.status_code, 401)

    def test_console_config_requires_auth(self):
        self.assertEqual(self.client.get("/api/console/config").status_code, 401)
        self.assertEqual(self.client.post("/api/console/config", json={"action": "enable"}).status_code, 401)

    def test_console_disabled_by_default(self):
        res = self.client.get("/console/meta")
        self.assertEqual(res.status_code, 403)

    def test_login_page_hides_template_version(self):
        # Pre-auth pages must not fingerprint the exact template version for
        # anyone who finds the instance.
        res = self.client.get("/login")
        self.assertNotIn(f"booa-hermes-template v{TEMPLATE_VERSION}", res.text)

    def test_correct_password_is_never_locked_out(self):
        # A flood of wrong guesses must not stop the real operator getting in —
        # otherwise anyone can lock a holder out of their own agent.
        # Own client: logging in here must not authenticate the shared one.
        client = TestClient(server.app)
        server.auth_limiter._failures.clear()
        server.login_limiter._failures.clear()
        for i in range(40):
            client.post(
                "/login",
                data={"username": "admin", "password": f"wrong-{i}"},
                headers={"x-forwarded-for": f"10.0.0.{i}"},
            )
        res = client.post(
            "/login",
            data={"username": "admin", "password": os.environ["ADMIN_PASSWORD"]},
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 303, "correct credentials must still authenticate")
        server.auth_limiter._failures.clear()
        server.login_limiter._failures.clear()

    def test_session_dies_when_password_rotates(self):
        client = TestClient(server.app)
        client.post(
            "/login",
            data={"username": "admin", "password": os.environ["ADMIN_PASSWORD"]},
            follow_redirects=False,
        )
        self.assertEqual(client.get("/api/wallet/status").status_code, 200)
        original = server.ADMIN_PASSWORD
        try:
            server.ADMIN_PASSWORD = "a-new-password"
            self.assertEqual(client.get("/api/wallet/status").status_code, 401)
        finally:
            server.ADMIN_PASSWORD = original


if __name__ == "__main__":
    unittest.main()
