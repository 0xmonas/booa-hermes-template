"""BOOA Hermes Agent — Railway admin server."""

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from booa.fetcher import fetch_booa_identity, fetch_skills, TokenNotFound
from booa.writer import (
    ensure_dirs, write_soul, write_identity, write_avatar, write_agent_json,
    write_user_md, write_seed_memory, write_skills, write_config,
    generate_user_md, mark_setup_complete, is_setup_complete,
    write_security_rules, install_output_filter_hook, migrate_pairing_files,
    refresh_mcp_config, ensure_api_server_platform, TEMPLATE_VERSION,
)
from booa import wallet_status
from booa import agent_wallet_link
from booa import backup
from booa import console_auth
from booa import output_filter
from booa.console_proxy import build_console_app, check_console_access
from booa.gateway import GatewayManager

def _reexec_without_admin_password():
    """Opt-in (BOOA_SCRUB_ENV=1): re-exec so the admin password leaves this process's
    environment block, then receive it over an inherited pipe.

    The agent runs as the same user and can read /proc/1/environ, which is this
    process — one shell line would hand it the dashboard password. execve rebuilds
    the environment block, so after this the password is not in it. Returns None
    when the flag is off, leaving startup byte-identical to before."""
    # Only when this file is the process being run. Re-execing on import would turn
    # any process that merely imports this module into a running server.
    if __name__ != "__main__" or os.environ.get("BOOA_SCRUB_ENV") != "1":
        return None

    if os.environ.get("_BOOA_ENV_SCRUBBED") == "1":
        fd = int(os.environ.get("_BOOA_PW_FD", "-1"))
        if fd < 0:
            return None
        try:
            pw = os.read(fd, 4096).decode()
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        return pw

    pw = os.environ.get("ADMIN_PASSWORD", "")
    if not pw:
        return None
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    os.write(write_fd, pw.encode())
    os.close(write_fd)
    env = {k: v for k, v in os.environ.items() if k != "ADMIN_PASSWORD"}
    env["_BOOA_ENV_SCRUBBED"] = "1"
    env["_BOOA_PW_FD"] = str(read_fd)
    os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)], env)


_scrubbed_pw = _reexec_without_admin_password()

# Config
HERMES_HOME = os.environ.get("HERMES_HOME", "/data/hermes")
# BOOA's canonical home is Ethereum (chain 1) post-migration. Override only if a
# BOOA still lives on Shape (360) and hasn't migrated.
BOOA_CHAIN_ID = int(os.environ.get("BOOA_CHAIN_ID", "1"))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _scrubbed_pw if _scrubbed_pw is not None else os.environ.get("ADMIN_PASSWORD", "")
PORT = int(os.environ.get("PORT", "8080"))

if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"[booa] Admin credentials — username: {ADMIN_USERNAME}  password: {ADMIN_PASSWORD}", flush=True)

# Session secret (persist across restarts)
ensure_dirs(HERMES_HOME)
SECRET_FILE = Path(HERMES_HOME) / ".session-secret"
if SECRET_FILE.exists():
    SESSION_SECRET = SECRET_FILE.read_text().strip()
else:
    SESSION_SECRET = secrets.token_hex(32)
    SECRET_FILE.write_text(SESSION_SECRET)

jinja = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
jinja.env.globals["template_version"] = TEMPLATE_VERSION
gateway = GatewayManager(HERMES_HOME)
auth_limiter = console_auth.AuthRateLimiter()
login_limiter = console_auth.AuthRateLimiter(max_failures=20, window_seconds=60)
LOGIN_THROTTLE_SECONDS = float(os.environ.get("BOOA_LOGIN_THROTTLE_SECONDS", "2"))
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
    context.setdefault("authed", require_auth(request))
    return jinja.TemplateResponse(request, name, context)


def _password_epoch() -> str:
    """Session validity is tied to the current password, so rotating
    ADMIN_PASSWORD immediately invalidates every session that used the old one."""
    return hashlib.sha256(f"booa-session:{ADMIN_PASSWORD}".encode()).hexdigest()[:16]


def require_auth(request: Request) -> bool:
    if request.session.get("authenticated") is not True:
        return False
    return secrets.compare_digest(str(request.session.get("pw") or ""), _password_epoch())


# ── Routes ────────────────────────────────────────────────────────────────────

async def health(request: Request):
    return JSONResponse({"status": "ok"})


async def index(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login")
    if is_setup_complete(HERMES_HOME):
        return RedirectResponse("/dashboard")
    return RedirectResponse("/wizard")


async def login_page(request: Request):
    return render(request, "login.html", {"error": None})


async def login_submit(request: Request):
    ip = console_auth.client_ip(request)
    form = await request.form()
    user_ok = secrets.compare_digest(str(form.get("username") or "").encode(), ADMIN_USERNAME.encode())
    pass_ok = secrets.compare_digest(str(form.get("password") or "").encode(), ADMIN_PASSWORD.encode())

    # The correct password is never rejected: throttling a valid login would let
    # anyone lock the operator out of their own agent by spamming wrong guesses.
    if user_ok and pass_ok:
        request.session["authenticated"] = True
        request.session["pw"] = _password_epoch()
        return RedirectResponse("/", status_code=303)

    auth_limiter.record_failure(ip)
    login_limiter.record_failure("global")
    # Wrong guesses pay a delay instead, so guessing stays slow without a lockout.
    if auth_limiter.blocked(ip) or login_limiter.blocked("global"):
        await asyncio.sleep(LOGIN_THROTTLE_SECONDS)
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
    try:
        wallet_status.refresh(HERMES_HOME, BOOA_CHAIN_ID, int(wizard_data.get("token_id", 0)))
    except Exception:
        pass
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

    # Check verified + agent wallet status from BOOA API
    verified = None
    agent_wallet_registered = False
    reg_data = {}
    token_id = wizard_data.get("token_id")
    if token_id:
        try:
            import httpx
            resp = httpx.get(
                f"https://booa.app/api/agent-registry/1/{token_id}",
                timeout=10,
                follow_redirects=True,
            )
            if resp.status_code == 200:
                reg_data = resp.json()
                verified = reg_data.get("verified")
                # The runtime wallet is "linked" when the onchain agent wallet
                # (adapter.getAgentWallet, exposed as `agentWallet`) equals this
                # wallet. Bound agents register it via adapter.setAgentWallet.
                onchain_agent_wallet = (reg_data.get("agentWallet") or "").lower()
                if wallet_address and onchain_agent_wallet and wallet_address.lower() == onchain_agent_wallet:
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
        "agent_wallet": reg_data.get("agentWallet", "") if token_id else "",
        "bound": reg_data.get("bound", False) if token_id else False,
        "controller": reg_data.get("controller", "") if token_id else "",
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
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
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


# Hermes moved pairing from platforms/pairing/ to pairing/ upstream.
_PAIRING_DIR_NEW = Path(HERMES_HOME) / "pairing"
_PAIRING_DIR_OLD = Path(HERMES_HOME) / "platforms" / "pairing"


class _PairingDir:
    def _resolve(self) -> Path:
        if _PAIRING_DIR_NEW.exists() and any(_PAIRING_DIR_NEW.glob("*.json")):
            return _PAIRING_DIR_NEW
        if _PAIRING_DIR_OLD.exists() and any(_PAIRING_DIR_OLD.glob("*.json")):
            return _PAIRING_DIR_OLD
        return _PAIRING_DIR_NEW

    def exists(self) -> bool:
        return self._resolve().exists()

    def glob(self, pattern: str):
        return self._resolve().glob(pattern)

    def __truediv__(self, other: str) -> Path:
        return self._resolve() / other


PAIRING_DIR = _PairingDir()
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


async def _do_export(password: str):
    if not secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        return JSONResponse({"error": "invalid password"}, status_code=400)
    load_wizard_data()
    tc = _token_chain_from_wizard()
    token_id, chain_id = tc if tc is not None else (None, BOOA_CHAIN_ID)
    name = wizard_data.get("name", "agent").lower().replace(" ", "-")
    path = await asyncio.to_thread(
        backup.create_backup_zip, HERMES_HOME, password,
        token_id=token_id, chain_id=chain_id, agent_name=name,
        hermes_pin=os.environ.get("HERMES_PIN", ""),
    )
    date = time.strftime("%Y%m%d")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"{name}-backup-{date}.zip",
        background=BackgroundTask(os.unlink, path),
    )


async def download_data(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    form = await request.form()
    return await _do_export(str(form.get("password") or ""))


async def _do_import(request: Request):
    form = await request.form()
    admin_password = str(form.get("admin_password") or "")
    if not secrets.compare_digest(admin_password.encode(), ADMIN_PASSWORD.encode()):
        return JSONResponse({"error": "invalid archive or password"}, status_code=400)
    archive_password = str(form.get("archive_password") or "") or admin_password

    upload = form.get("archive")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "missing archive"}, status_code=400)

    tmp = Path(HERMES_HOME) / f".import-upload-{int(time.time())}.zip"
    written = 0
    try:
        with open(tmp, "wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > backup.MAX_ARCHIVE_BYTES:
                    return JSONResponse({"error": "archive too large"}, status_code=400)
                out.write(chunk)

        load_wizard_data()
        tc = _token_chain_from_wizard()
        instance_token_id = tc[0] if tc is not None else None

        await gateway.stop()
        result = await asyncio.to_thread(
            backup.restore_backup, HERMES_HOME, str(tmp), archive_password,
            instance_token_id=instance_token_id,
            confirm_token_mismatch=str(form.get("confirm_token_mismatch") or "") == "1",
            restore_wallet=str(form.get("restore_wallet") or "") == "1",
        )
        status = result.pop("status", 200)
        if result.get("ok"):
            ensure_api_server_platform(HERMES_HOME)
            refresh_mcp_config(HERMES_HOME)
            install_output_filter_hook(HERMES_HOME)
            wizard_data.clear()
            load_wizard_data()
            tc = _token_chain_from_wizard()
            if tc is not None:
                try:
                    wallet_status.refresh(HERMES_HOME, tc[1], tc[0])
                except Exception:
                    pass
        await gateway.start()
        result["gateway_running"] = gateway.is_running
        return JSONResponse(result, status_code=status)
    finally:
        tmp.unlink(missing_ok=True)


async def import_data(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _do_import(request)


async def console_export(request: Request):
    denied = check_console_access(HERMES_HOME, auth_limiter, request)
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)
    return await _do_export(str(body.get("password") or ""))


async def console_import(request: Request):
    denied = check_console_access(HERMES_HOME, auth_limiter, request)
    if denied:
        return denied
    return await _do_import(request)


async def reset_wizard(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login")
    marker = Path(HERMES_HOME) / ".setup-complete"
    if marker.exists():
        marker.unlink()
    await gateway.stop()
    wizard_data.clear()
    return RedirectResponse("/wizard", status_code=303)


# ── Wallet status / verification ───────────────────────────────────────────────

def _token_chain_from_wizard() -> tuple[int, int] | None:
    tok = wizard_data.get("token_id")
    if not tok:
        return None
    try:
        return int(tok), BOOA_CHAIN_ID
    except (TypeError, ValueError):
        return None


async def wallet_status_get(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    state = wallet_status.read_state(HERMES_HOME)
    if state is None:
        return JSONResponse({"state": "unknown", "message": "no state yet"})
    return JSONResponse(vars(state))


async def wallet_refresh(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    tc = _token_chain_from_wizard()
    if tc is None:
        return JSONResponse({"error": "setup incomplete"}, status_code=400)
    token_id, chain_id = tc
    state = wallet_status.refresh(HERMES_HOME, chain_id, token_id)
    return JSONResponse(vars(state))


async def wallet_challenge_create(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    tc = _token_chain_from_wizard()
    if tc is None:
        return JSONResponse({"error": "setup incomplete"}, status_code=400)
    token_id, chain_id = tc
    info = wallet_status._read_local_wallet_info(HERMES_HOME)
    payload = wallet_status.create_challenge(HERMES_HOME, chain_id, token_id)
    return JSONResponse({
        "suggested_wallet": info["address"] if info else None,
        "suggested_name": (info.get("name") if info else None) or "my-agent",
        **payload,
    })


async def wallet_verify_post(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    tc = _token_chain_from_wizard()
    if tc is None:
        return JSONResponse({"error": "setup incomplete"}, status_code=400)
    token_id, chain_id = tc
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    nonce = body.get("nonce")
    signature = body.get("signature")
    if not nonce or not signature:
        return JSONResponse({"error": "nonce and signature required"}, status_code=400)
    result = wallet_status.verify_challenge(HERMES_HOME, chain_id, token_id, nonce, signature)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


async def wallet_link_code_post(request: Request):
    """Produce the setAgentWallet link code the operator pastes into the BOOA Bridge."""
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    tc = _token_chain_from_wizard()
    if tc is None:
        return JSONResponse({"ok": False, "error": "setup incomplete"}, status_code=400)
    token_id, chain_id = tc
    info = wallet_status._read_local_wallet_info(HERMES_HOME)
    if not info or not info.get("address"):
        return JSONResponse({"ok": False, "error": "No agent wallet yet. Create one with OWS first."}, status_code=400)
    result = agent_wallet_link.build_link_blob(
        chain_id, token_id, info.get("name") or "my-agent", info["address"],
    )
    if result.get("ok") and result.get("url"):
        result["qr"] = _qr_svg_datauri(result["url"])
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


def _qr_svg_datauri(data: str):
    """Render a QR as a self-contained SVG data-URI (no external calls). None if unavailable."""
    try:
        import base64 as _b64
        import qrcode
        qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        n = len(matrix)
        rects = "".join(
            f'<rect x="{x}" y="{y}" width="1" height="1"/>'
            for y, row in enumerate(matrix) for x, cell in enumerate(row) if cell
        )
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" '
            f'shape-rendering="crispEdges"><rect width="{n}" height="{n}" fill="#fff"/>'
            f'<g fill="#000">{rects}</g></svg>'
        )
        return "data:image/svg+xml;base64," + _b64.b64encode(svg.encode()).decode()
    except Exception:
        return None


# Operator-editable onchain/trading settings (override the same-named env vars).
# Secrets are NOT here on purpose: API keys belong in Railway → Variables.
ONCHAIN_KEYS = [
    "BOOA_ONCHAIN_MCP", "BOOA_ONCHAIN_WRITES", "BOOA_MAX_TX_ETH", "BOOA_DAILY_CAP_ETH",
    "BOOA_SEND_ALLOWLIST", "BOOA_SWAP_TOKEN_ALLOWLIST", "BOOA_MAX_SLIPPAGE_BPS",
    "BOOA_OPENSEA_MCP", "BOOA_OPENSEA_REQUIRE_VERIFIED",
]
_ONCHAIN_PATH = os.path.join(HERMES_HOME, "onchain-settings.json")


def _sync_secret_env_keys():
    """Upsert secrets from the process env (Railway Variables) into HERMES_HOME/.env."""
    env_path = Path(HERMES_HOME) / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text().splitlines()
        changed = False
        for key in ("OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN"):
            val = os.environ.get(key)
            if not val:
                continue
            line = f"{key}={val}"
            for i, existing in enumerate(lines):
                if existing.startswith(key + "="):
                    if existing != line:
                        lines[i] = line
                        changed = True
                    break
            else:
                lines.append(line)
                changed = True
        if changed:
            env_path.write_text("\n".join(lines) + "\n")
            print("[booa] synced secret keys from Railway env into .env", flush=True)
    except OSError:
        pass


def _scrub_onchain_secret():
    try:
        p = Path(_ONCHAIN_PATH)
        if p.exists():
            d = json.loads(p.read_text())
            if d.pop("OPENSEA_API_KEY", None) is not None:
                p.write_text(json.dumps(d, indent=2))
    except Exception:
        pass


def _read_onchain_settings() -> dict:
    try:
        with open(_ONCHAIN_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


async def onchain_settings_get(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cur = _read_onchain_settings()
    out = {k: cur.get(k, os.environ.get(k, "")) for k in ONCHAIN_KEYS}
    # Mirror the runtime default: verified-only buying is ON unless explicitly
    # disabled. Without this the checkbox renders unchecked and a plain Save
    # would silently switch the scam guard off.
    if out["BOOA_OPENSEA_REQUIRE_VERIFIED"] == "":
        out["BOOA_OPENSEA_REQUIRE_VERIFIED"] = "1"
    out["OPENSEA_API_KEY_set"] = bool(os.environ.get("OPENSEA_API_KEY"))
    return JSONResponse(out)


async def onchain_settings_post(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    form = await request.form()
    cur = _read_onchain_settings()
    cur.pop("OPENSEA_API_KEY", None)  # secrets live in Railway env, never here
    for k in ONCHAIN_KEYS:
        cur[k] = (form.get(k) or "").strip()
    with open(_ONCHAIN_PATH, "w") as f:
        json.dump(cur, f, indent=2)
    try:
        os.chmod(_ONCHAIN_PATH, 0o600)
    except OSError:
        pass
    refresh_mcp_config(HERMES_HOME)  # rebuild config.yaml mcp_servers from the new settings
    return JSONResponse({"ok": True, "note": "Saved. Limits and allowlists apply immediately. "
                         "The enable toggles (read tools, trading, OpenSea) take effect after you restart the gateway. "
                         "API keys are set in Railway → Variables."})


async def console_config_get(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({
        "enabled": console_auth.console_enabled(HERMES_HOME),
        "key": console_auth.get_or_create_console_key(HERMES_HOME),
    })


async def console_config_post(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)
    action = body.get("action")
    if action == "enable":
        console_auth.set_console_enabled(HERMES_HOME, True)
    elif action == "disable":
        console_auth.set_console_enabled(HERMES_HOME, False)
    elif action == "rotate":
        console_auth.rotate_console_key(HERMES_HOME)
    else:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    return JSONResponse({
        "enabled": console_auth.console_enabled(HERMES_HOME),
        "key": console_auth.get_or_create_console_key(HERMES_HOME),
    })


# ── Web console (booa.app) ────────────────────────────────────────────────────

_hermes_health_cache: dict = {"ts": 0.0, "version": None}
_restart_lock = asyncio.Lock()
_log_stream_count = {"count": 0}


async def _hermes_version() -> str | None:
    now = time.time()
    if now - _hermes_health_cache["ts"] < 60:
        return _hermes_health_cache["version"]
    version = None
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            r = await c.get("http://127.0.0.1:8642/health")
            if r.status_code == 200:
                version = r.json().get("version")
    except Exception:
        pass
    _hermes_health_cache.update(ts=now, version=version)
    return version


async def console_meta(request: Request):
    denied = check_console_access(HERMES_HOME, auth_limiter, request)
    if denied:
        return denied
    load_wizard_data()
    tc = _token_chain_from_wizard()
    token_id, chain_id = tc if tc is not None else (None, BOOA_CHAIN_ID)
    return JSONResponse({
        "template_version": TEMPLATE_VERSION,
        "hermes_version": await _hermes_version(),
        "hermes_pin": os.environ.get("HERMES_PIN", ""),
        "token_id": token_id,
        "chain_id": chain_id,
        "agent_name": wizard_data.get("name", ""),
        "gateway": {"running": gateway.is_running, "uptime": int(gateway.uptime_seconds)},
        "console": {"enabled": True},
    })


async def console_gateway_restart(request: Request):
    denied = check_console_access(HERMES_HOME, auth_limiter, request)
    if denied:
        return denied
    async with _restart_lock:
        ok = await gateway.restart()
    return JSONResponse({"ok": bool(ok), "running": gateway.is_running})


async def console_logs_stream(request: Request):
    denied = check_console_access(HERMES_HOME, auth_limiter, request)
    if denied:
        return denied
    if _log_stream_count["count"] >= 2:
        return JSONResponse({"error": "too many streams"}, status_code=429)

    private_hashes = output_filter.compute_file_hashes([
        os.path.join(HERMES_HOME, "memories", "USER.md"),
        os.path.join(HERMES_HOME, "memories", "MEMORY.md"),
        os.path.join(HERMES_HOME, ".env"),
        os.path.join(HERMES_HOME, "secrets.txt"),
    ])
    deny = [
        console_auth.get_or_create_console_key(HERMES_HOME),
        console_auth.get_or_create_api_server_key(HERMES_HOME),
        ADMIN_PASSWORD,
    ]
    for _k in ("OPENROUTER_API_KEY", "OWS_PASSPHRASE", "OPENSEA_API_KEY",
               "ETH_RPC", "BASE_RPC", "TELEGRAM_BOT_TOKEN"):
        _v = os.environ.get(_k)
        if _v and len(_v) >= 8:
            deny.append(_v)

    def clean(line: str) -> str:
        return output_filter.filter_output(
            line, channel="console-logs",
            private_file_hashes=private_hashes, deny_list=deny,
        ).text

    async def generate():
        _log_stream_count["count"] += 1
        try:
            for line in gateway.get_recent_logs(200):
                yield f"data: {clean(line)}\n\n"
            seen = gateway.log_seq
            last_emit = time.time()
            while True:
                current = gateway.log_seq
                if current > seen:
                    new = min(current - seen, len(gateway.log_lines))
                    for line in list(gateway.log_lines)[-new:]:
                        yield f"data: {clean(line)}\n\n"
                    seen = current
                    last_emit = time.time()
                elif time.time() - last_emit >= 15:
                    yield ": keepalive\n\n"
                    last_emit = time.time()
                await asyncio.sleep(0.5)
        finally:
            _log_stream_count["count"] -= 1

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    Route("/api/onchain-settings", onchain_settings_get),
    Route("/api/onchain-settings", onchain_settings_post, methods=["POST"]),
    Route("/api/console/config", console_config_get),
    Route("/api/console/config", console_config_post, methods=["POST"]),
    Route("/download", download_data, methods=["POST"]),
    Route("/import", import_data, methods=["POST"]),
    Route("/gateway/errors", gateway_errors),
    Route("/pairing", pairing_list),
    Route("/pairing/approve", pairing_approve, methods=["POST"]),
    Route("/pairing/deny", pairing_deny, methods=["POST"]),
    Route("/api/wallet/status", wallet_status_get),
    Route("/api/wallet/refresh", wallet_refresh, methods=["POST"]),
    Route("/api/wallet/challenge", wallet_challenge_create, methods=["POST"]),
    Route("/api/wallet/verify", wallet_verify_post, methods=["POST"]),
    Route("/api/wallet/link-code", wallet_link_code_post, methods=["POST"]),
    Mount("/console", app=build_console_app(HERMES_HOME, auth_limiter, extra_routes=[
        Route("/meta", console_meta),
        Route("/gateway/restart", console_gateway_restart, methods=["POST"]),
        Route("/logs/stream", console_logs_stream),
        Route("/export", console_export, methods=["POST"]),
        Route("/import", console_import, methods=["POST"]),
    ])),
    Mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static"),
]

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    migrate_pairing_files(HERMES_HOME)
    if is_setup_complete(HERMES_HOME):
        install_output_filter_hook(HERMES_HOME)
        # Refresh the security policy on every boot so existing deployments pick up
        # security updates on the next restart/redeploy. Safe to overwrite: SECURITY.md
        # is our static hardcoded policy, never operator data or the NFT-derived SOUL.
        write_security_rules(HERMES_HOME)
        # Keep config.yaml mcp_servers in sync with the operator's onchain-settings.json.
        ensure_api_server_platform(HERMES_HOME)
        refresh_mcp_config(HERMES_HOME)
        # Railway Variables are the source of truth for secrets: sync them into the
        # Hermes .env on every boot, so a key set or rotated in Railway takes effect
        # after a restart (there is deliberately no dashboard input for secrets).
        _sync_secret_env_keys()
        # Migration: secrets no longer live in onchain-settings.json.
        _scrub_onchain_secret()
        # Refresh OUR managed skills (booa, cobbee, bundled ows) on every boot so
        # stale copies on old volumes get updated — e.g. pre-rebrand "khora" docs.
        # Best-effort: offline boots keep whatever is on the volume. Skills the
        # operator installed themselves are never touched.
        try:
            skills = await fetch_skills()
            write_skills(HERMES_HOME, skills)
            legacy = Path(HERMES_HOME) / "skills" / "khora"
            if skills.get("booa") and legacy.is_dir():
                shutil.rmtree(legacy, ignore_errors=True)
                print("[booa] migrated legacy 'khora' skill to 'booa'", flush=True)
        except Exception as exc:
            print(f"[booa] skill refresh failed: {exc}", flush=True)
        tc = _token_chain_from_wizard()
        if tc is not None:
            try:
                token_id, chain_id = tc
                wallet_status.refresh(HERMES_HOME, chain_id, token_id)
            except Exception as exc:
                print(f"[booa] wallet status refresh failed: {exc}", flush=True)
        print("[booa] Setup complete — auto-starting gateway", flush=True)
        await gateway.start()
    yield
    await gateway.stop()


app = Starlette(
    routes=routes,
    middleware=[Middleware(
        SessionMiddleware,
        secret_key=SESSION_SECRET,
        https_only=os.environ.get("BOOA_INSECURE_COOKIES") != "1",
        same_site="lax",
        max_age=24 * 60 * 60,
    )],
    lifespan=lifespan,
)

if __name__ == "__main__":
    print(f"[booa] Starting on port {PORT}", flush=True)
    print(f"[booa] HERMES_HOME={HERMES_HOME}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
