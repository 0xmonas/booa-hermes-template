"""Unit tests for booa.output_filter.

Covers the BOOA-specific detection layers (BIP39, WIF, OWS key, labeled hex,
private-file content, deny-list) plus redaction correctness, incident logging,
and false-positive guards. Generic API-key prefixes (sk-, AKIA, ghp_, Telegram
bot, etc.) are covered upstream by Nous's ``agent.redact`` and are not tested
here.

Run:
    python -m unittest tests.test_output_filter
    # or: python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from booa import output_filter as of


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A real 12-word BIP39 phrase (valid checksum is unnecessary for pattern test —
# we only check that each word is on the wordlist).
VALID_BIP39_12 = (
    "abandon ability able about above absent absorb abstract absurd abuse access accident"
)

# 24-word phrase (each word is in the BIP39 list)
VALID_BIP39_24 = (
    "abandon ability able about above absent absorb abstract absurd abuse access accident "
    "account accuse achieve acid acoustic acquire across act action actor actress actual"
)

# Text that happens to have 12 lowercase words but none are in BIP39 list
FAKE_ENGLISH_12 = (
    "sometimes the quick brown fox jumps over the lazy dog today ran"
)

# BOOA-specific API key — OWS (OpenWallet Standard) wallet API token
OWS_KEY = "ows_key_abcdefghij1234567890xyz"

# WIF private key (valid base58 pattern, starts with K)
WIF_KEY = "KwdMAjGmerYanjeui5SHS7JkmpZvVipYvB2LJGU1ZxJwYvP98617"

# Private key hex — 0x + 64 hex
PRIV_KEY_HEX = "0x" + "a" * 64
TX_HASH = "0x" + "b" * 64  # Same shape but semantically a tx hash


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestScanBip39(unittest.TestCase):
    def test_detects_valid_12_word(self) -> None:
        hits = of.scan(f"my phrase: {VALID_BIP39_12}")
        self.assertTrue(any(h.pattern_type == "bip39" for h in hits))
        bip39 = [h for h in hits if h.pattern_type == "bip39"][0]
        self.assertEqual(bip39.subtype, "12-word")
        self.assertEqual(bip39.severity, "critical")

    def test_detects_valid_24_word(self) -> None:
        hits = of.scan(VALID_BIP39_24)
        bip39 = [h for h in hits if h.pattern_type == "bip39"]
        self.assertEqual(len(bip39), 1)
        self.assertEqual(bip39[0].subtype, "24-word")

    def test_ignores_non_bip39_english(self) -> None:
        hits = of.scan(FAKE_ENGLISH_12)
        self.assertEqual([h for h in hits if h.pattern_type == "bip39"], [])

    def test_ignores_11_or_13_word_sequences(self) -> None:
        eleven = " ".join(VALID_BIP39_12.split()[:11])
        thirteen = VALID_BIP39_12 + " access"
        self.assertEqual([h for h in of.scan(eleven) if h.pattern_type == "bip39"], [])
        self.assertEqual([h for h in of.scan(thirteen) if h.pattern_type == "bip39"], [])

    def test_case_insensitive(self) -> None:
        upper = VALID_BIP39_12.upper()
        hits = [h for h in of.scan(upper) if h.pattern_type == "bip39"]
        self.assertEqual(len(hits), 1)


class TestScanWif(unittest.TestCase):
    def test_detects_wif(self) -> None:
        hits = of.scan(f"backup: {WIF_KEY}")
        wif = [h for h in hits if h.pattern_type == "wif"]
        self.assertEqual(len(wif), 1)
        self.assertEqual(wif[0].severity, "critical")

    def test_ignores_random_base58(self) -> None:
        # Too short to be WIF
        hits = of.scan("5abc123")
        self.assertEqual([h for h in hits if h.pattern_type == "wif"], [])


class TestScanApiKeys(unittest.TestCase):
    """Only OWS is covered here. Generic API keys are handled by Nous's redact."""

    def test_ows(self) -> None:
        hits = [h for h in of.scan(OWS_KEY) if h.pattern_type == "api_key"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].subtype, "ows")

    def test_ows_in_sentence(self) -> None:
        text = f"the token is {OWS_KEY} please keep it safe"
        hits = [h for h in of.scan(text) if h.pattern_type == "api_key"]
        self.assertEqual(len(hits), 1)


class TestScanLabeledHex(unittest.TestCase):
    def test_detects_labeled_private_key(self) -> None:
        text = f"my private key: {PRIV_KEY_HEX}"
        hits = [h for h in of.scan(text) if h.pattern_type == "labeled_hex"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].severity, "critical")

    def test_detects_pk_abbreviation(self) -> None:
        text = f"pk = {PRIV_KEY_HEX}"
        hits = [h for h in of.scan(text) if h.pattern_type == "labeled_hex"]
        self.assertEqual(len(hits), 1)

    def test_detects_secret_label(self) -> None:
        text = f"secret: {PRIV_KEY_HEX}"
        hits = [h for h in of.scan(text) if h.pattern_type == "labeled_hex"]
        self.assertEqual(len(hits), 1)

    def test_ignores_tx_hash_without_label(self) -> None:
        text = f"Transaction hash: {TX_HASH}"
        hits = [h for h in of.scan(text) if h.pattern_type == "labeled_hex"]
        self.assertEqual(hits, [])

    def test_ignores_bare_hex(self) -> None:
        text = f"Here is a value: {TX_HASH}"
        hits = [h for h in of.scan(text) if h.pattern_type == "labeled_hex"]
        self.assertEqual(hits, [])

    def test_redacts_only_hex_not_label(self) -> None:
        text = f"my private key: {PRIV_KEY_HEX}"
        hits = of.scan(text)
        out = of.redact(text, hits)
        self.assertIn("private key:", out)
        self.assertNotIn(PRIV_KEY_HEX, out)
        self.assertIn("[REDACTED:labeled_hex]", out)


class TestScanPrivateFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.user_md = Path(self.tmp.name) / "USER.md"
        self.user_md.write_text(
            "# USER.md\n\n"
            "My name is Alice Smithfield and I hold BOOA 1496.\n"
            "short\n"  # too short, will be skipped
            "My spending limit is 20 USDC per day.\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_compute_file_hashes(self) -> None:
        hashes = of.compute_file_hashes([str(self.user_md)])
        self.assertIsInstance(hashes, frozenset)
        # "short" line should NOT be included (too short)
        import hashlib
        short_hash = hashlib.sha256(b"short").hexdigest()
        self.assertNotIn(short_hash, hashes)
        # Name line should be included
        name_hash = hashlib.sha256(
            b"My name is Alice Smithfield and I hold BOOA 1496."
        ).hexdigest()
        self.assertIn(name_hash, hashes)

    def test_detects_private_line(self) -> None:
        hashes = of.compute_file_hashes([str(self.user_md)])
        agent_output = "User asked about their identity:\nMy name is Alice Smithfield and I hold BOOA 1496."
        hits = of.scan(agent_output, private_file_hashes=hashes)
        private_hits = [h for h in hits if h.pattern_type == "private_file"]
        self.assertEqual(len(private_hits), 1)
        self.assertEqual(private_hits[0].severity, "high")

    def test_ignores_missing_file(self) -> None:
        hashes = of.compute_file_hashes(["/nonexistent/path/USER.md"])
        self.assertEqual(hashes, frozenset())


class TestDenyList(unittest.TestCase):
    def test_detects_phrase(self) -> None:
        hits = of.scan("the answer is forbidden-project-codename", deny_list=["forbidden-project-codename"])
        deny_hits = [h for h in hits if h.pattern_type == "deny_list"]
        self.assertEqual(len(deny_hits), 1)

    def test_case_insensitive(self) -> None:
        hits = of.scan("Forbidden-Project-Codename", deny_list=["forbidden-project-codename"])
        self.assertEqual(len([h for h in hits if h.pattern_type == "deny_list"]), 1)

    def test_empty_deny_list(self) -> None:
        hits = of.scan("anything", deny_list=[])
        self.assertEqual([h for h in hits if h.pattern_type == "deny_list"], [])


class TestRedact(unittest.TestCase):
    def test_no_hits_returns_original(self) -> None:
        self.assertEqual(of.redact("clean text", []), "clean text")

    def test_redacts_bip39(self) -> None:
        text = f"mnemonic: {VALID_BIP39_12}"
        hits = of.scan(text)
        out = of.redact(text, hits)
        self.assertNotIn("abandon", out)
        self.assertIn("[REDACTED:", out)

    def test_redacts_multiple_non_overlapping(self) -> None:
        text = f"{OWS_KEY} and {WIF_KEY}"
        hits = of.scan(text)
        out = of.redact(text, hits)
        self.assertNotIn(OWS_KEY, out)
        self.assertNotIn(WIF_KEY, out)
        self.assertEqual(out.count("[REDACTED:"), 2)

    def test_higher_severity_wins_in_overlap(self) -> None:
        # Construct an overlap where labeled_hex (critical) would replace
        # a deny_list match (medium) in the same span
        text = f"private key: {PRIV_KEY_HEX}"
        hits = of.scan(text, deny_list=["a" * 64])
        out = of.redact(text, hits)
        # We expect labeled_hex redaction to win
        self.assertIn("[REDACTED:labeled_hex]", out)


class TestFilterOutput(unittest.TestCase):
    def test_clean_text_returns_unchanged(self) -> None:
        result = of.filter_output("just a normal message", channel="telegram")
        self.assertFalse(result.was_filtered)
        self.assertEqual(result.text, "just a normal message")
        self.assertEqual(result.hits, [])

    def test_dirty_text_is_redacted(self) -> None:
        result = of.filter_output(f"key: {OWS_KEY}", channel="telegram")
        self.assertTrue(result.was_filtered)
        self.assertNotIn(OWS_KEY, result.text)
        self.assertIn("[REDACTED:", result.text)

    def test_incident_is_logged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "incidents.log")
            of.filter_output(
                f"key: {OWS_KEY}",
                channel="telegram",
                incident_log_path=log_path,
            )
            self.assertTrue(os.path.exists(log_path))
            with open(log_path) as f:
                record = json.loads(f.readline())
            self.assertEqual(record["channel"], "telegram")
            self.assertEqual(record["hit_count"], 1)
            self.assertEqual(record["hits"][0]["type"], "api_key")
            # Original text must NOT be logged
            self.assertNotIn("ows_key_", json.dumps(record))

    def test_log_path_creates_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "nested", "dir", "incidents.log")
            of.filter_output(
                f"key: {OWS_KEY}",
                channel="telegram",
                incident_log_path=log_path,
            )
            self.assertTrue(os.path.exists(log_path))

    def test_clean_text_does_not_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "incidents.log")
            of.filter_output("clean", channel="telegram", incident_log_path=log_path)
            self.assertFalse(os.path.exists(log_path))

    def test_log_failure_does_not_raise(self) -> None:
        # Pass a log path that cannot be written (parent is a file, not dir)
        with tempfile.NamedTemporaryFile() as tf:
            bogus_log = os.path.join(tf.name, "cannot-create.log")
            result = of.filter_output(
                f"key: {OWS_KEY}",
                channel="telegram",
                incident_log_path=bogus_log,
            )
            # Filter should still succeed, logging failure is silent
            self.assertTrue(result.was_filtered)


class TestFalsePositives(unittest.TestCase):
    """Guard against common legitimate agent output being flagged."""

    def test_tx_hash_passes_clean(self) -> None:
        text = (
            "Your transfer succeeded. "
            "Transaction: 0xabc1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcd"
        )
        result = of.filter_output(text, channel="telegram")
        self.assertFalse(result.was_filtered, f"Unexpected hits: {result.hits}")

    def test_eth_address_passes_clean(self) -> None:
        text = "Your wallet is 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb1."
        result = of.filter_output(text, channel="telegram")
        self.assertFalse(result.was_filtered, f"Unexpected hits: {result.hits}")

    def test_normal_english_passes_clean(self) -> None:
        text = (
            "I checked the registry and your agent is verified. "
            "Everything looks good for your upcoming mint."
        )
        result = of.filter_output(text, channel="telegram")
        self.assertFalse(result.was_filtered, f"Unexpected hits: {result.hits}")

    def test_markdown_with_hashes_passes(self) -> None:
        text = "## Step 1\n\n- Fetch the block hash 0x" + "0" * 64
        result = of.filter_output(text, channel="telegram")
        self.assertFalse(result.was_filtered, f"Unexpected hits: {result.hits}")


if __name__ == "__main__":
    unittest.main()
