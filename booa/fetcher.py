"""Fetch BOOA identity files from booa.app API."""

import os
from pathlib import Path

import httpx

BOOA_API = "https://booa.app/api/agent-files/360"
BOOA_SKILLS_URL = "https://booa.app/skills"
COBBEE_SKILLS_URL = "https://cobbee.fun/skills"

# Local skills bundled with the template (read from repo at build time).
LOCAL_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


class TokenNotFound(Exception):
    pass


async def fetch_booa_identity(token_id: int) -> dict:
    """Fetch all identity files for a BOOA token."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        soul = await client.get(f"{BOOA_API}/{token_id}/soul.md")
        identity = await client.get(f"{BOOA_API}/{token_id}/identity.md")
        avatar = await client.get(f"{BOOA_API}/{token_id}/avatar.svg")
        agent_json = await client.get(f"{BOOA_API}/{token_id}/agent.json")

    if soul.status_code == 404:
        raise TokenNotFound(f"BOOA #{token_id} not found")

    agent_data = agent_json.json() if agent_json.status_code == 200 else {}

    return {
        "soul_md": soul.text,
        "identity_md": identity.text,
        "avatar_svg": avatar.text if avatar.status_code == 200 else "",
        "agent_json": agent_data,
        "token_id": token_id,
        "name": agent_data.get("name", f"BOOA #{token_id}"),
        "creature": agent_data.get("creature", ""),
        "vibe": agent_data.get("vibe", ""),
        "emoji": agent_data.get("emoji", ""),
    }


SKILL_URLS = {
    "khora": {
        "SKILL.md": f"{BOOA_SKILLS_URL}/SKILL.md",
        "references/wallet-setup.md": f"{BOOA_SKILLS_URL}/references/wallet-setup.md",
    },
    "cobbee": {
        "SKILL.md": f"{COBBEE_SKILLS_URL}/SKILL.md",
        "references/wallet-setup.md": f"{COBBEE_SKILLS_URL}/references/wallet-setup.md",
        "references/authentication.md": f"{COBBEE_SKILLS_URL}/references/authentication.md",
        "references/profile.md": f"{COBBEE_SKILLS_URL}/references/profile.md",
        "references/support.md": f"{COBBEE_SKILLS_URL}/references/support.md",
        "references/products.md": f"{COBBEE_SKILLS_URL}/references/products.md",
        "references/discovery.md": f"{COBBEE_SKILLS_URL}/references/discovery.md",
        "references/error-handling.md": f"{COBBEE_SKILLS_URL}/references/error-handling.md",
        "references/api-endpoints.md": f"{COBBEE_SKILLS_URL}/references/api-endpoints.md",
    },
}


def load_local_skills() -> dict[str, dict[str, str]]:
    """Load skills bundled in the repo (skills/<name>/**). Independent of network."""
    results: dict[str, dict[str, str]] = {}
    if not LOCAL_SKILLS_DIR.is_dir():
        return results
    for skill_dir in LOCAL_SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        files: dict[str, str] = {}
        for file_path in skill_dir.rglob("*"):
            if file_path.is_file():
                rel = file_path.relative_to(skill_dir).as_posix()
                files[rel] = file_path.read_text()
        if files:
            results[skill_dir.name] = files
    return results


async def fetch_skills() -> dict[str, dict[str, str]]:
    """Fetch all pre-installed skills — remote (BOOA, Cobbee) merged with local bundled skills."""
    results: dict[str, dict[str, str]] = load_local_skills()
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for skill_name, files in SKILL_URLS.items():
            results.setdefault(skill_name, {})
            for path, url in files.items():
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        results[skill_name][path] = resp.text
                except httpx.HTTPError:
                    pass
    return results
