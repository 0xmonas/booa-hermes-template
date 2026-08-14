"""Unit tests for booa.console_auth.

Run:
    python -m unittest tests.test_console_auth
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest

from booa import console_auth


class FakeRequest:
    def __init__(self, headers=None, client_host="1.2.3.4"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": client_host})()


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def test_api_server_key_persists(self):
        k1 = console_auth.get_or_create_api_server_key(self.home)
        k2 = console_auth.get_or_create_api_server_key(self.home)
        self.assertEqual(k1, k2)
        self.assertGreaterEqual(len(k1), 16)

    def test_console_key_prefix_and_perms(self):
        key = console_auth.get_or_create_console_key(self.home)
        self.assertTrue(key.startswith("booa_ck_"))
        mode = stat.S_IMODE(os.stat(os.path.join(self.home, ".console-key")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_rotate_invalidates_old_key(self):
        old = console_auth.get_or_create_console_key(self.home)
        new = console_auth.rotate_console_key(self.home)
        self.assertNotEqual(old, new)
        req_old = FakeRequest({"authorization": f"Bearer {old}"})
        req_new = FakeRequest({"authorization": f"Bearer {new}"})
        self.assertFalse(console_auth.verify_console_key(self.home, req_old))
        self.assertTrue(console_auth.verify_console_key(self.home, req_new))

    def test_verify_rejects_garbage(self):
        console_auth.get_or_create_console_key(self.home)
        self.assertFalse(console_auth.verify_console_key(self.home, FakeRequest()))
        self.assertFalse(console_auth.verify_console_key(self.home, FakeRequest({"authorization": "Bearer "})))
        self.assertFalse(console_auth.verify_console_key(self.home, FakeRequest({"authorization": "Bearer nope"})))
        self.assertFalse(console_auth.verify_console_key(self.home, FakeRequest({"authorization": "Basic abc"})))


class EnabledStateTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def test_disabled_by_default(self):
        self.assertFalse(console_auth.console_enabled(self.home))

    def test_enable_disable_roundtrip(self):
        console_auth.set_console_enabled(self.home, True)
        self.assertTrue(console_auth.console_enabled(self.home))
        console_auth.set_console_enabled(self.home, False)
        self.assertFalse(console_auth.console_enabled(self.home))


class RateLimiterTests(unittest.TestCase):
    def test_blocks_after_max_failures(self):
        rl = console_auth.AuthRateLimiter(max_failures=3, window_seconds=60)
        self.assertFalse(rl.blocked("ip1"))
        for _ in range(3):
            rl.record_failure("ip1")
        self.assertTrue(rl.blocked("ip1"))
        self.assertFalse(rl.blocked("ip2"))

    def test_window_expiry(self):
        rl = console_auth.AuthRateLimiter(max_failures=1, window_seconds=0)
        rl.record_failure("ip1")
        self.assertFalse(rl.blocked("ip1"))

    def test_client_ip_uses_rightmost_hop(self):
        # The leftmost value is caller-supplied; keying limits on it lets an attacker
        # rotate it to dodge throttling or spoof the operator's IP to lock them out.
        req = FakeRequest({"x-forwarded-for": "9.9.9.9, 10.0.0.1"})
        self.assertEqual(console_auth.client_ip(req), "10.0.0.1")
        self.assertEqual(console_auth.client_ip(FakeRequest()), "1.2.3.4")


if __name__ == "__main__":
    unittest.main()
