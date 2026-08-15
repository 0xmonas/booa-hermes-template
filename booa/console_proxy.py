"""Web console sub-app: proxies an allowlisted slice of the hermes api_server."""

import os
import re

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from booa import console_auth

UPSTREAM = "http://127.0.0.1:8642"
# www is the canonical booa.app origin (the apex 308-redirects to it), so it is the
# Origin browsers actually send. BOOA_CONSOLE_ORIGINS adds extras, comma-separated.
_DEFAULT_ORIGINS = ["https://www.booa.app", "https://booa.app", "http://localhost:3000"]
ALLOWED_ORIGINS = _DEFAULT_ORIGINS + [
    o.strip() for o in os.environ.get("BOOA_CONSOLE_ORIGINS", "").split(",")
    if o.strip().startswith("https://")
]
MAX_PROXY_STREAMS = 6
# Every proxied route carries small JSON (chat messages, session ops); reading
# request.body() unbounded would let one oversized POST OOM the container.
# Import/export live outside this proxy with their own caps.
MAX_PROXY_BODY = 256 * 1024

_PATH_PARAM = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_FORWARD_REQUEST_HEADERS = frozenset({
    "content-type", "accept", "x-hermes-session-id", "x-hermes-session-key", "last-event-id",
})
_FORWARD_RESPONSE_HEADERS = frozenset({"content-type", "x-hermes-session-id"})


def check_console_access(hermes_home: str, auth_limiter, request: Request) -> Response | None:
    if not console_auth.console_enabled(hermes_home):
        return JSONResponse({"error": "console disabled"}, status_code=403)
    ip = console_auth.client_ip(request)
    if auth_limiter.blocked(ip):
        return JSONResponse({"error": "too many requests"}, status_code=429)
    if not console_auth.verify_console_key(hermes_home, request):
        auth_limiter.record_failure(ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


def build_console_app(hermes_home: str, auth_limiter, extra_routes=()) -> Starlette:
    client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(5, read=120))
    stream_client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(5, read=None))
    streams = {"count": 0}

    def proxy(method: str, upstream_path: str, stream: bool = False):
        async def handler(request: Request):
            denied = check_console_access(hermes_home, auth_limiter, request)
            if denied:
                return denied

            path = upstream_path
            for name, value in request.path_params.items():
                value = str(value)
                if not _PATH_PARAM.match(value):
                    return JSONResponse({"error": "invalid path"}, status_code=400)
                path = path.replace("{" + name + "}", value)
            if request.url.query:
                path = f"{path}?{request.url.query}"

            headers = {
                k: v for k, v in request.headers.items()
                if k.lower() in _FORWARD_REQUEST_HEADERS
            }
            headers["Authorization"] = f"Bearer {console_auth.get_or_create_api_server_key(hermes_home)}"
            declared = request.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_PROXY_BODY:
                return JSONResponse({"error": "request too large"}, status_code=413)
            chunks = []
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > MAX_PROXY_BODY:
                    return JSONResponse({"error": "request too large"}, status_code=413)
                chunks.append(chunk)
            body = b"".join(chunks)

            try:
                if stream:
                    if streams["count"] >= MAX_PROXY_STREAMS:
                        return JSONResponse({"error": "too many streams"}, status_code=429)
                    req = stream_client.build_request(method, path, headers=headers, content=body)
                    upstream = await stream_client.send(req, stream=True)
                    streams["count"] += 1

                    async def cleanup():
                        streams["count"] -= 1
                        await upstream.aclose()

                    resp_headers = {
                        k: v for k, v in upstream.headers.items()
                        if k.lower() in _FORWARD_RESPONSE_HEADERS
                    }
                    resp_headers["Cache-Control"] = "no-cache"
                    resp_headers["X-Accel-Buffering"] = "no"
                    return StreamingResponse(
                        upstream.aiter_raw(),
                        status_code=upstream.status_code,
                        headers=resp_headers,
                        background=BackgroundTask(cleanup),
                    )

                upstream = await client.request(method, path, headers=headers, content=body)
                resp_headers = {
                    k: v for k, v in upstream.headers.items()
                    if k.lower() in _FORWARD_RESPONSE_HEADERS
                }
                return Response(upstream.content, status_code=upstream.status_code, headers=resp_headers)
            except httpx.HTTPError:
                return JSONResponse({"error": "agent not reachable — is the gateway running?"}, status_code=502)

        return handler

    routes = [
        Route("/v1/models", proxy("GET", "/v1/models")),
        Route("/v1/capabilities", proxy("GET", "/v1/capabilities")),
        Route("/v1/skills", proxy("GET", "/v1/skills")),
        Route("/v1/toolsets", proxy("GET", "/v1/toolsets")),
        Route("/v1/runs", proxy("POST", "/v1/runs"), methods=["POST"]),
        Route("/v1/runs/{run_id}", proxy("GET", "/v1/runs/{run_id}")),
        Route("/v1/runs/{run_id}/events", proxy("GET", "/v1/runs/{run_id}/events", stream=True)),
        Route("/v1/runs/{run_id}/approval", proxy("POST", "/v1/runs/{run_id}/approval"), methods=["POST"]),
        Route("/v1/runs/{run_id}/stop", proxy("POST", "/v1/runs/{run_id}/stop"), methods=["POST"]),
        Route("/api/sessions", proxy("GET", "/api/sessions")),
        Route("/api/sessions", proxy("POST", "/api/sessions"), methods=["POST"]),
        Route("/api/sessions/{session_id}", proxy("GET", "/api/sessions/{session_id}")),
        Route("/api/sessions/{session_id}", proxy("PATCH", "/api/sessions/{session_id}"), methods=["PATCH"]),
        Route("/api/sessions/{session_id}", proxy("DELETE", "/api/sessions/{session_id}"), methods=["DELETE"]),
        Route("/api/sessions/{session_id}/messages", proxy("GET", "/api/sessions/{session_id}/messages")),
        Route("/api/sessions/{session_id}/fork", proxy("POST", "/api/sessions/{session_id}/fork"), methods=["POST"]),
        Route("/api/sessions/{session_id}/chat", proxy("POST", "/api/sessions/{session_id}/chat"), methods=["POST"]),
        Route("/api/sessions/{session_id}/chat/stream", proxy("POST", "/api/sessions/{session_id}/chat/stream", stream=True), methods=["POST"]),
        Route("/api/model/options", proxy("GET", "/api/model/options")),
        # Cron jobs: manage what already exists (see it, pause, resume, run, remove).
        # Creating/editing jobs is deliberately NOT proxied — a new recurring job is
        # new autonomous activity and belongs behind the wallet-signed approval flow.
        Route("/api/jobs", proxy("GET", "/api/jobs")),
        Route("/api/jobs/{job_id}", proxy("GET", "/api/jobs/{job_id}")),
        Route("/api/jobs/{job_id}", proxy("DELETE", "/api/jobs/{job_id}"), methods=["DELETE"]),
        Route("/api/jobs/{job_id}/pause", proxy("POST", "/api/jobs/{job_id}/pause"), methods=["POST"]),
        Route("/api/jobs/{job_id}/resume", proxy("POST", "/api/jobs/{job_id}/resume"), methods=["POST"]),
        Route("/api/jobs/{job_id}/run", proxy("POST", "/api/jobs/{job_id}/run"), methods=["POST"]),
        *extra_routes,
    ]

    return Starlette(
        routes=routes,
        middleware=[Middleware(
            CORSMiddleware,
            allow_origins=ALLOWED_ORIGINS,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Accept",
                           "X-Hermes-Session-Id", "X-Hermes-Session-Key", "Last-Event-ID"],
            expose_headers=["X-Hermes-Session-Id"],
            max_age=600,
        )],
    )
