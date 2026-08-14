"""Unit tests for booa.console_proxy.

Run:
    python -m unittest tests.test_console_proxy
"""

from __future__ import annotations

import http.server
import json
import tempfile
import threading
import unittest

from starlette.testclient import TestClient

from booa import console_auth, console_proxy
from booa.console_proxy import build_console_app


class EchoHandler(http.server.BaseHTTPRequestHandler):
    def _echo(self):
        body = {
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "cookie": self.headers.get("Cookie", ""),
        }
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "leak=1")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _echo
    do_POST = _echo
    do_DELETE = _echo

    def log_message(self, *args):
        pass


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        console_proxy_upstream = f"http://127.0.0.1:{cls.upstream.server_address[1]}"
        cls._orig_upstream = console_proxy.UPSTREAM
        console_proxy.UPSTREAM = console_proxy_upstream

    @classmethod
    def tearDownClass(cls):
        console_proxy.UPSTREAM = cls._orig_upstream
        cls.upstream.shutdown()

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.limiter = console_auth.AuthRateLimiter(max_failures=1000)
        self.client = TestClient(build_console_app(self.home, self.limiter))
        console_auth.set_console_enabled(self.home, True)
        self.key = console_auth.get_or_create_console_key(self.home)
        self.auth = {"Authorization": f"Bearer {self.key}"}

    def test_disabled_returns_403_before_auth(self):
        console_auth.set_console_enabled(self.home, False)
        res = self.client.get("/v1/models", headers=self.auth)
        self.assertEqual(res.status_code, 403)

    def test_missing_key_401(self):
        res = self.client.get("/v1/models")
        self.assertEqual(res.status_code, 401)

    def test_wrong_key_401(self):
        res = self.client.get("/v1/models", headers={"Authorization": "Bearer nope"})
        self.assertEqual(res.status_code, 401)

    def test_allowlisted_path_forwards_with_internal_bearer(self):
        res = self.client.get("/v1/models", headers={**self.auth, "Cookie": "session=abc"})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        internal = console_auth.get_or_create_api_server_key(self.home)
        self.assertEqual(body["path"], "/v1/models")
        self.assertEqual(body["authorization"], f"Bearer {internal}")
        self.assertEqual(body["cookie"], "")
        self.assertNotIn("set-cookie", {k.lower() for k in res.headers})

    def test_query_string_forwarded(self):
        res = self.client.get("/api/sessions?limit=5&source=api_server", headers=self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["path"], "/api/sessions?limit=5&source=api_server")

    def test_excluded_paths_404(self):
        for path in ["/v1/chat/completions", "/v1/responses", "/api/cron/fire",
                     "/p/default/v1/models", "/health/detailed"]:
            res = self.client.post(path, headers=self.auth) if path != "/health/detailed" else self.client.get(path, headers=self.auth)
            self.assertEqual(res.status_code, 404, path)

    def test_job_management_proxied_but_creation_is_not(self):
        # Managing existing jobs is allowed; creating/editing recurring autonomous
        # work is not — that boundary is the point, so pin it.
        self.assertEqual(self.client.get("/api/jobs", headers=self.auth).status_code, 200)
        self.assertEqual(self.client.post("/api/jobs/j1/pause", headers=self.auth).status_code, 200)
        self.assertEqual(self.client.post("/api/jobs/j1/resume", headers=self.auth).status_code, 200)
        self.assertEqual(self.client.post("/api/jobs/j1/run", headers=self.auth).status_code, 200)
        self.assertEqual(self.client.delete("/api/jobs/j1", headers=self.auth).status_code, 200)
        self.assertEqual(self.client.post("/api/jobs", headers=self.auth).status_code, 405)
        self.assertEqual(self.client.patch("/api/jobs/j1", headers=self.auth).status_code, 405)

    def test_path_param_validation(self):
        res = self.client.get("/api/sessions/ok_session-1/messages", headers=self.auth)
        self.assertEqual(res.status_code, 200)
        res = self.client.get("/api/sessions/bad%2Fslash/messages", headers=self.auth)
        self.assertIn(res.status_code, (400, 404))

    def test_cors_preflight_pinned_origin(self):
        res = self.client.options("/api/sessions", headers={
            "Origin": "https://booa.app",
            "Access-Control-Request-Method": "POST",
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("access-control-allow-origin"), "https://booa.app")

    def test_cors_preflight_www_origin(self):
        # www is canonical (the apex 308-redirects to it), so this is the Origin
        # browsers actually send — missing it blocked every real console connect.
        for path in ("/console/meta", "/api/sessions", "/v1/models"):
            res = self.client.options(path, headers={
                "Origin": "https://www.booa.app",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            })
            self.assertEqual(res.status_code, 200, path)
            self.assertEqual(res.headers.get("access-control-allow-origin"), "https://www.booa.app", path)

    def test_cors_preflight_evil_origin(self):
        res = self.client.options("/api/sessions", headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        })
        self.assertNotEqual(res.headers.get("access-control-allow-origin"), "https://evil.example")


if __name__ == "__main__":
    unittest.main()
