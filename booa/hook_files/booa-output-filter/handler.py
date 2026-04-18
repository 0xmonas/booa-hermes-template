"""BOOA output-filter hook handler.

Runs on ``gateway:startup``. Monkey-patches every loaded
``BasePlatformAdapter.send`` so that every outbound message passes through:

  1. Nous's ``agent.redact.redact_sensitive_text`` (generic secrets)
  2. BOOA's ``booa.output_filter.filter_output`` (crypto + private-file hashes)

This covers the actual platform delivery path (Telegram, Discord, Slack, …)
which Nous does *not* route through its own redact. The filter only rewrites
the ``content`` string passed to ``send`` — media, media captions, and reactions
are not currently filtered.

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
            filter_output=output_filter.filter_output,
            private_hashes=private_hashes,
            incident_log=incident_log,
        )
        patched += 1

    log.info(
        "[booa-filter] installed on %d platform adapter(s) — incident log %s",
        patched,
        incident_log,
    )


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
    filter_output,
    private_hashes,
    incident_log,
) -> None:
    """Replace cls.send with a filtered version. Idempotent."""
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

        if filtered and redact_sensitive_text is not None:
            try:
                filtered = redact_sensitive_text(filtered)
            except Exception as exc:
                log.debug("[booa-filter] nous redact failed: %s", exc)

        if filtered:
            try:
                channel = getattr(getattr(self, "platform", None), "value", "unknown")
                result = filter_output(
                    filtered,
                    channel=str(channel),
                    incident_log_path=incident_log,
                    private_file_hashes=private_hashes,
                )
                filtered = result.text
            except Exception as exc:
                log.debug("[booa-filter] booa filter failed: %s", exc)

        return await __orig(self, chat_id, filtered, reply_to=reply_to, metadata=metadata)

    cls.send = wrapped_send
    cls._booa_filtered = True
