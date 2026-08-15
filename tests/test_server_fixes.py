"""Regression tests for server.py auth/route fixes.

Run:
    python -m unittest tests.test_server_fixes
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp())
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pw")
# The test client speaks http, so Secure cookies would never come back.
os.environ.setdefault("BOOA_INSECURE_COOKIES", "1")
os.environ.setdefault("BOOA_LOGIN_THROTTLE_SECONDS", "0")

from starlette.testclient import TestClient

import server
from booa import console_auth
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

    def test_importing_the_server_never_reexecs(self):
        # The scrub re-execs the process. If that fired on import, anything importing
        # this module (a test run, a script) would silently become a running server.
        self.assertIsNone(server._reexec_without_admin_password())

    def test_env_scrub_flag_does_not_break_an_importing_process(self):
        # Turning the hardening flag on must not change anything for a process that
        # imports the module rather than running it — otherwise enabling it would
        # break tooling and tests in ways an operator would not expect.
        import subprocess, tempfile as tf, textwrap
        repo = os.path.dirname(os.path.abspath(server.__file__))
        probe = textwrap.dedent(f"""
            import os, sys, json
            sys.path.insert(0, {repo!r})
            import server
            print(json.dumps({{
                "password_usable": server.ADMIN_PASSWORD == "probe-pw",
                "no_reexec": server._reexec_without_admin_password() is None,
            }}))
        """)
        with tf.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(probe)
            path = f.name
        env = dict(os.environ, ADMIN_PASSWORD="probe-pw", BOOA_SCRUB_ENV="1",
                   HERMES_HOME=tf.mkdtemp())
        try:
            out = subprocess.run([sys.executable, path], env=env, capture_output=True,
                                 text=True, timeout=60)
            self.assertTrue(out.stdout.strip(), f"probe produced no output: {out.stderr[-400:]}")
            data = json.loads(out.stdout.strip().splitlines()[-1])
        finally:
            os.unlink(path)
        self.assertTrue(data["password_usable"])
        self.assertTrue(data["no_reexec"])

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


class ConsoleCommandsTests(unittest.TestCase):
    def setUp(self):
        home = os.environ["HERMES_HOME"]
        console_auth.set_console_enabled(home, True)
        self.auth = {"Authorization": f"Bearer {console_auth.get_or_create_console_key(home)}"}
        self.client = TestClient(server.app)

    def tearDown(self):
        console_auth.set_console_enabled(os.environ["HERMES_HOME"], False)
        for mod in ("hermes_cli.commands", "hermes_cli"):
            sys.modules.pop(mod, None)

    def test_requires_console_key(self):
        self.assertEqual(self.client.get("/console/commands").status_code, 401)

    def test_501_without_the_registry(self):
        res = self.client.get("/console/commands", headers=self.auth)
        self.assertEqual(res.status_code, 501)

    def test_manifest_mirrors_the_telegram_menu(self):
        import types

        pkg = types.ModuleType("hermes_cli")
        mod = types.ModuleType("hermes_cli.commands")
        mod.telegram_menu_commands = lambda max_commands=100: (
            [("help", "Show available commands"), ("ows_pitfalls", "Known pitfalls")], 2,
        )
        pkg.commands = mod
        sys.modules["hermes_cli"] = pkg
        sys.modules["hermes_cli.commands"] = mod
        res = self.client.get("/console/commands", headers=self.auth)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["hidden"], 2)
        self.assertEqual(body["commands"][0], {"command": "/help", "description": "Show available commands"})
        self.assertEqual(body["commands"][1]["command"], "/ows_pitfalls")


if __name__ == "__main__":
    unittest.main()
