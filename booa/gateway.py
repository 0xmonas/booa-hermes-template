"""Hermes gateway subprocess manager."""

import asyncio
import os
import signal
from collections import deque
from typing import AsyncGenerator

from booa.console_auth import get_or_create_api_server_key


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
        for secret_key in ("ADMIN_PASSWORD", "ADMIN_USERNAME", "BOOA_CONSOLE_ORIGINS"):
            env.pop(secret_key, None)
        env["HERMES_HOME"] = self.hermes_home
        env["HOME"] = os.path.dirname(self.hermes_home)
        env["API_SERVER_KEY"] = get_or_create_api_server_key(self.hermes_home)

        try:
            self.process = await asyncio.create_subprocess_exec(
                "hermes", "gateway",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=os.path.join(self.hermes_home, "workspace"),
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
