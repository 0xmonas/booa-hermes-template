"""Output filter — BOOA-specific secret detection, complementing Nous's redact.

This module is the BOOA/crypto-specific layer on top of Nous Research's
``agent.redact`` (hermes-agent upstream). Nous already redacts ~30 generic
API-key prefixes (OpenAI, GitHub, AWS, Stripe, Slack, Google, Replicate,
HuggingFace, Telegram bot, …), JSON/ENV assignment patterns, JWTs,
Authorization headers, PEM private-key blocks, database connection-string
passwords, and PII (Discord IDs, phone numbers). Callers should run Nous's
``redact_sensitive_text`` first, then pass the result through ``filter_output``
for the BOOA-specific patterns documented below.

Detection layers (what this module adds on top of Nous):
  1. BIP39 mnemonic phrases (12 or 24 word sequences from the BIP39 wordlist)
  2. WIF private keys (base58, 51-52 chars, 5/K/L prefix)
  3. OWS wallet API keys (``ows_key_...``) — OpenWallet Standard, BOOA-specific
  4. Context-labeled 0x + 64 hex ("private key: 0x…") — NOT blanket tx hashes
  5. Private file content (lines hashed from USER.md, MEMORY.md, .env, secrets.txt)
  6. Operator-configured deny-list

The filter is a runtime layer, not a model-level behavior. The prompt tells the
model not to produce these patterns; this filter catches the cases where the
model is wrong.

Implements agent-defense.md §4.2 and §8.1 together with Nous's upstream redact.

See: https://khora.fun/agent-defense.md
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# BOOA-specific API key patterns only. Generic API keys (sk-, AKIA, ghp_, xoxb-,
# Telegram bot tokens, etc.) are handled by Nous's ``agent.redact`` — we do not
# duplicate that work here. The one exception is OWS (OpenWallet Standard), which
# is a BOOA-aligned wallet protocol that Nous has no awareness of.
_API_KEY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bows_key_[A-Za-z0-9]{20,}\b"), "ows"),
]

_WIF_PATTERN = re.compile(r"\b[5KL][1-9A-HJ-NP-Za-km-z]{50,51}\b")

_LABELED_HEX = re.compile(
    r"(?i)"
    r"(?:private\s*key|priv\s*key|\bpk\b|secret(?:\s*key)?|mnemonic|seed(?:\s*phrase)?|wallet\s*key)"
    r"\s*[:=]?\s*"
    r"0x[a-fA-F0-9]{64}\b"
)

# BIP39 supports 12, 15, 18, 21, or 24 words. The candidate regex matches any
# span of 12-24 consecutive lowercase words of valid length; _scan_bip39 then
# enforces the exact length set and validates each word against the wordlist.
_BIP39_CANDIDATE = re.compile(r"\b(?:[a-z]{3,8}\s+){11,23}[a-z]{3,8}\b", re.IGNORECASE)
_BIP39_VALID_LENGTHS = frozenset({12, 15, 18, 21, 24})

_MIN_PRIVATE_LINE_LEN = 10


# ---------------------------------------------------------------------------
# BIP39 wordlist — loaded once on import
# ---------------------------------------------------------------------------

_BIP39_WORDLIST: frozenset[str] = frozenset()


def _load_bip39_wordlist() -> frozenset[str]:
    global _BIP39_WORDLIST
    if _BIP39_WORDLIST:
        return _BIP39_WORDLIST
    path = Path(__file__).parent / "bip39_wordlist.txt"
    try:
        words = path.read_text(encoding="utf-8").split()
        _BIP39_WORDLIST = frozenset(w.strip().lower() for w in words if w.strip())
    except FileNotFoundError:
        _BIP39_WORDLIST = frozenset()
    return _BIP39_WORDLIST


_load_bip39_wordlist()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    """A single detection result."""

    pattern_type: str           # bip39 | wif | api_key | labeled_hex | private_file | deny_list
    subtype: str | None         # e.g. "openai" for api_key, "12-word" for bip39
    span: tuple[int, int]       # (start, end) character offsets in scanned text
    severity: str               # critical | high | medium


@dataclass
class FilterResult:
    """Result of filter_output()."""

    text: str                   # Filtered text (redacted if hits)
    hits: list[Hit] = field(default_factory=list)
    original_hash: str = ""     # SHA-256 of original text (short) — for logs

    @property
    def was_filtered(self) -> bool:
        return len(self.hits) > 0


_OPERATOR_WARNING_TEMPLATE = (
    "⚠️  SENSITIVE DATA BELOW — save it offline NOW (paper or password manager)\n"
    "⚠️  Do not keep this message in your chat history — delete after copying\n"
    "⚠️  Never share with anyone, human or agent\n"
    "\n"
)


def operator_warning(hits: list[Hit]) -> str:
    """Return a human-readable warning prefix describing what sensitive content follows.

    Used when bypassing redaction for a verified operator — the raw secret is shown
    but prepended with a warning so the operator handles it carefully.
    """
    if not hits:
        return ""
    types = sorted({h.pattern_type + (f"/{h.subtype}" if h.subtype else "") for h in hits})
    return _OPERATOR_WARNING_TEMPLATE + f"Detected: {', '.join(types)}\n\n"


# ---------------------------------------------------------------------------
# Public API — scan / redact / filter_output
# ---------------------------------------------------------------------------


def scan(
    text: str,
    *,
    private_file_hashes: frozenset[str] | None = None,
    deny_list: list[str] | None = None,
) -> list[Hit]:
    """Scan text for all pattern types. Returns a list of Hit objects.

    Thread-safe. No I/O. No state mutation.
    """
    hits: list[Hit] = []
    hits.extend(_scan_bip39(text))
    hits.extend(_scan_wif(text))
    hits.extend(_scan_api_keys(text))
    hits.extend(_scan_labeled_hex(text))
    if private_file_hashes:
        hits.extend(_scan_private_files(text, private_file_hashes))
    if deny_list:
        hits.extend(_scan_deny_list(text, deny_list))
    return hits


def redact(text: str, hits: list[Hit]) -> str:
    """Replace each hit span with `[REDACTED:<type>]`.

    Overlapping hits: the higher-severity hit wins. Ties broken by earlier span.
    """
    if not hits:
        return text

    # Resolve overlaps: keep the highest-severity hit in each overlapping region
    severity_rank = {"critical": 3, "high": 2, "medium": 1}
    sorted_hits = sorted(
        hits,
        key=lambda h: (h.span[0], -severity_rank.get(h.severity, 0)),
    )
    resolved: list[Hit] = []
    for h in sorted_hits:
        if resolved and h.span[0] < resolved[-1].span[1]:
            # Overlap — keep whichever has higher severity
            if severity_rank.get(h.severity, 0) > severity_rank.get(resolved[-1].severity, 0):
                resolved[-1] = h
            continue
        resolved.append(h)

    # Rebuild text with redactions (reverse order to preserve offsets)
    out = text
    for h in sorted(resolved, key=lambda h: h.span[0], reverse=True):
        tag = h.subtype or h.pattern_type
        out = out[: h.span[0]] + f"[REDACTED:{tag}]" + out[h.span[1]:]
    return out


def filter_output(
    text: str,
    *,
    channel: str,
    incident_log_path: str | None = None,
    private_file_hashes: frozenset[str] | None = None,
    deny_list: list[str] | None = None,
) -> FilterResult:
    """Scan text, redact hits, optionally log an incident.

    Args:
        text: Outbound text the agent intends to send.
        channel: Destination channel identifier (e.g. "telegram", "twitter").
        incident_log_path: If set, append a JSON-lines incident record on hit.
        private_file_hashes: Hash set from compute_file_hashes(). Optional.
        deny_list: Extra phrases to redact (case-insensitive substring). Optional.

    Returns:
        FilterResult with the (possibly redacted) text and hit list.
    """
    hits = scan(
        text,
        private_file_hashes=private_file_hashes,
        deny_list=deny_list,
    )
    original_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    if not hits:
        return FilterResult(text=text, hits=[], original_hash=original_hash)

    filtered = redact(text, hits)
    if incident_log_path:
        _log_incident(
            incident_log_path,
            channel=channel,
            original_hash=original_hash,
            hits=hits,
        )
    return FilterResult(text=filtered, hits=hits, original_hash=original_hash)


# ---------------------------------------------------------------------------
# Hash computation for private-file matching
# ---------------------------------------------------------------------------


def compute_file_hashes(paths: list[str]) -> frozenset[str]:
    """Compute SHA-256 of each stripped line from given files.

    Only lines >= _MIN_PRIVATE_LINE_LEN chars are hashed — shorter lines
    would match common English and trigger false positives.

    Missing files are silently skipped.
    """
    hashes: set[str] = set()
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if len(stripped) >= _MIN_PRIVATE_LINE_LEN:
                        hashes.add(hashlib.sha256(stripped.encode("utf-8")).hexdigest())
        except (FileNotFoundError, PermissionError, IsADirectoryError):
            continue
    return frozenset(hashes)


# ---------------------------------------------------------------------------
# Internal scanners
# ---------------------------------------------------------------------------


def _scan_bip39(text: str) -> list[Hit]:
    if not _BIP39_WORDLIST:
        return []
    hits: list[Hit] = []
    for m in _BIP39_CANDIDATE.finditer(text):
        words = [w.lower() for w in m.group().split()]
        if len(words) not in _BIP39_VALID_LENGTHS:
            continue
        if all(w in _BIP39_WORDLIST for w in words):
            hits.append(Hit(
                pattern_type="bip39",
                subtype=f"{len(words)}-word",
                span=m.span(),
                severity="critical",
            ))
    return hits


def _scan_wif(text: str) -> list[Hit]:
    return [
        Hit("wif", None, m.span(), "critical")
        for m in _WIF_PATTERN.finditer(text)
    ]


def _scan_api_keys(text: str) -> list[Hit]:
    hits: list[Hit] = []
    for pat, subtype in _API_KEY_PATTERNS:
        for m in pat.finditer(text):
            hits.append(Hit("api_key", subtype, m.span(), "high"))
    return hits


def _scan_labeled_hex(text: str) -> list[Hit]:
    hits: list[Hit] = []
    for m in _LABELED_HEX.finditer(text):
        # Redact only the 0x... portion, not the label
        full = m.group()
        hex_match = re.search(r"0x[a-fA-F0-9]{64}", full)
        if hex_match:
            start = m.start() + hex_match.start()
            end = m.start() + hex_match.end()
            hits.append(Hit("labeled_hex", None, (start, end), "critical"))
    return hits


def _scan_private_files(text: str, hashes: frozenset[str]) -> list[Hit]:
    hits: list[Hit] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if len(stripped) >= _MIN_PRIVATE_LINE_LEN:
            h = hashlib.sha256(stripped.encode("utf-8")).hexdigest()
            if h in hashes:
                start_in_line = line.find(stripped)
                abs_start = offset + start_in_line
                abs_end = abs_start + len(stripped)
                hits.append(Hit("private_file", None, (abs_start, abs_end), "high"))
        offset += len(line)
    return hits


def _scan_deny_list(text: str, deny_list: list[str]) -> list[Hit]:
    hits: list[Hit] = []
    lower = text.lower()
    for phrase in deny_list:
        p = phrase.lower()
        if not p:
            continue
        start = 0
        while True:
            idx = lower.find(p, start)
            if idx == -1:
                break
            hits.append(Hit("deny_list", None, (idx, idx + len(p)), "medium"))
            start = idx + 1
    return hits


# ---------------------------------------------------------------------------
# Incident logging
# ---------------------------------------------------------------------------


def _log_incident(
    log_path: str,
    *,
    channel: str,
    original_hash: str,
    hits: list[Hit],
) -> None:
    """Append a single JSON-lines record describing the incident.

    The original text is *never* written to the log — only a short hash for
    correlation. Logging the redacted content would defeat the purpose.
    """
    record = {
        "timestamp": time.time(),
        "channel": channel,
        "content_hash": original_hash,
        "hit_count": len(hits),
        "hits": [
            {
                "type": h.pattern_type,
                "subtype": h.subtype,
                "severity": h.severity,
                "span": list(h.span),
            }
            for h in hits
        ],
    }
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        # Failing to log must not break the filter pipeline
        pass
