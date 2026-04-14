"""Hermes gateway subprocess manager."""

import asyncio
import os
import signal
from collections import deque
from typing import AsyncGenerator


class GatewayManager:
    def __init__(self, hermes_home: str):
        self.hermes_home = hermes_home
        self.process: asyncio.subprocess.Process | None = None
        self.log_lines: deque[str] = deque(maxlen=1000)
        self._read_task: asyncio.Task | None = None
        self._started_at: float | None = None
        self._recent_errors: list[tuple[float, str]] = []

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
        env["HERMES_HOME"] = self.hermes_home
        env["HOME"] = os.path.dirname(self.hermes_home)

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
            self.log_lines.append("[khora] gateway started")
            return True
        except Exception as e:
            self.log_lines.append(f"[khora] failed to start gateway: {e}")
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

            self.log_lines.append("[khora] gateway stopped")
            self.process = None
            self._started_at = None

            if self._read_task:
                self._read_task.cancel()
                self._read_task = None

            return True
        except Exception as e:
            self.log_lines.append(f"[khora] failed to stop gateway: {e}")
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
                    self.log_lines.append(text)
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
        seen = len(self.log_lines)
        while True:
            current = len(self.log_lines)
            if current > seen:
                for line in list(self.log_lines)[seen:current]:
                    yield line
                seen = current
            await asyncio.sleep(0.5)
