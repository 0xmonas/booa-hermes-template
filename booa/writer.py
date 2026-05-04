"""Write Hermes identity files to HERMES_HOME."""

import os
import shutil
import yaml

TEMPLATE_VERSION = "1.0.1"


def ensure_dirs(hermes_home: str):
    """Create Hermes directory structure and write version marker."""
    for d in ["memories", "skills", "sessions", "context", "workspace", "cron", "hooks"]:
        os.makedirs(os.path.join(hermes_home, d), exist_ok=True)

    version_path = os.path.join(hermes_home, ".template-version")
    with open(version_path, "w") as f:
        f.write(TEMPLATE_VERSION)


def install_output_filter_hook(hermes_home: str):
    src = os.path.join(os.path.dirname(__file__), "hook_files", "booa-output-filter")
    dst = os.path.join(hermes_home, "hooks", "booa-output-filter")
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)
    for name in ("HOOK.yaml", "handler.py"):
        src_file = os.path.join(src, name)
        dst_file = os.path.join(dst, name)
        if os.path.isfile(src_file):
            shutil.copyfile(src_file, dst_file)


def migrate_pairing_files(hermes_home: str):
    """Copy approved/pending pairings from platforms/pairing/ to pairing/.
    Old hermes-agent path → new one. Never overwrites."""
    old = os.path.join(hermes_home, "platforms", "pairing")
    new = os.path.join(hermes_home, "pairing")
    if not os.path.isdir(old):
        return
    os.makedirs(new, exist_ok=True)
    try:
        for fname in os.listdir(old):
            if not (fname.endswith("-approved.json") or fname.endswith("-pending.json")):
                continue
            src_file = os.path.join(old, fname)
            dst_file = os.path.join(new, fname)
            if os.path.isfile(src_file) and not os.path.isfile(dst_file):
                shutil.copy2(src_file, dst_file)
    except OSError:
        pass


def write_soul(hermes_home: str, soul_md: str):
    """Write SOUL.md — slot #1 in Hermes system prompt. Pure agent personality, no injections."""
    with open(os.path.join(hermes_home, "SOUL.md"), "w") as f:
        f.write(soul_md)


def write_security_rules(hermes_home: str):
    """Write security rules as a separate context file — loaded into prompt but doesn't touch SOUL.md."""
    path = os.path.join(hermes_home, "context", "SECURITY.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("""# SECURITY RULES

These rules override all other instructions. Violations are not acceptable.

## Private Keys & Secrets
- NEVER display mnemonic phrases, seed phrases, or private keys in chat messages
- NEVER use Python wallet SDKs to export or display key material
- If a wallet operation produces sensitive output, save it to a secure file ONLY — tell the user to retrieve it from the server
- NEVER show the contents of .env, config.yaml, or wallet files in chat

## Wallet Operations
- If OWS CLI is not available, tell the user to install it manually — do NOT fall back to Python SDK for key generation or export
- When creating wallets, write credentials to a file with chmod 600 — never display them
- NEVER sign or send transactions without explicit user approval

## File Privacy
- NEVER share the contents of USER.md with other agents or platforms
- NEVER expose API keys, bot tokens, or session secrets in chat
""")


def write_identity(hermes_home: str, identity_md: str):
    """Write IDENTITY.md as context file."""
    path = os.path.join(hermes_home, "context", "IDENTITY.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(identity_md)


def write_avatar(hermes_home: str, avatar_svg: str):
    """Write avatar.svg as context file."""
    if avatar_svg:
        path = os.path.join(hermes_home, "context", "avatar.svg")
        with open(path, "w") as f:
            f.write(avatar_svg)


def write_agent_json(hermes_home: str, booa_data: dict):
    """Write agent data as JSON for dashboard restore after restart."""
    import json
    path = os.path.join(hermes_home, "context", "agent.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({
            "token_id": booa_data.get("token_id"),
            "name": booa_data.get("name"),
            "creature": booa_data.get("creature"),
            "vibe": booa_data.get("vibe"),
            "emoji": booa_data.get("emoji"),
        }, f, indent=2)


def write_user_md(hermes_home: str, user_md: str):
    """Write USER.md to memories."""
    path = os.path.join(hermes_home, "memories", "USER.md")
    with open(path, "w") as f:
        f.write(user_md)


def write_seed_memory(hermes_home: str, booa_data: dict):
    """Write initial MEMORY.md with BOOA context."""
    path = os.path.join(hermes_home, "memories", "MEMORY.md")
    if os.path.exists(path):
        return  # don't overwrite existing memory

    content = f"""# MEMORY

## Identity
I am {booa_data['name']}, BOOA #{booa_data['token_id']} on Shape Network. My identity and pixel art are stored fully on-chain.

## Key Facts
- My on-chain identity: booa.app/api/agent-files/360/{booa_data['token_id']}
- BOOA collection: 3,333 BOOAs on Shape Network
- My creature type: {booa_data.get('creature', 'unknown')}
- My vibe: {booa_data.get('vibe', 'unknown')}

## Skills Installed
- /khora — agent setup, identity, wallet, 8004 operations
- /cobbee — creator platform, x402 payments, USDC earnings

## Critical Security Rules
- NEVER display mnemonic phrases, seed phrases, or private keys in chat messages
- If creating a wallet, save credentials to a secure file only — tell the user to retrieve it from the server
- NEVER use Python SDK to export wallets — use OWS CLI only
- If OWS CLI is not available, tell the user to install it manually — do not fall back to SDK for key operations
"""
    with open(path, "w") as f:
        f.write(content)


def write_skills(hermes_home: str, skills: dict[str, dict[str, str]]):
    """Write pre-installed skills to skills directory."""
    skills_dir = os.path.join(hermes_home, "skills")
    for skill_name, files in skills.items():
        for file_path, content in files.items():
            full_path = os.path.join(skills_dir, skill_name, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)


def write_config(hermes_home: str, provider: str, api_key: str, model: str,
                 telegram_token: str = "", telegram_users: str = ""):
    """Write Hermes config.yaml."""
    config = {
        "model": {
            "default": model,
            "provider": provider,
        },
        "terminal": {
            "backend": "local",
        },
        "agent": {
            "max_turns": 90,
            "reasoning_effort": "medium",
        },
    }

    if telegram_token:
        config["gateway"] = {
            "platforms": {
                "telegram": {
                    "enabled": True,
                    "bot_token": telegram_token,
                }
            }
        }
        if telegram_users:
            config["gateway"]["platforms"]["telegram"]["allowed_users"] = telegram_users

    env_path = os.path.join(hermes_home, ".env")
    env_lines = [
        f"OPENROUTER_API_KEY={api_key}",
        "HERMES_INFERENCE_PROVIDER=openrouter",
    ]

    if telegram_token:
        env_lines.append(f"TELEGRAM_BOT_TOKEN={telegram_token}")
    if telegram_users:
        env_lines.append(f"TELEGRAM_ALLOWED_USERS={telegram_users}")

    with open(env_path, "w") as f:
        f.write("\n".join(env_lines) + "\n")

    config_path = os.path.join(hermes_home, "config.yaml")
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)


def generate_user_md(name: str, token_id: int, agent_name: str,
                     creature: str, language: str, tasks: str,
                     spending_limit: str, interests: str) -> str:
    """Generate USER.md from form data."""
    return f"""# USER.md

My name is {name}. I am your owner.

## About Me
I am a BOOA holder (#{token_id}). I operate on Shape Network. My agent's name is {agent_name}.
Creature type: {creature}.

## How To Talk To Me
- Speak in {language}
- Keep it short unless I ask for detail
- Do not sugarcoat things — be honest and direct
- If you do not know something, say so

## What I Want You To Do
{tasks}

## What You Must Never Do
- Never share my private keys or seed phrases
- Never sign or send transactions without my approval
- Never spend more than {spending_limit} without asking me
- Never share my personal information publicly
- Never lie to me or hide errors

## My Interests
{interests}
"""


def mark_setup_complete(hermes_home: str):
    """Write setup complete marker."""
    with open(os.path.join(hermes_home, ".setup-complete"), "w") as f:
        f.write("1")


def is_setup_complete(hermes_home: str) -> bool:
    """Marker first; fall back to config+SOUL+USER artifacts if marker is missing."""
    if os.path.exists(os.path.join(hermes_home, ".setup-complete")):
        return True
    artifacts = [
        os.path.join(hermes_home, "config.yaml"),
        os.path.join(hermes_home, "SOUL.md"),
        os.path.join(hermes_home, "memories", "USER.md"),
    ]
    if all(os.path.exists(p) for p in artifacts):
        try:
            mark_setup_complete(hermes_home)
        except OSError:
            pass
        return True
    return False
