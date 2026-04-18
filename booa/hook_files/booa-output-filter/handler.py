"""BOOA output-filter hook handler.

Runs on ``gateway:startup``. Monkey-patches every loaded
``BasePlatformAdapter.send`` so that every outbound message passes through:

  1. Nous's ``agent.redact.redact_sensitive_text`` (generic secrets)
  2. BOOA's ``booa.output_filter.filter_output`` (crypto + private-file hashes)

**Operator-aware mode.** The filter's purpose is to keep sensitive data away
from anyone who is *not* the operator — not from the operator themselves.
When a message is destined for a chat_id that matches the platform's
allowed_users list (i.e. the operator), the filter logs the detection but
**does not redact**; it prepends a safety warning so the operator knows to
save the data offline and delete the chat message after copying. For any
other recipient, full redaction applies.

If any import fails, the hook logs and returns without crashing the gateway.
Filtering failure must never block the main pipeline.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("booa.hook.output_filter")


# Default paths inside the Hermes Template container. Overridden via env vars
# for tests / custom deployments.
_DEFAULT_BOOA_PATH = "/app"
_DEFAULT_HERMES_HOME = "/data/hermes"
_DEFAULT_INCIDENT_LOG = "/data/hermes/incidents.log"

# Platform adapter modules we attempt to pre-import so that
# ``BasePlatformAdapter.__subclasses__()`` returns them. Missing modules are
# silently skipped — different Hermes builds ship different platforms.
_PLATFORM_MODULES = [
    "gateway.platforms.telegram",
    "gateway.platforms.discord",
    "gateway.platforms.slack",
    "gateway.platforms.matrix",
    "gateway.platforms.signal",
    "gateway.platforms.whatsapp",
    "gateway.platforms.feishu",
    "gateway.platforms.weixin",
    "gateway.platforms.bluebubbles",
    "gateway.platforms.dingtalk",
    "gateway.platforms.email",
    "gateway.platforms.homeassistant",
    "gateway.platforms.mattermost",
    "gateway.platforms.webhook",
    "gateway.platforms.api_server",
]


async def handle(event_type: str, context: dict) -> None:
    """Hermes hook entry point."""
    if event_type != "gateway:startup":
        return
    try:
        _install_filter()
    except Exception as exc:
        log.warning("[booa-filter] install failed: %s", exc)


def _install_filter() -> None:
    """Wrap every loaded platform adapter's ``send`` method."""
    # Make booa package importable from within the hermes-agent subprocess.
    booa_path = os.environ.get("BOOA_PATH", _DEFAULT_BOOA_PATH)
    if booa_path not in sys.path:
        sys.path.insert(0, booa_path)

    try:
        from booa import output_filter
    except ImportError as exc:
        log.warning("[booa-filter] booa.output_filter unavailable: %s", exc)
        return

    try:
        from agent.redact import redact_sensitive_text
    except ImportError:
        log.info("[booa-filter] agent.redact unavailable; BOOA filter runs alone")
        redact_sensitive_text = None  # type: ignore[assignment]

    try:
        from gateway.platforms.base import BasePlatformAdapter
    except ImportError as exc:
        log.warning("[booa-filter] gateway.platforms.base not importable: %s", exc)
        return

    hermes_home = os.environ.get("HERMES_HOME", _DEFAULT_HERMES_HOME)
    incident_log = os.environ.get(
        "BOOA_INCIDENT_LOG",
        _DEFAULT_INCIDENT_LOG,
    )

    private_paths = [
        os.path.join(hermes_home, "memories", "USER.md"),
        os.path.join(hermes_home, "memories", "MEMORY.md"),
        os.path.join(hermes_home, ".env"),
        # Registered operator secrets (optional file). When present, lines are
        # hashed and matched against outbound content.
        os.path.join(hermes_home, "secrets.txt"),
    ]
    private_hashes = output_filter.compute_file_hashes(private_paths)

    # Load operator allowlists per platform so we can distinguish operator-
    # bound messages from stranger-bound messages. The filter passes sensitive
    # content through to operators (with a safety warning) rather than
    # redacting — the goal is to protect *non-operators*, not to hide data
    # from Alice herself.
    operator_registry = _load_operator_registry(hermes_home)

    # Force-import known platform modules so that BasePlatformAdapter has a
    # populated __subclasses__ list.
    for mod in _PLATFORM_MODULES:
        try:
            __import__(mod)
        except ImportError:
            continue

    patched = 0
    for cls in _all_subclasses(BasePlatformAdapter):
        if getattr(cls, "_booa_filtered", False):
            continue
        _wrap_send(
            cls,
            redact_sensitive_text=redact_sensitive_text,
            output_filter_module=output_filter,
            private_hashes=private_hashes,
            incident_log=incident_log,
            operator_registry=operator_registry,
        )
        patched += 1

    op_count = sum(len(s) for s in operator_registry.values())
    log.info(
        "[booa-filter] installed on %d platform adapter(s), %d operator chat_id(s) — incident log %s",
        patched,
        op_count,
        incident_log,
    )


def _load_operator_registry(hermes_home: str) -> "dict[str, set[str]]":
    """Return ``{platform: {chat_id, …}}`` from env vars + config.yaml.

    Supports the standard Hermes env names (TELEGRAM_ALLOWED_USERS, etc.) plus
    a fallback to config.yaml's ``gateway.platforms.<name>.allowed_users`` list.
    Missing or unparseable sources are silently skipped.
    """
    registry: dict[str, set[str]] = {}

    # Env var pattern: <PLATFORM>_ALLOWED_USERS = "id1,id2,id3"
    env_sources = {
        "telegram": "TELEGRAM_ALLOWED_USERS",
        "discord": "DISCORD_ALLOWED_USERS",
        "slack": "SLACK_ALLOWED_USERS",
        "signal": "SIGNAL_ALLOWED_USERS",
        "whatsapp": "WHATSAPP_ALLOWED_USERS",
        "matrix": "MATRIX_ALLOWED_USERS",
    }
    for platform, env_name in env_sources.items():
        raw = os.environ.get(env_name, "").strip()
        if raw:
            ids = {s.strip() for s in raw.split(",") if s.strip()}
            if ids:
                registry[platform] = ids

    # Fallback: config.yaml (some templates declare allowed_users inline)
    config_path = os.path.join(hermes_home, "config.yaml")
    if os.path.isfile(config_path):
        try:
            import yaml  # hermes-agent ships pyyaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            platforms = (cfg.get("gateway") or {}).get("platforms") or {}
            for platform, pconf in platforms.items():
                allowed = pconf.get("allowed_users") if isinstance(pconf, dict) else None
                if not allowed:
                    continue
                if isinstance(allowed, str):
                    ids = {s.strip() for s in allowed.split(",") if s.strip()}
                else:
                    ids = {str(x).strip() for x in allowed if str(x).strip()}
                if ids:
                    registry.setdefault(platform, set()).update(ids)
        except Exception as exc:
            log.debug("[booa-filter] could not parse config.yaml: %s", exc)

    # Pairing files (Hermes's native approved-users store). One JSON per
    # platform at gateway/platforms/pairing/<platform>-approved.json with the
    # shape ``{"<user_id>": {"user_name": ..., "approved_at": ...}, ...}``.
    # This is the authoritative allowlist for platforms like Telegram where
    # users are approved via the dashboard pairing flow.
    pairing_dir = os.path.join(hermes_home, "platforms", "pairing")
    if os.path.isdir(pairing_dir):
        import json
        for entry in os.listdir(pairing_dir):
            if not entry.endswith("-approved.json"):
                continue
            platform = entry[: -len("-approved.json")]
            try:
                with open(os.path.join(pairing_dir, entry), "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
                ids = {str(k).strip() for k in data.keys() if str(k).strip()}
                if ids:
                    registry.setdefault(platform, set()).update(ids)
            except Exception as exc:
                log.debug("[booa-filter] could not parse %s: %s", entry, exc)

    return registry


def _is_operator(platform: str, chat_id, operator_registry: "dict[str, set[str]]") -> bool:
    if not operator_registry:
        return False
    allowed = operator_registry.get(platform)
    if not allowed:
        return False
    return str(chat_id).strip() in allowed


def _all_subclasses(cls):
    """Recursive subclass iteration (BFS avoids duplicates)."""
    seen = set()
    queue = list(cls.__subclasses__())
    while queue:
        sub = queue.pop(0)
        if sub in seen:
            continue
        seen.add(sub)
        yield sub
        queue.extend(sub.__subclasses__())


def _wrap_send(
    cls,
    *,
    redact_sensitive_text,
    output_filter_module,
    private_hashes,
    incident_log,
    operator_registry,
) -> None:
    """Replace cls.send with an operator-aware filtered version. Idempotent."""
    original_send = cls.send

    async def wrapped_send(
        self,
        chat_id,
        content,
        reply_to=None,
        metadata=None,
        __orig=original_send,
    ):
        filtered = content or ""
        channel = str(getattr(getattr(self, "platform", None), "value", "unknown"))
        is_operator = _is_operator(channel, chat_id, operator_registry)

        if filtered:
            try:
                if is_operator:
                    # Operator path: show the data (Alice owns it) but detect
                    # and prepend a safety warning so she knows to save offline
                    # and delete the message after copying.
                    hits = output_filter_module.scan(
                        filtered,
                        private_file_hashes=private_hashes,
                    )
                    if hits:
                        warning = output_filter_module.operator_warning(hits)
                        filtered = warning + filtered
                        log.warning(
                            "[booa-filter] %d hit(s) on %s (OPERATOR — passthrough with warning): %s",
                            len(hits),
                            channel,
                            ", ".join(
                                f"{h.pattern_type}"
                                + (f"/{h.subtype}" if h.subtype else "")
                                for h in hits
                            ),
                        )
                else:
                    # Non-operator path: full redaction chain.
                    if redact_sensitive_text is not None:
                        filtered = redact_sensitive_text(filtered)
                    result = output_filter_module.filter_output(
                        filtered,
                        channel=channel,
                        incident_log_path=incident_log,
                        private_file_hashes=private_hashes,
                    )
                    filtered = result.text
                    if result.hits:
                        log.warning(
                            "[booa-filter] %d hit(s) on %s (stranger — redacted): %s",
                            len(result.hits),
                            channel,
                            ", ".join(
                                f"{h.pattern_type}"
                                + (f"/{h.subtype}" if h.subtype else "")
                                for h in result.hits
                            ),
                        )
            except Exception as exc:
                log.debug("[booa-filter] filter chain failed: %s", exc)

        return await __orig(self, chat_id, filtered, reply_to=reply_to, metadata=metadata)

    cls.send = wrapped_send
    cls._booa_filtered = True
