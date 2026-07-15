#!/usr/bin/env python3
"""Create a consistent deployment backup of the live SQLite state database."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


state_path = Path(
    os.environ.get("MAIL_SYNC_STATE_DB", "/app/data/mail_sync_state.sqlite3")
)
backup_name = required_environment("MAIL_SYNC_BACKUP_NAME")
if Path(backup_name).name != backup_name:
    raise RuntimeError("MAIL_SYNC_BACKUP_NAME must be a file name, not a path")
if not backup_name.startswith("deploy-") or not backup_name.endswith(".sqlite3"):
    raise RuntimeError("deployment backup name must match deploy-*.sqlite3")

try:
    backups_to_keep = int(os.environ.get("MAIL_SYNC_DEPLOY_BACKUP_KEEP", "10"))
except ValueError as exc:
    raise RuntimeError("MAIL_SYNC_DEPLOY_BACKUP_KEEP must be a positive integer") from exc
if backups_to_keep < 1:
    raise RuntimeError("MAIL_SYNC_DEPLOY_BACKUP_KEEP must be a positive integer")

if not state_path.is_file():
    print(f"backup skipped: state database does not exist at {state_path}")
    raise SystemExit(0)

backup_dir = state_path.parent / "backups"
backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
try:
    backup_dir.chmod(0o700)
except PermissionError:
    pass

backup_path = backup_dir / backup_name
metadata_path = backup_path.with_suffix(f"{backup_path.suffix}.json")
if backup_path.exists() or metadata_path.exists():
    raise RuntimeError(f"deployment backup already exists: {backup_path}")

try:
    source = sqlite3.connect(
        f"{state_path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=30,
    )
    destination = None
    try:
        destination = sqlite3.connect(str(backup_path), timeout=30)
        source.execute("PRAGMA busy_timeout = 30000")
        destination.execute("PRAGMA busy_timeout = 30000")
        source.backup(destination, pages=1024, sleep=0.05)
        integrity_result = destination.execute("PRAGMA integrity_check").fetchone()
        if not integrity_result or integrity_result[0] != "ok":
            raise RuntimeError(
                "backup integrity check failed: "
                f"{integrity_result[0] if integrity_result else 'no result'}"
            )
    finally:
        if destination is not None:
            destination.close()
        source.close()

    backup_path.chmod(0o600)
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": str(state_path),
        "backup": str(backup_path),
        "old_commit": os.environ.get("MAIL_SYNC_DEPLOY_OLD_COMMIT", ""),
        "old_image_id": os.environ.get("MAIL_SYNC_DEPLOY_OLD_IMAGE_ID", ""),
        "old_image_ref": os.environ.get("MAIL_SYNC_DEPLOY_OLD_IMAGE_REF", ""),
    }
    with metadata_path.open("x", encoding="utf-8") as metadata_file:
        json.dump(metadata, metadata_file, ensure_ascii=False, indent=2)
        metadata_file.write("\n")
    metadata_path.chmod(0o600)
except Exception:
    backup_path.unlink(missing_ok=True)
    metadata_path.unlink(missing_ok=True)
    raise

deployment_backups = sorted(
    (
        candidate
        for candidate in backup_dir.glob("deploy-*.sqlite3")
        if candidate.is_file()
    ),
    key=lambda candidate: candidate.name,
    reverse=True,
)
for expired_backup in deployment_backups[backups_to_keep:]:
    expired_metadata = expired_backup.with_suffix(f"{expired_backup.suffix}.json")
    expired_backup.unlink()
    expired_metadata.unlink(missing_ok=True)

print(f"backup created: {backup_path}")
