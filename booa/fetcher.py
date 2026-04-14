"""Fetch BOOA identity files from khora.fun API."""

import httpx

KHORA_API = "https://khora.fun/api/agent-files/360"
KHORA_SKILLS_URL = "https://khora.fun/skills"
COBBEE_SKILLS_URL = "https://cobbee.fun/skills"


class TokenNotFound(Exception):
    pass


async def fetch_booa_identity(token_id: int) -> dict:
    """Fetch all identity files for a BOOA token."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        soul = await client.get(f"{KHORA_API}/{token_id}/soul.md")
        identity = await client.get(f"{KHORA_API}/{token_id}/identity.md")
        avatar = await client.get(f"{KHORA_API}/{token_id}/avatar.svg")
        agent_json = await client.get(f"{KHORA_API}/{token_id}/agent.json")

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
        "SKILL.md": f"{KHORA_SKILLS_URL}/SKILL.md",
        "references/wallet-setup.md": f"{KHORA_SKILLS_URL}/references/wallet-setup.md",
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


async def fetch_skills() -> dict[str, dict[str, str]]:
    """Fetch all pre-installed skills from Khôra and Cobbee."""
    results: dict[str, dict[str, str]] = {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for skill_name, files in SKILL_URLS.items():
            results[skill_name] = {}
            for path, url in files.items():
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        results[skill_name][path] = resp.text
                except httpx.HTTPError:
                    pass
    return results
