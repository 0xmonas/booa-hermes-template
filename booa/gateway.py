"""Hermes gateway subprocess manager."""

import asyncio
import os
import signal
from collections import deque
from typing import AsyncGenerator, Optional

from booa.console_auth import get_or_create_api_server_key

RUNTIME_USER = "agent"
# Files under HERMES_HOME the agent's shell must not own or rewrite. The
# console/api keys stay root-only; onchain-settings.json stays root-owned but
# world-readable so the MCP can read the guardrails it enforces without being
# able to lift its own caps by editing the file.
_SERVER_OWNED_600 = frozenset({
    ".console-key", ".api-server-key", ".console-settings.json",
    # The session signing key: agent-readable would mean the agent can mint
    # admin cookies. The wallet verify state: agent-writable would let it fake
    # a "verified" controller.
    ".session-secret", ".wallet-state.json",
})
_SERVER_OWNED_644 = frozenset({"onchain-settings.json"})


def runtime_user() -> Optional[tuple[int, int]]:
    """(uid, gid) the gateway should run as, or None to inherit the server's.

    Default ON. Requires the server to be root (else it cannot switch users)
    and the image to ship the runtime user; both hold from template v1.2.1 —
    on anything older this quietly resolves to None and nothing changes.
    """
    if os.environ.get("BOOA_GATEWAY_NONROOT", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    if os.geteuid() != 0:
        return None
    try:
        import pwd
        rec = pwd.getpwnam(RUNTIME_USER)
        return rec.pw_uid, rec.pw_gid
    except (ImportError, KeyError):
        return None


def hand_over_data(hermes_home: str, uid: int, gid: int) -> None:
    """Give the runtime tree to the agent user, keeping server secrets root's.

    lchown, never chown: the agent could plant a symlink into /app and a
    following chown would hand it the server's code. Runs before every gateway
    start so files the (root) server wrote in between — imports, skill syncs,
    config rewrites — are back in the agent's hands, and existing root-owned
    volumes migrate on their first boot after the update.
    """
    home = os.path.dirname(hermes_home) or "/data"

    def give(path: str) -> None:
        try:
            os.lchown(path, uid, gid)
        except OSError:
            pass

    def keep(path: str, mode: int) -> None:
        try:
            os.lchown(path, 0, 0)
            if not os.path.islink(path):
                os.chmod(path, mode)
        except OSError:
            pass

    give(home)
    for root, dirs, files in os.walk(home):
        for name in dirs:
            give(os.path.join(root, name))
        for name in files:
            path = os.path.join(root, name)
            if root == hermes_home and name in _SERVER_OWNED_600:
                keep(path, 0o600)
            elif root == hermes_home and name in _SERVER_OWNED_644:
                keep(path, 0o644)
            else:
                give(path)


class GatewayManager:
    def __init__(self, hermes_home: str):
        self.hermes_home = hermes_home
        self.process: asyncio.subprocess.Process | None = None
        self.log_lines: deque[str] = deque(maxlen=1000)
        self.log_seq = 0
        self._read_task: asyncio.Task | None = None
        self._started_at: float | None = None
        self._recent_errors: list[tuple[float, str]] = []

    def _log(self, text: str):
        self.log_lines.append(text)
        self.log_seq += 1

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def uptime_seconds(self) -> float:
        if not self.is_running or self._started_at is None:
            return 0
        import time
        return time.time() - self._started_at

    async def start(self) -> bool:
        if self.is_running:
            return True

        env = os.environ.copy()
        # The agent reads untrusted input and has shell + file tools. Dashboard
        # credentials are not its business: leaving them in this env makes a prompt
        # injection a path to the admin login and the backup archive password.
        for secret_key in ("ADMIN_PASSWORD", "ADMIN_USERNAME", "BOOA_CONSOLE_ORIGINS",
                           "_BOOA_PW_FD", "_BOOA_ENV_SCRUBBED"):
            env.pop(secret_key, None)
        env["HERMES_HOME"] = self.hermes_home
        env["HOME"] = os.path.dirname(self.hermes_home)
        env["API_SERVER_KEY"] = get_or_create_api_server_key(self.hermes_home)

        spawn_kwargs: dict = {}
        user = runtime_user()
        if user is not None:
            uid, gid = user
            hand_over_data(self.hermes_home, uid, gid)
            # extra_groups=[] drops inherited supplementary groups — without it
            # the child keeps the server's group 0 membership.
            spawn_kwargs = {"user": uid, "group": gid, "extra_groups": []}
            self._log(f"[booa] gateway runs as uid {uid} — server files stay root-owned")

        try:
            self.process = await asyncio.create_subprocess_exec(
                "hermes", "gateway",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=os.path.join(self.hermes_home, "workspace"),
                **spawn_kwargs,
            )
            import time
            self._started_at = time.time()
            self._read_task = asyncio.create_task(self._read_output())
            self._log("[booa] gateway started")
            return True
        except Exception as e:
            self._log(f"[booa] failed to start gateway: {e}")
            return False

    async def stop(self) -> bool:
        if not self.is_running or self.process is None:
            return True

        try:
            self.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

            self._log("[booa] gateway stopped")
            self.process = None
            self._started_at = None

            if self._read_task:
                self._read_task.cancel()
                self._read_task = None

            return True
        except Exception as e:
            self._log(f"[booa] failed to stop gateway: {e}")
            return False

    async def restart(self) -> bool:
        await self.stop()
        return await self.start()

    async def _read_output(self):
        if self.process is None or self.process.stdout is None:
            return
        try:
            async for line in self.process.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    import time
                    self._log(text)
                    # Track recent errors with timestamp
                    if "ERROR" in text or "credit balance" in text or "Invalid token" in text:
                        self._recent_errors.append((time.time(), text))
                        # Keep only last 10
                        if len(self._recent_errors) > 10:
                            self._recent_errors.pop(0)
        except asyncio.CancelledError:
            pass

    def get_recent_errors(self, max_age_seconds: int = 60) -> list[str]:
        """Return errors from the last N seconds only."""
        import time
        now = time.time()
        return [msg for ts, msg in self._recent_errors if now - ts <= max_age_seconds]

    def get_recent_logs(self, n: int = 200) -> list[str]:
        return list(self.log_lines)[-n:]

    async def stream_logs(self) -> AsyncGenerator[str, None]:
        seen = self.log_seq
        while True:
            current = self.log_seq
            if current > seen:
                new = min(current - seen, len(self.log_lines))
                for line in list(self.log_lines)[-new:]:
                    yield line
                seen = current
            await asyncio.sleep(0.5)
