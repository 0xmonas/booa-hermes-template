"""Unit tests for booa.backup.

Run:
    python -m unittest tests.test_backup
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

import pyzipper
import yaml

from booa import backup


def make_home() -> str:
    data = tempfile.mkdtemp()
    home = os.path.join(data, "hermes")
    for d in ["memories", "skills", "context", "sessions"]:
        os.makedirs(os.path.join(home, d))
    Path(home, "SOUL.md").write_text("soul")
    Path(home, "memories", "MEMORY.md").write_text("memory line one")
    Path(home, "sessions", "s1.jsonl").write_text('{"role":"user"}')
    Path(home, "onchain-settings.json").write_text('{"BOOA_ONCHAIN_MCP":"1"}')
    Path(home, "config.yaml").write_text(yaml.dump({
        "model": {"default": "m", "provider": "p"},
        "gateway": {"platforms": {"telegram": {"enabled": True, "bot_token": "123:SECRET"}}},
    }))
    return home


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.home = make_home()

    def export(self, password="pw"):
        return backup.create_backup_zip(
            self.home, password, token_id=7, chain_id=1, agent_name="testa")

    def test_roundtrip_manifest(self):
        path = self.export()
        with pyzipper.AESZipFile(path) as zf:
            zf.setpassword(b"pw")
            manifest = json.loads(zf.read("manifest.json"))
        self.assertEqual(manifest["format"], backup.BACKUP_FORMAT)
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["token_id"], 7)
        self.assertIn("memories/", manifest["contents"])
        os.unlink(path)

    def test_wrong_password_fails(self):
        path = self.export()
        with pyzipper.AESZipFile(path) as zf:
            zf.setpassword(b"wrong")
            with self.assertRaises(Exception):
                zf.read("manifest.json")
        os.unlink(path)

    def test_telegram_token_redacted(self):
        path = self.export()
        with pyzipper.AESZipFile(path) as zf:
            zf.setpassword(b"pw")
            cfg = yaml.safe_load(zf.read("config.yaml"))
            manifest = json.loads(zf.read("manifest.json"))
        self.assertEqual(cfg["gateway"]["platforms"]["telegram"]["bot_token"], "__REDACTED__")
        self.assertTrue(any("bot_token" in r for r in manifest["redactions"]))
        os.unlink(path)

    def test_mcp_secrets_stripped_from_config(self):
        with open(os.path.join(self.home, "config.yaml"), "w") as f:
            yaml.dump({
                "model": {"default": "m"},
                "mcp_servers": {
                    "booa-onchain": {"env": {"OWS_PASSPHRASE": "vault-secret", "ETH_RPC": "https://rpc?key=abc"}},
                    "opensea": {"headers": {"X-API-KEY": "os-secret"}},
                },
            }, f)
        path = self.export()
        with pyzipper.AESZipFile(path) as zf:
            zf.setpassword(b"pw")
            raw = zf.read("config.yaml").decode()
        self.assertNotIn("vault-secret", raw)
        self.assertNotIn("os-secret", raw)
        self.assertNotIn("mcp_servers", raw)
        os.unlink(path)

    def test_ows_signing_secrets_excluded(self):
        ows = Path(self.home).parent / ".ows"
        (ows / "wallets").mkdir(parents=True)
        (ows / "wallets" / "agent.json").write_text("ENCRYPTED_VAULT")
        (ows / "keys").mkdir()
        (ows / "keys" / "scoped.json").write_text("SCOPED_API_KEY")
        (ows / "agent-api-key.txt").write_text("ows_key_SIGNING_SECRET")
        path = self.export()
        with pyzipper.AESZipFile(path) as zf:
            zf.setpassword(b"pw")
            names = zf.namelist()
        self.assertIn("ows/wallets/agent.json", names)
        self.assertNotIn("ows/agent-api-key.txt", names)
        self.assertFalse(any("keys/" in n for n in names))
        os.unlink(path)

    def test_manifest_ram_bomb_rejected(self):
        bad = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        bad.close()
        huge = json.dumps({"format": backup.BACKUP_FORMAT, "format_version": 1,
                           "token_id": 7, "pad": "A" * (2 * 1024 * 1024)})
        with pyzipper.AESZipFile(bad.name, "w", encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(b"pw")
            zf.writestr("manifest.json", huge)
        result = backup.restore_backup(tempfile.mkdtemp(), bad.name, "pw", instance_token_id=7)
        self.assertEqual(result["status"], 400)
        os.unlink(bad.name)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.src_home = make_home()
        self.dst_home = make_home()
        Path(self.dst_home, "memories", "MEMORY.md").write_text("OLD destination memory")
        self.archive = backup.create_backup_zip(
            self.src_home, "pw", token_id=7, chain_id=1, agent_name="testa")

    def tearDown(self):
        if os.path.exists(self.archive):
            os.unlink(self.archive)

    def test_roundtrip_restore(self):
        result = backup.restore_backup(self.dst_home, self.archive, "pw", instance_token_id=7)
        self.assertTrue(result.get("ok"), result)
        self.assertIn("memories", result["restored"])
        self.assertEqual(Path(self.dst_home, "memories", "MEMORY.md").read_text(), "memory line one")

    def test_wrong_password_uninformative(self):
        result = backup.restore_backup(self.dst_home, self.archive, "nope", instance_token_id=7)
        self.assertEqual(result["status"], 400)
        self.assertEqual(result["error"], "invalid archive or password")

    def test_token_mismatch_409_then_confirm(self):
        result = backup.restore_backup(self.dst_home, self.archive, "pw", instance_token_id=99)
        self.assertEqual(result["status"], 409)
        self.assertEqual(result["error"], "token_mismatch")
        self.assertEqual(result["manifest_token_id"], 7)
        result = backup.restore_backup(self.dst_home, self.archive, "pw",
                                       instance_token_id=99, confirm_token_mismatch=True)
        self.assertTrue(result.get("ok"), result)

    def test_config_yaml_never_restored(self):
        original = Path(self.dst_home, "config.yaml").read_text()
        result = backup.restore_backup(self.dst_home, self.archive, "pw", instance_token_id=7)
        self.assertTrue(result.get("ok"))
        self.assertIn("config.yaml", result["skipped"])
        self.assertEqual(Path(self.dst_home, "config.yaml").read_text(), original)

    def test_missing_manifest_rejected(self):
        bad = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        bad.close()
        with pyzipper.AESZipFile(bad.name, "w", encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(b"pw")
            zf.writestr("memories/x.md", "hi")
        result = backup.restore_backup(self.dst_home, bad.name, "pw", instance_token_id=7)
        self.assertEqual(result["status"], 400)
        os.unlink(bad.name)

    def test_zip_slip_rejected(self):
        bad = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        bad.close()
        manifest = {"format": backup.BACKUP_FORMAT, "format_version": 1, "token_id": 7}
        with pyzipper.AESZipFile(bad.name, "w", encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(b"pw")
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("memories/../../evil.txt", "pwned")
        result = backup.restore_backup(self.dst_home, bad.name, "pw", instance_token_id=7)
        self.assertEqual(result["status"], 400)
        self.assertEqual(result["error"], "unsafe archive entry")
        parent = Path(self.dst_home).parent.parent
        self.assertFalse((parent / "evil.txt").exists())
        os.unlink(bad.name)

    def test_absolute_path_rejected(self):
        bad = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        bad.close()
        manifest = {"format": backup.BACKUP_FORMAT, "format_version": 1, "token_id": 7}
        with pyzipper.AESZipFile(bad.name, "w", encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(b"pw")
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("/etc/evil.txt", "pwned")
        result = backup.restore_backup(self.dst_home, bad.name, "pw", instance_token_id=7)
        self.assertEqual(result["status"], 400)
        os.unlink(bad.name)

    def test_newer_format_version_rejected(self):
        bad = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        bad.close()
        manifest = {"format": backup.BACKUP_FORMAT, "format_version": 2, "token_id": 7}
        with pyzipper.AESZipFile(bad.name, "w", encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(b"pw")
            zf.writestr("manifest.json", json.dumps(manifest))
        result = backup.restore_backup(self.dst_home, bad.name, "pw", instance_token_id=7)
        self.assertEqual(result["status"], 400)
        self.assertIn("newer template", result["error"])
        os.unlink(bad.name)

    def test_live_ows_vault_skipped_by_default(self):
        src_data = Path(self.src_home).parent
        (src_data / ".ows" / "wallets").mkdir(parents=True)
        (src_data / ".ows" / "wallets" / "w.json").write_text("{}")
        archive = backup.create_backup_zip(
            self.src_home, "pw", token_id=7, chain_id=1, agent_name="testa")

        dst_data = Path(self.dst_home).parent
        (dst_data / ".ows" / "wallets").mkdir(parents=True)
        (dst_data / ".ows" / "wallets" / "live.json").write_text("LIVE")

        result = backup.restore_backup(self.dst_home, archive, "pw", instance_token_id=7)
        self.assertTrue(result.get("ok"))
        self.assertNotIn("ows", result["restored"])
        self.assertTrue(any("vault" in w for w in result["warnings"]))
        self.assertTrue((dst_data / ".ows" / "wallets" / "live.json").exists())

        result = backup.restore_backup(self.dst_home, archive, "pw",
                                       instance_token_id=7, restore_wallet=True)
        self.assertTrue(result.get("ok"))
        self.assertIn("ows", result["restored"])
        self.assertTrue((dst_data / ".ows" / "wallets" / "w.json").exists())
        os.unlink(archive)

    def test_plain_zip_with_manifest_still_needs_admin_gate(self):
        bad = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        bad.close()
        manifest = {"format": backup.BACKUP_FORMAT, "format_version": 1, "token_id": 7}
        with zipfile.ZipFile(bad.name, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("memories/injected.md", "attacker data")
        result = backup.restore_backup(self.dst_home, bad.name, "anything", instance_token_id=7)
        self.assertTrue(result.get("ok"))
        os.unlink(bad.name)


if __name__ == "__main__":
    unittest.main()
