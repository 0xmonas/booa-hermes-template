"""Khôra BOOA Hermes Agent — Railway admin server."""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from booa.fetcher import fetch_booa_identity, fetch_skills, TokenNotFound
from booa.writer import (
    ensure_dirs, write_soul, write_identity, write_avatar, write_agent_json,
    write_user_md, write_seed_memory, write_skills, write_config,
    generate_user_md, mark_setup_complete, is_setup_complete,
    write_security_rules, install_output_filter_hook,
)
from booa.gateway import GatewayManager

# Config
HERMES_HOME = os.environ.get("HERMES_HOME", "/data/hermes")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
PORT = int(os.environ.get("PORT", "8080"))

if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"[khora] Admin credentials — username: {ADMIN_USERNAME}  password: {ADMIN_PASSWORD}", flush=True)

# Session secret (persist across restarts)
ensure_dirs(HERMES_HOME)
SECRET_FILE = Path(HERMES_HOME) / ".session-secret"
if SECRET_FILE.exists():
    SESSION_SECRET = SECRET_FILE.read_text().strip()
else:
    SESSION_SECRET = secrets.token_hex(32)
    SECRET_FILE.write_text(SESSION_SECRET)

jinja = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
gateway = GatewayManager(HERMES_HOME)
wizard_data: dict = {}


def load_wizard_data():
    """Restore wizard_data from saved files after restart."""
    if not wizard_data and is_setup_complete(HERMES_HOME):
        # Read agent.json from context if available
        agent_json_path = Path(HERMES_HOME) / "context" / "agent.json"
        if agent_json_path.exists():
            try:
                data = json.loads(agent_json_path.read_text())
                wizard_data.update(data)
            except Exception:
                pass

        # Read config for provider/model info
        config_path = Path(HERMES_HOME) / "config.yaml"
        if config_path.exists():
            try:
                import yaml
                config = yaml.safe_load(config_path.read_text())
                wizard_data["provider"] = config.get("model", {}).get("provider", "?")
                wizard_data["model"] = config.get("model", {}).get("default", "?")
            except Exception:
                pass

        # Read .env for telegram token
        env_path = Path(HERMES_HOME) / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    wizard_data["telegram_token"] = line.split("=", 1)[1]

        # Check installed skills
        skills_dir = Path(HERMES_HOME) / "skills"
        if skills_dir.exists():
            wizard_data["skills_installed"] = [d.name for d in skills_dir.iterdir() if d.is_dir()]


load_wizard_data()


def render(request: Request, name: str, ctx: dict | None = None):
    context = ctx or {}
    context["request"] = request
    return jinja.TemplateResponse(request, name, context)


def require_auth(request: Request) -> bool:
    return request.session.get("authenticated") is True


# ── Routes ────────────────────────────────────────────────────────────────────

async def health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gateway.is_running})


async def index(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login")
    if is_setup_complete(HERMES_HOME):
        return RedirectResponse("/dashboard")
    return RedirectResponse("/wizard")


async def login_page(request: Request):
    return render(request, "login.html", {"error": None})


async def login_submit(request: Request):
    form = await request.form()
    if form.get("username") == ADMIN_USERNAME and form.get("password") == ADMIN_PASSWORD:
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", {"error": "Invalid credentials"})


async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


# ── Wizard ────────────────────────────────────────────────────────────────────

async def wizard_page(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login")
    step = int(request.query_params.get("step", "1"))
    ctx = {"step": step, "data": wizard_data}
    templates_map = {
        1: "wizard/step1_token.html",
        2: "wizard/step2_user.html",
        3: "wizard/step3_provider.html",
        4: "wizard/step4_messaging.html",
    }
    return render(request, templates_map.get(step, templates_map[1]), ctx)


async def wizard_step1(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login")
    form = await request.form()
    token_id = int(form.get("token_id", "0"))

    try:
        booa_data = await fetch_booa_identity(token_id)
        skills = await fetch_skills()

        write_soul(HERMES_HOME, booa_data["soul_md"])
        write_identity(HERMES_HOME, booa_data["identity_md"])
        write_avatar(HERMES_HOME, booa_data["avatar_svg"])
        write_agent_json(HERMES_HOME, booa_data)
        write_seed_memory(HERMES_HOME, booa_data)
        write_skills(HERMES_HOME, skills)
        write_security_rules(HERMES_HOME)
        install_output_filter_hook(HERMES_HOME)

        wizard_data.update(booa_data)
        wizard_data["skills_installed"] = list(skills.keys())

        return RedirectResponse("/wizard?step=2", status_code=303)
    except TokenNotFound:
        return render(request, "wizard/step1_token.html", {
            "step": 1, "data": wizard_data,
            "error": f"BOOA #{token_id} not found. Check the token ID and try again."
        })
    except Exception as e:
        return render(request, "wizard/step1_token.html", {
            "step": 1, "data": wizard_data,
            "error": f"Failed to fetch agent data: {e}"
        })


async def wizard_step2(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login")
    form = await request.form()

    user_md = generate_user_md(
        name=form.get("owner_name", ""),
        token_id=wizard_data.get("token_id", 0),
        agent_name=wizard_data.get("name", ""),
        creature=wizard_data.get("creature", ""),
        language=form.get("language", "English"),
        tasks=form.get("tasks", ""),
        spending_limit=form.get("spending_limit", "0"),
        interests=form.get("interests", ""),
    )
    write_user_md(HERMES_HOME, user_md)

    return RedirectResponse("/wizard?step=3", status_code=303)


async def wizard_step3(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login")
    form = await request.form()

    wizard_data["provider"] = form.get("provider", "openrouter")
    wizard_data["api_key"] = form.get("api_key", "")
    wizard_data["model"] = form.get("model", "anthropic/claude-haiku-4.5")

    return RedirectResponse("/wizard?step=4", status_code=303)


async def wizard_step4(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login")
    form = await request.form()

    write_config(
        HERMES_HOME,
        provider=wizard_data.get("provider", "openrouter"),
        api_key=wizard_data.get("api_key", ""),
        model=wizard_data.get("model", ""),
        telegram_token=form.get("telegram_token", ""),
        telegram_users="",
    )

    mark_setup_complete(HERMES_HOME)
    await gateway.start()

    return RedirectResponse("/dashboard", status_code=303)


# ── Dashboard ─────────────────────────────────────────────────────────────────

async def dashboard_page(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login")
    if not is_setup_complete(HERMES_HOME):
        return RedirectResponse("/wizard")

    avatar_path = Path(HERMES_HOME) / "context" / "avatar.svg"
    avatar_svg = avatar_path.read_text() if avatar_path.exists() else ""

    # Check for wallet info
    wallet_address = ""
    wallet_path = Path("/data/.agent/wallet-info.txt")
    if wallet_path.exists():
        for line in wallet_path.read_text().splitlines():
            if "EVM Address:" in line:
                wallet_address = line.split("EVM Address:")[-1].strip()
                break

    # Check verified + agent wallet status from Khôra API
    verified = None
    agent_wallet_registered = False
    reg_data = {}
    token_id = wizard_data.get("token_id")
    if token_id:
        try:
            import httpx
            resp = httpx.get(
                f"https://khora.fun/api/agent-registry/360/{token_id}",
                timeout=10,
                follow_redirects=True,
            )
            if resp.status_code == 200:
                reg_data = resp.json()
                verified = reg_data.get("verified")
                # Check if an agent wallet is registered in 8004
                # The registeredBy field shows who owns the 8004
                # If wallet_address matches any known field, wallet is registered
                registered_by = reg_data.get("registeredBy", "").lower()
                current_owner = reg_data.get("currentNftOwner", "").lower()
                if wallet_address and wallet_address.lower() in [registered_by, current_owner]:
                    agent_wallet_registered = True
                    registered_agent_wallet = wallet_address
        except Exception:
            pass

    return render(request, "dashboard.html", {
        "data": wizard_data,
        "avatar_svg": avatar_svg,
        "gateway_running": gateway.is_running,
        "uptime": int(gateway.uptime_seconds),
        "wallet_address": wallet_address,
        "verified": verified,
        "agent_wallet_registered": agent_wallet_registered,
        "registered_by": reg_data.get("registeredBy", "") if token_id else "",
        "nft_owner": reg_data.get("currentNftOwner", "") if token_id else "",
    })


async def gateway_start_route(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"started": await gateway.start()})


async def gateway_stop_route(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"stopped": await gateway.stop()})


async def gateway_status(request: Request):
    return JSONResponse({"running": gateway.is_running, "uptime": int(gateway.uptime_seconds)})


async def logs_page(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login")
    return render(request, "logs.html", {"logs": gateway.get_recent_logs()})


async def logs_stream(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    async def generate():
        async for line in gateway.stream_logs():
            yield f"data: {line}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


async def settings_page(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login")
    return render(request, "settings.html", {"data": wizard_data})


PAIRING_DIR = Path(HERMES_HOME) / "platforms" / "pairing"
PAIRING_TTL = 3600


def _read_pairing_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _write_pairing_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, 0o600)


def _pairing_platforms() -> list[str]:
    platforms = set()
    if PAIRING_DIR.exists():
        for f in PAIRING_DIR.glob("*-pending.json"):
            platforms.add(f.name.replace("-pending.json", ""))
    return list(platforms)


async def gateway_errors(request: Request):
    """Return errors from the last 60 seconds only."""
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"errors": gateway.get_recent_errors(60)})


async def pairing_list(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    now = time.time()
    pending_out = []
    approved_out = []

    for platform in _pairing_platforms():
        pending = _read_pairing_json(PAIRING_DIR / f"{platform}-pending.json")
        for code, info in pending.items():
            if now - info.get("created_at", now) <= PAIRING_TTL:
                pending_out.append({
                    "platform": platform,
                    "code": code,
                    "user_id": info.get("user_id", ""),
                    "user_name": info.get("user_name", ""),
                    "age_minutes": int((now - info.get("created_at", now)) / 60),
                })

    # Also check approved users
    if PAIRING_DIR.exists():
        for f in PAIRING_DIR.glob("*-approved.json"):
            platform = f.name.replace("-approved.json", "")
            approved = _read_pairing_json(f)
            for uid, info in approved.items():
                approved_out.append({
                    "platform": platform,
                    "user_id": uid,
                    "user_name": info.get("user_name", ""),
                })

    return JSONResponse({"pending": pending_out, "approved": approved_out})


async def pairing_approve(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    platform = body.get("platform", "")
    code = body.get("code", "").upper().strip()
    if not platform or not code:
        return JSONResponse({"error": "platform and code required"}, status_code=400)

    pending_path = PAIRING_DIR / f"{platform}-pending.json"
    pending = _read_pairing_json(pending_path)
    if code not in pending:
        return JSONResponse({"error": "Code not found"}, status_code=404)

    entry = pending.pop(code)
    _write_pairing_json(pending_path, pending)

    approved_path = PAIRING_DIR / f"{platform}-approved.json"
    approved = _read_pairing_json(approved_path)
    approved[entry["user_id"]] = {
        "user_name": entry.get("user_name", ""),
        "approved_at": time.time(),
    }
    _write_pairing_json(approved_path, approved)

    return JSONResponse({"ok": True, "user_id": entry.get("user_id", ""), "user_name": entry.get("user_name", "")})


async def pairing_deny(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    platform = body.get("platform", "")
    code = body.get("code", "").upper().strip()
    pending_path = PAIRING_DIR / f"{platform}-pending.json"
    pending = _read_pairing_json(pending_path)
    if code in pending:
        del pending[code]
        _write_pairing_json(pending_path, pending)
    return JSONResponse({"ok": True})


async def download_data(request: Request):
    """Download all agent data as ZIP. Requires password re-confirmation."""
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    pw = request.query_params.get("pw", "")
    if pw != ADMIN_PASSWORD:
        return render(request, "dashboard.html", {
            "data": wizard_data,
            "avatar_svg": "",
            "gateway_running": gateway.is_running,
            "uptime": 0,
            "wallet_address": "",
            "verified": None,
            "agent_wallet_registered": False,
            "registered_by": "",
            "nft_owner": "",
        })

    import zipfile
    import io

    buf = io.BytesIO()
    hermes_path = Path(HERMES_HOME)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder in ["memories", "skills", "context", "sessions"]:
            folder_path = hermes_path / folder
            if folder_path.exists():
                for f in folder_path.rglob("*"):
                    if f.is_file():
                        arcname = str(f.relative_to(hermes_path))
                        zf.write(f, arcname)

        # Include SOUL.md and config
        for fname in ["SOUL.md", "config.yaml"]:
            fpath = hermes_path / fname
            if fpath.exists():
                zf.write(fpath, fname)

        # Include wallet info (without mnemonic)
        wallet_path = Path("/data/.agent/wallet-info.txt")
        if wallet_path.exists():
            content = wallet_path.read_text()
            safe_lines = [l for l in content.splitlines()
                         if "mnemonic" not in l.lower()
                         and not (len(l.strip().split()) >= 10 and all(w.isalpha() for w in l.strip().split()))]
            zf.writestr("wallet-info.txt", "\n".join(safe_lines))

        # Include OWS wallet files (encrypted vault — safe to export)
        ows_path = Path("/data/.ows")
        if ows_path.exists():
            for f in ows_path.rglob("*"):
                if f.is_file():
                    arcname = "ows/" + str(f.relative_to(ows_path))
                    zf.write(f, arcname)

    buf.seek(0)
    name = wizard_data.get("name", "agent").lower().replace(" ", "-")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}-data.zip"'},
    )


async def reset_wizard(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login")
    marker = Path(HERMES_HOME) / ".setup-complete"
    if marker.exists():
        marker.unlink()
    await gateway.stop()
    wizard_data.clear()
    return RedirectResponse("/wizard", status_code=303)


# ── App ───────────────────────────────────────────────────────────────────────

routes = [
    Route("/health", health),
    Route("/", index),
    Route("/login", login_page, methods=["GET"]),
    Route("/login", login_submit, methods=["POST"]),
    Route("/logout", logout),
    Route("/wizard", wizard_page, methods=["GET"]),
    Route("/wizard/step1", wizard_step1, methods=["POST"]),
    Route("/wizard/step2", wizard_step2, methods=["POST"]),
    Route("/wizard/step3", wizard_step3, methods=["POST"]),
    Route("/wizard/step4", wizard_step4, methods=["POST"]),
    Route("/dashboard", dashboard_page),
    Route("/gateway/start", gateway_start_route, methods=["POST"]),
    Route("/gateway/stop", gateway_stop_route, methods=["POST"]),
    Route("/gateway/status", gateway_status),
    Route("/logs", logs_page),
    Route("/logs/stream", logs_stream),
    Route("/settings", settings_page),
    Route("/settings/reset", reset_wizard, methods=["POST"]),
    Route("/download", download_data),
    Route("/gateway/errors", gateway_errors),
    Route("/pairing", pairing_list),
    Route("/pairing/approve", pairing_approve, methods=["POST"]),
    Route("/pairing/deny", pairing_deny, methods=["POST"]),
    Mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static"),
]

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    if is_setup_complete(HERMES_HOME):
        install_output_filter_hook(HERMES_HOME)
        print("[khora] Setup complete — auto-starting gateway", flush=True)
        await gateway.start()
    yield
    await gateway.stop()


app = Starlette(
    routes=routes,
    middleware=[Middleware(SessionMiddleware, secret_key=SESSION_SECRET)],
    lifespan=lifespan,
)

if __name__ == "__main__":
    print(f"[khora] Starting on port {PORT}", flush=True)
    print(f"[khora] HERMES_HOME={HERMES_HOME}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
