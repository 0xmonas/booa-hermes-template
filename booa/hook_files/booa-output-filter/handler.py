"""gateway:startup hook — monkey-patch platform adapter send() to run
Nous redact + booa.output_filter. Operator recipients get passthrough with
a safety warning; everyone else gets redaction."""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("booa.hook.output_filter")


_DEFAULT_BOOA_PATH = "/app"
_DEFAULT_HERMES_HOME = "/data/hermes"
_DEFAULT_INCIDENT_LOG = "/data/hermes/incidents.log"

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
    if event_type != "gateway:startup":
        return
    try:
        _install_filter()
    except Exception as exc:
        log.warning("[booa-filter] install failed: %s", exc)


def _install_filter() -> None:
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
    incident_log = os.environ.get("BOOA_INCIDENT_LOG", _DEFAULT_INCIDENT_LOG)

    private_paths = [
        os.path.join(hermes_home, "memories", "USER.md"),
        os.path.join(hermes_home, "memories", "MEMORY.md"),
        os.path.join(hermes_home, ".env"),
        os.path.join(hermes_home, "secrets.txt"),
    ]
    private_hashes = output_filter.compute_file_hashes(private_paths)
    operator_registry = _load_operator_registry(hermes_home)

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
    """Return {platform: {chat_id, …}} from env vars + config.yaml + pairing files."""
    registry: dict[str, set[str]] = {}

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

    config_path = os.path.join(hermes_home, "config.yaml")
    if os.path.isfile(config_path):
        try:
            import yaml
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
