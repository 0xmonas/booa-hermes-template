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

    def test_login_page_shows_template_version(self):
        res = self.client.get("/login")
        self.assertIn(f"booa-hermes-template v{TEMPLATE_VERSION}", res.text)
        self.assertNotIn("v1.0.0", res.text)


if __name__ == "__main__":
    unittest.main()
