"""Encrypted backup export and restore for agent data."""

import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import pyzipper
import yaml

from booa.writer import TEMPLATE_VERSION

BACKUP_FORMAT = "booa-hermes-backup"
BACKUP_FORMAT_VERSION = 1
EXPORT_DIRS = ["memories", "skills", "context", "sessions"]
RESTORE_DIRS = frozenset({"memories", "skills", "context", "sessions"})
RESTORE_FILES = frozenset({"SOUL.md", "onchain-settings.json"})
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ENTRY_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_ENTRIES = 20_000

_SYMLINK_MODE = 0o120000


def _redacted_config(hermes_path: Path) -> tuple[str, list[str]] | None:
    p = hermes_path / "config.yaml"
    if not p.exists():
        return None
    try:
        config = yaml.safe_load(p.read_text()) or {}
    except Exception:
        return None
    redactions: list[str] = []
    tg = config.get("gateway", {}).get("platforms", {}).get("telegram")
    if isinstance(tg, dict) and tg.get("bot_token"):
        tg["bot_token"] = "__REDACTED__"
        redactions.append("config.yaml: gateway.platforms.telegram.bot_token")
    if config.pop("mcp_servers", None) is not None:
        redactions.append("config.yaml: mcp_servers (OWS passphrase, RPC, OpenSea key)")
    return yaml.dump(config, default_flow_style=False), redactions


def _filtered_wallet_info(data_home: Path) -> str | None:
    p = data_home / ".agent" / "wallet-info.txt"
    if not p.exists():
        return None
    safe_lines = [
        l for l in p.read_text().splitlines()
        if "mnemonic" not in l.lower()
        and not (len(l.strip().split()) >= 10 and all(w.isalpha() for w in l.strip().split()))
    ]
    return "\n".join(safe_lines)


def create_backup_zip(hermes_home: str, password: str, *, token_id, chain_id,
                      agent_name: str, hermes_pin: str = "") -> str:
    hermes_path = Path(hermes_home)
    data_home = Path(os.path.dirname(hermes_home))
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()

    files = 0
    total = 0
    contents: set[str] = set()
    redactions: list[str] = []

    with pyzipper.AESZipFile(tmp.name, "w", compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as zf:
        zf.setpassword(password.encode())

        def add_file(src: Path, arcname: str, top: str):
            nonlocal files, total
            zf.write(src, arcname)
            files += 1
            total += src.stat().st_size
            contents.add(top)

        def add_text(text: str, arcname: str, top: str):
            nonlocal files, total
            zf.writestr(arcname, text)
            files += 1
            total += len(text.encode())
            contents.add(top)

        for folder in EXPORT_DIRS:
            folder_path = hermes_path / folder
            if folder_path.exists():
                for f in folder_path.rglob("*"):
                    if f.is_file() and not f.is_symlink():
                        add_file(f, str(f.relative_to(hermes_path)), folder + "/")

        soul = hermes_path / "SOUL.md"
        if soul.exists():
            add_file(soul, "SOUL.md", "SOUL.md")

        cfg = _redacted_config(hermes_path)
        if cfg is not None:
            add_text(cfg[0], "config.yaml", "config.yaml")
            redactions.extend(cfg[1])

        onchain = hermes_path / "onchain-settings.json"
        if onchain.exists():
            add_file(onchain, "onchain-settings.json", "onchain-settings.json")

        wallet_info = _filtered_wallet_info(data_home)
        if wallet_info is not None:
            add_text(wallet_info, "wallet-info.txt", "wallet-info.txt")

        ows_wallets = data_home / ".ows" / "wallets"
        if ows_wallets.exists():
            for f in ows_wallets.rglob("*"):
                if f.is_file() and not f.is_symlink():
                    add_file(f, "ows/wallets/" + str(f.relative_to(ows_wallets)), "ows/")

        manifest = {
            "format": BACKUP_FORMAT,
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "template_version": TEMPLATE_VERSION,
            "hermes_pin": hermes_pin,
            "token_id": token_id,
            "chain_id": chain_id,
            "agent_name": agent_name,
            "contents": sorted(contents),
            "redactions": redactions,
            "counts": {"files": files, "bytes_uncompressed": total},
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    return tmp.name


def _err(msg: str, status: int, **extra) -> dict:
    return {"error": msg, "status": status, **extra}


def restore_backup(hermes_home: str, archive_path: str, password: str, *,
                   instance_token_id=None, confirm_token_mismatch: bool = False,
                   restore_wallet: bool = False) -> dict:
    hermes_path = Path(hermes_home)
    data_home = Path(os.path.dirname(hermes_home))

    try:
        if os.path.getsize(archive_path) > MAX_ARCHIVE_BYTES:
            return _err("archive too large", 400)
        zf = pyzipper.AESZipFile(archive_path)
        zf.setpassword(password.encode())
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            return _err("archive too large", 400)
        manifest_info = next((i for i in infos if i.filename == "manifest.json"), None)
        if manifest_info is None or manifest_info.file_size > MAX_MANIFEST_BYTES:
            return _err("invalid archive or password", 400)
        with zf.open(manifest_info) as mf:
            manifest = json.loads(mf.read(MAX_MANIFEST_BYTES + 1))
    except Exception:
        return _err("invalid archive or password", 400)

    if manifest.get("format") != BACKUP_FORMAT:
        return _err("not a booa-hermes-backup archive", 400)
    if not isinstance(manifest.get("format_version"), int):
        return _err("invalid manifest", 400)
    if manifest["format_version"] > BACKUP_FORMAT_VERSION:
        return _err("backup created by a newer template — update this instance first", 400)
    if "token_id" not in manifest:
        return _err("manifest missing token_id", 400)
    if (instance_token_id is not None
            and manifest["token_id"] != instance_token_id
            and not confirm_token_mismatch):
        return _err("token_mismatch", 409,
                    manifest_token_id=manifest["token_id"],
                    instance_token_id=instance_token_id)

    total = 0
    for info in infos:
        name = info.filename
        if "\\" in name or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
            return _err("unsafe archive entry", 400)
        if (info.external_attr >> 16) & 0o170000 == _SYMLINK_MODE:
            return _err("unsafe archive entry", 400)
        if info.file_size > MAX_ENTRY_BYTES:
            return _err("archive entry too large", 400)
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            return _err("archive too large", 400)

    extract_root = hermes_path / f".restore-tmp-{int(time.time())}"
    extract_root.mkdir(parents=True, exist_ok=True)
    extract_resolved = str(extract_root.resolve())

    restored_targets: set[str] = set()
    skipped: list[str] = []
    warnings: list[str] = []

    def fail(msg: str, status: int) -> dict:
        shutil.rmtree(extract_root, ignore_errors=True)
        return _err(msg, status)

    try:
        for info in infos:
            name = info.filename
            if name.endswith("/") or name == "manifest.json":
                continue
            top = name.split("/", 1)[0]
            if name in RESTORE_FILES:
                target_rel, target_key = name, name
            elif top in RESTORE_DIRS and "/" in name:
                target_rel, target_key = name, top
            elif top == "ows" and "/" in name:
                target_rel, target_key = name, "ows"
            else:
                skipped.append(name)
                continue

            dest = extract_root / target_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not str(dest.resolve()).startswith(extract_resolved + os.sep):
                return fail("unsafe archive entry", 400)

            written = 0
            with zf.open(info) as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_ENTRY_BYTES:
                        return fail("archive entry too large", 400)
                    out.write(chunk)
            restored_targets.add(target_key)
    except Exception:
        return fail("invalid archive or password", 400)
    finally:
        zf.close()

    if "ows" in restored_targets:
        ows_root = data_home / ".ows"
        ows_live = ows_root.exists() and any(ows_root.iterdir())
        if ows_live and not restore_wallet:
            restored_targets.discard("ows")
            skipped.append("ows/")
            warnings.append("live wallet vault present — ows/ skipped (set restore_wallet to overwrite)")

    snapshot = hermes_path / ".pre-import-backup"
    shutil.rmtree(snapshot, ignore_errors=True)
    snapshot.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []

    def live_path(target_key: str) -> Path:
        if target_key == "ows":
            return data_home / ".ows"
        return hermes_path / target_key

    try:
        for target in sorted(restored_targets):
            live = live_path(target)
            snap = snapshot / target
            if live.exists():
                shutil.move(str(live), str(snap))
                moved.append((snap, live))
            shutil.move(str(extract_root / ("ows" if target == "ows" else target)), str(live))
    except Exception:
        for snap, live in reversed(moved):
            if live.exists():
                if live.is_dir():
                    shutil.rmtree(live, ignore_errors=True)
                else:
                    live.unlink(missing_ok=True)
            shutil.move(str(snap), str(live))
        shutil.rmtree(extract_root, ignore_errors=True)
        return _err("restore failed — previous data rolled back", 500)

    shutil.rmtree(extract_root, ignore_errors=True)
    return {
        "ok": True,
        "status": 200,
        "restored": sorted(restored_targets),
        "skipped": sorted(skipped),
        "warnings": warnings,
    }
