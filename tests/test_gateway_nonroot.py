"""Unit tests for the non-root gateway user split.

Run:
    python -m unittest tests.test_gateway_nonroot
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from booa import gateway


class RuntimeUserTests(unittest.TestCase):
    def test_disabled_by_flag(self):
        with mock.patch.dict(os.environ, {"BOOA_GATEWAY_NONROOT": "0"}):
            self.assertIsNone(gateway.runtime_user())

    def test_none_when_not_root(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("os.geteuid", return_value=501):
            os.environ.pop("BOOA_GATEWAY_NONROOT", None)
            self.assertIsNone(gateway.runtime_user())

    def test_resolves_agent_when_root(self):
        rec = mock.Mock(pw_uid=1000, pw_gid=1000)
        with mock.patch("os.geteuid", return_value=0), \
             mock.patch("pwd.getpwnam", return_value=rec):
            os.environ.pop("BOOA_GATEWAY_NONROOT", None)
            self.assertEqual(gateway.runtime_user(), (1000, 1000))

    def test_none_when_user_missing(self):
        with mock.patch("os.geteuid", return_value=0), \
             mock.patch("pwd.getpwnam", side_effect=KeyError("agent")):
            os.environ.pop("BOOA_GATEWAY_NONROOT", None)
            self.assertIsNone(gateway.runtime_user())


class HandOverTests(unittest.TestCase):
    def test_server_secrets_stay_root_and_the_rest_goes_to_agent(self):
        home = tempfile.mkdtemp()
        hh = os.path.join(home, "hermes")
        os.makedirs(os.path.join(hh, "memories"))
        for name in (".console-key", ".api-server-key", "onchain-settings.json",
                     ".session-secret", ".wallet-state.json",
                     "config.yaml", ".env", ".approvals.json"):
            with open(os.path.join(hh, name), "w") as f:
                f.write("x")
        with open(os.path.join(hh, "memories", "MEMORY.md"), "w") as f:
            f.write("x")

        owners: dict[str, tuple[int, int]] = {}
        modes: dict[str, int] = {}
        with mock.patch("os.lchown", side_effect=lambda p, u, g: owners.__setitem__(p, (u, g))), \
             mock.patch("os.chmod", side_effect=lambda p, m: modes.__setitem__(p, m)):
            gateway.hand_over_data(hh, 1000, 1000)

        self.assertEqual(owners[os.path.join(hh, ".console-key")], (0, 0))
        self.assertEqual(modes[os.path.join(hh, ".console-key")], 0o600)
        self.assertEqual(owners[os.path.join(hh, ".session-secret")], (0, 0))
        self.assertEqual(owners[os.path.join(hh, ".wallet-state.json")], (0, 0))
        self.assertEqual(owners[os.path.join(hh, "onchain-settings.json")], (0, 0))
        self.assertEqual(modes[os.path.join(hh, "onchain-settings.json")], 0o644)
        self.assertEqual(owners[os.path.join(hh, "config.yaml")], (1000, 1000))
        self.assertEqual(owners[os.path.join(hh, ".approvals.json")], (1000, 1000))
        self.assertEqual(owners[os.path.join(hh, "memories", "MEMORY.md")], (1000, 1000))
        self.assertEqual(owners[home], (1000, 1000))

    def test_symlinks_are_not_followed(self):
        home = tempfile.mkdtemp()
        hh = os.path.join(home, "hermes")
        os.makedirs(hh)
        target = tempfile.mktemp()
        with open(target, "w") as f:
            f.write("server code")
        os.symlink(target, os.path.join(hh, "sneaky"))

        calls: list[str] = []
        with mock.patch("os.lchown", side_effect=lambda p, u, g: calls.append(p)), \
             mock.patch("os.chmod"):
            gateway.hand_over_data(hh, 1000, 1000)
        self.assertIn(os.path.join(hh, "sneaky"), calls)
        self.assertNotIn(target, calls)


if __name__ == "__main__":
    unittest.main()
