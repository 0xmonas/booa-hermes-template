"""Unit tests for booa.writer api_server platform wiring.

Run:
    python -m unittest tests.test_writer_api_server
"""

from __future__ import annotations

import os
import tempfile
import unittest

import yaml

from booa import writer


class ApiServerPlatformTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        writer.ensure_dirs(self.home)

    def read_config(self) -> dict:
        with open(os.path.join(self.home, "config.yaml")) as f:
            return yaml.safe_load(f)

    def test_write_config_enables_api_server(self):
        writer.write_config(self.home, "openrouter", "key123", "some/model")
        cfg = self.read_config()
        api = cfg["gateway"]["platforms"]["api_server"]
        self.assertTrue(api["enabled"])
        self.assertEqual(api["extra"]["host"], "127.0.0.1")
        self.assertEqual(api["extra"]["port"], 8642)

    def test_telegram_block_preserved(self):
        writer.write_config(self.home, "openrouter", "key123", "some/model",
                            telegram_token="123:abc", telegram_users="alice")
        cfg = self.read_config()
        tg = cfg["gateway"]["platforms"]["telegram"]
        self.assertTrue(tg["enabled"])
        self.assertEqual(tg["bot_token"], "123:abc")
        self.assertEqual(tg["allowed_users"], "alice")
        self.assertIn("api_server", cfg["gateway"]["platforms"])

    def test_idempotent(self):
        writer.write_config(self.home, "openrouter", "key123", "some/model")
        writer.ensure_api_server_platform(self.home)
        writer.ensure_api_server_platform(self.home)
        cfg = self.read_config()
        self.assertTrue(cfg["gateway"]["platforms"]["api_server"]["enabled"])

    def test_key_absent_from_config(self):
        writer.write_config(self.home, "openrouter", "key123", "some/model")
        with open(os.path.join(self.home, "config.yaml")) as f:
            raw = f.read()
        self.assertNotIn("key", (yaml.safe_load(raw)["gateway"]["platforms"]["api_server"].get("extra") or {}))

    def test_migration_on_legacy_config(self):
        with open(os.path.join(self.home, "config.yaml"), "w") as f:
            yaml.dump({"model": {"default": "m"},
                       "gateway": {"platforms": {"telegram": {"enabled": True, "bot_token": "t"}}}}, f)
        writer.ensure_api_server_platform(self.home)
        cfg = self.read_config()
        self.assertIn("api_server", cfg["gateway"]["platforms"])
        self.assertEqual(cfg["gateway"]["platforms"]["telegram"]["bot_token"], "t")


if __name__ == "__main__":
    unittest.main()
