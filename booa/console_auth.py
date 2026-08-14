"""Keys and auth for the web console (booa.app) surface."""

import hmac
import json
import os
import secrets
import time


def _read_or_create_key(path: str, generate) -> str:
    if os.path.isfile(path):
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    key = generate()
    with open(path, "w") as f:
        f.write(key)
    os.chmod(path, 0o600)
    return key


def get_or_create_api_server_key(hermes_home: str) -> str:
    return _read_or_create_key(
        os.path.join(hermes_home, ".api-server-key"),
        lambda: secrets.token_urlsafe(32),
    )


def get_or_create_console_key(hermes_home: str) -> str:
    return _read_or_create_key(
        os.path.join(hermes_home, ".console-key"),
        lambda: "booa_ck_" + secrets.token_urlsafe(32),
    )


def rotate_console_key(hermes_home: str) -> str:
    path = os.path.join(hermes_home, ".console-key")
    key = "booa_ck_" + secrets.token_urlsafe(32)
    with open(path, "w") as f:
        f.write(key)
    os.chmod(path, 0o600)
    return key


def console_enabled(hermes_home: str) -> bool:
    try:
        with open(os.path.join(hermes_home, ".console-settings.json")) as f:
            return bool(json.load(f).get("enabled"))
    except Exception:
        return False


def set_console_enabled(hermes_home: str, enabled: bool) -> None:
    path = os.path.join(hermes_home, ".console-settings.json")
    with open(path, "w") as f:
        json.dump({"enabled": bool(enabled)}, f)
    os.chmod(path, 0o600)


def verify_console_key(hermes_home: str, request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return False
    supplied = auth[7:].strip()
    if not supplied:
        return False
    expected = get_or_create_console_key(hermes_home)
    return hmac.compare_digest(supplied.encode(), expected.encode())


def client_ip(request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class AuthRateLimiter:
    def __init__(self, max_failures: int = 30, window_seconds: int = 60):
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, list[float]] = {}

    def blocked(self, ip: str) -> bool:
        now = time.time()
        hits = [t for t in self._failures.get(ip, []) if t > now - self.window]
        if hits:
            self._failures[ip] = hits
        elif ip in self._failures:
            del self._failures[ip]
        return len(hits) >= self.max_failures

    def record_failure(self, ip: str) -> None:
        self._failures.setdefault(ip, []).append(time.time())
        if len(self._failures) > 10_000:
            cutoff = time.time() - self.window
            for k in list(self._failures):
                if not any(t > cutoff for t in self._failures[k]):
                    del self._failures[k]
