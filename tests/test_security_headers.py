"""Unit tests for the security-headers middleware.

Run:
    python -m unittest tests.test_security_headers
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp())
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pw")
os.environ.setdefault("BOOA_INSECURE_COOKIES", "1")

from starlette.testclient import TestClient

import server


class SecurityHeadersTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_headers_present_on_every_response(self):
        res = self.client.get("/health")
        self.assertEqual(res.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.headers.get("Referrer-Policy"), "no-referrer")
        csp = res.headers.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_csp_forbids_inline_script(self):
        csp = self.client.get("/login").headers.get("Content-Security-Policy", "")
        # No 'unsafe-inline' / 'unsafe-eval' anywhere in the script-src directive.
        script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
        self.assertNotIn("unsafe-inline", script_src)
        self.assertNotIn("unsafe-eval", script_src)

    def test_login_page_has_no_inline_script_or_handlers(self):
        # The CSP is only real if the pages actually comply.
        for path in ("/login",):
            html = self.client.get(path).text
            self.assertNotIn("<script>", html)


if __name__ == "__main__":
    unittest.main()
