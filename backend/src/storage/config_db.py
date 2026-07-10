from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from common.errors import SyncError
from config.proxy import parse_imap_proxy


PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 260_000
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD_FILE = "initial-admin-password.txt"

ACCOUNT_KEYS = {
    "email",
    "service_address",
    "password",
    "imap_server",
    "imap_port",
    "use_ssl",
    "auth",
    "imap_proxy",
    "imap_id",
    "pop_fallback",
    "imap_timeout",
}
SOURCE_RULE_KEYS = {
    "account_label",
    "exclude_folders",
    "folder_labels",
    "target_folders",
    "default_folder_label",
    "deleted_label",
    "skip_deleted_drafts",
    "skip_uids",
    "sync_interval",
    "batch_size",
}
TARGET_KEYS = ACCOUNT_KEYS | {"folder"}


def ensure_config_tables(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS proxy_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL DEFAULT 'http_connect',
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            username TEXT,
            password TEXT,
            extra_json TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE(type, host, port, username, password)
        );
        CREATE TABLE IF NOT EXISTS mail_accounts (
            email TEXT NOT NULL COLLATE NOCASE,
            service_address TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            enabled INTEGER NOT NULL DEFAULT 1,
            imap_server TEXT NOT NULL,
            imap_port INTEGER,
            use_ssl INTEGER NOT NULL DEFAULT 1,
            password TEXT,
            auth_json TEXT,
            proxy_profile_id INTEGER,
            imap_proxy_json TEXT,
            imap_id_json TEXT,
            pop_fallback_json TEXT,
            protocols_json TEXT,
            smtp_json TEXT,
            imap_timeout INTEGER,
            target_folder TEXT,
            extra_json TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(email, service_address),
            UNIQUE(email, service_address),
            FOREIGN KEY(proxy_profile_id) REFERENCES proxy_profiles(id)
        );
        CREATE TABLE IF NOT EXISTS source_rules (
            email TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            account_label TEXT NOT NULL,
            default_folder_label TEXT NOT NULL,
            deleted_label TEXT NOT NULL,
            sync_interval INTEGER,
            batch_size INTEGER,
            skip_deleted_drafts INTEGER NOT NULL DEFAULT 0,
            extra_json TEXT,
            last_sync_status TEXT NOT NULL DEFAULT 'ok',
            last_sync_error_summary TEXT,
            last_sync_error_detail TEXT
        );
        CREATE TABLE IF NOT EXISTS folder_rules (
            source_email TEXT NOT NULL,
            folder_name TEXT NOT NULL,
            target_folder TEXT,
            folder_label TEXT,
            folder_labels_json TEXT,
            excluded INTEGER NOT NULL DEFAULT 0,
            skip_uids_json TEXT,
            PRIMARY KEY(source_email, folder_name),
            FOREIGN KEY(source_email) REFERENCES source_rules(email)
            ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS mail_label (
            target_email TEXT NOT NULL DEFAULT '',
            target_service_address TEXT NOT NULL DEFAULT '',
            label_id TEXT NOT NULL,
            label_name TEXT NOT NULL,
            PRIMARY KEY(target_email, target_service_address, label_id),
            UNIQUE(target_email, target_service_address, label_name)
        );
        CREATE TABLE IF NOT EXISTS login_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            password_algorithm TEXT NOT NULL DEFAULT 'pbkdf2_sha256',
            password_iterations INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            enabled INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT
        );
        """
    )
    _migrate_mail_accounts_identity(db)
    _migrate_source_rules(db)
    _drop_obsolete_rule_status_table(db)
    _migrate_mail_label_identity(db)
    _migrate_legacy_synology_labels_table(db)
    _migrate_global_mail_labels_to_target(db)
    _migrate_folder_rules(db)
    _migrate_outlook_login_settings(db)
    _migrate_oauth_tokens_to_auth_json(db)
    ensure_default_admin_user(db)


def hash_password(
    password: str,
    *,
    salt: Optional[bytes] = None,
    iterations: int = PASSWORD_ITERATIONS,
) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str, iterations: int) -> bool:
    salt = bytes.fromhex(salt_hex)
    _, candidate = hash_password(password, salt=salt, iterations=iterations)
    return hmac.compare_digest(candidate, hash_hex)


def ensure_default_admin_user(db: sqlite3.Connection) -> None:
    username = os.environ.get("MAIL_SYNC_ADMIN_USERNAME", DEFAULT_ADMIN_USERNAME)
    existing = db.execute(
        "SELECT 1 FROM login_users WHERE username=? LIMIT 1",
        (username,),
    ).fetchone()
    if existing is not None:
        return
    password = os.environ.get("MAIL_SYNC_ADMIN_PASSWORD", "")
    must_change_password = 0
    if not password:
        password = secrets.token_urlsafe(24)
        must_change_password = 1
        _write_initial_admin_password(db, username, password)
    now = _now_iso()
    salt, password_hash = hash_password(password)
    db.execute(
        """
        INSERT OR IGNORE INTO login_users(
            username, password_hash, password_salt, password_algorithm,
            password_iterations, role, enabled, must_change_password,
            created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 'admin', 1, ?, ?, ?)
        """,
        (
            username,
            password_hash,
            salt,
            PASSWORD_ALGORITHM,
            PASSWORD_ITERATIONS,
            must_change_password,
            now,
            now,
        ),
    )


def _write_initial_admin_password(
    db: sqlite3.Connection,
    username: str,
    password: str,
) -> None:
    password_file = _initial_admin_password_path(db)
    if password_file is None:
        return
    password_file.parent.mkdir(parents=True, exist_ok=True)
    password_file.write_text(
        "\n".join(
            [
                "Mail Sync generated this one-time initial admin password",
                "because MAIL_SYNC_ADMIN_PASSWORD was not set.",
                f"username: {username}",
                f"password: {password}",
                "Delete this file after setting a permanent admin password.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(password_file, 0o600)


def _initial_admin_password_path(db: sqlite3.Connection) -> Optional[Path]:
    for _, name, filename in db.execute("PRAGMA database_list"):
        if name != "main" or not filename or filename == ":memory:":
            continue
        return Path(str(filename)).with_name(DEFAULT_ADMIN_PASSWORD_FILE)
    return None


def has_runtime_config(db: sqlite3.Connection) -> bool:
    ensure_config_tables(db)
    row = db.execute("SELECT 1 FROM mail_accounts LIMIT 1").fetchone()
    return row is not None


def import_legacy_config(
    db: sqlite3.Connection,
    config: dict,
    *,
    replace: bool = False,
    imported_from: Optional[Path] = None,
) -> None:
    ensure_config_tables(db)
    if has_runtime_config(db) and not replace:
        raise SyncError(
            "SQLite configuration already exists; rerun import-config with --replace"
        )
    if replace:
        _clear_runtime_config(db)

    target = config.get("target")
    target_email = target.get("email") if isinstance(target, dict) else ""
    target_service_address = (
        _service_address_from_profile(target) if isinstance(target, dict) else ""
    )
    if isinstance(target, dict):
        _insert_account(db, "target", target, 0)

    sources = config.get("sources")
    if isinstance(sources, list):
        for index, source in enumerate(sources, start=1):
            if not isinstance(source, dict):
                continue
            _insert_account(db, "source", source, index)
            _insert_source_rule(
                db,
                source,
                target_email=target_email,
                target_service_address=target_service_address,
            )

    labels = config.get("synology_labels")
    if isinstance(labels, dict):
        for label_id, label_name in labels.items():
            db.execute(
                """
                INSERT INTO mail_label(
                    target_email, target_service_address, label_id, label_name
                )
                VALUES(?, ?, ?, ?)
                ON CONFLICT(target_email, target_service_address, label_id) DO UPDATE
                SET label_name=excluded.label_name
                """,
                (target_email, target_service_address, label_id, label_name),
            )

    if imported_from is not None:
        db.execute(
            """
            INSERT INTO app_settings(key, value_json)
            VALUES('legacy_config_imported_from', ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json
            """,
            (json.dumps(str(imported_from), ensure_ascii=False),),
        )
    db.commit()


def load_runtime_config(
    db: sqlite3.Connection,
    state_path: Path,
    include_source_email: str | None = None,
    *,
    ensure_schema: bool = True,
) -> dict:
    if ensure_schema:
        ensure_config_tables(db)
    params: tuple[str, ...] = ()
    source_filter = "enabled=1"
    if include_source_email:
        source_filter = "(enabled=1 OR email=?)"
        params = (include_source_email,)
    rule_rows = db.execute(
        f"""
        SELECT *
        FROM source_rules
        WHERE {source_filter}
        ORDER BY enabled DESC, email
        """,
        params,
    ).fetchall()
    sources = []
    first_target_row = None
    first_labels: dict[str, str] = {}
    for rule_row in rule_rows:
        source_row = _source_row_for_rule(db, rule_row)
        if source_row is None:
            continue
        target_row = _target_row_for_rule(db, rule_row)
        labels = _labels_for_target_row(db, target_row)
        source = _source_from_rows(db, source_row, rule_row)
        source["target"] = _target_from_row(db, target_row)
        source["target_email"] = target_row["email"]
        source["target_service_address"] = target_row["service_address"]
        source["synology_labels"] = labels
        sources.append(source)
        if first_target_row is None:
            first_target_row = target_row
            first_labels = labels

    if first_target_row is None:
        first_target_row = _first_enabled_mail_account(db)
        if first_target_row is None:
            raise SyncError(
                "No target mailbox is configured in SQLite; run import-config first"
            )
        first_labels = _labels_for_target_row(db, first_target_row)

    return {
        "state_db": str(state_path),
        "target": _target_from_row(db, first_target_row),
        "sources": sources,
        "synology_labels": first_labels,
    }


def _clear_runtime_config(db: sqlite3.Connection) -> None:
    for table in (
        "folder_rules",
        "source_rules",
        "mail_accounts",
        "proxy_profiles",
        "mail_label",
        "app_settings",
    ):
        db.execute(f"DELETE FROM {table}")


def _migrate_mail_accounts_identity(db: sqlite3.Connection) -> None:
    info = db.execute("PRAGMA table_info(mail_accounts)").fetchall()
    columns = {row[1] for row in info}
    pk_columns = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
    needs_rebuild = (
        "service_address" not in columns
        or "protocols_json" not in columns
        or "smtp_json" not in columns
        or "role" in columns
        or pk_columns != ["email", "service_address"]
    )
    if not needs_rebuild:
        db.execute(
            """
            UPDATE mail_accounts
            SET service_address=COALESCE(NULLIF(service_address, ''), imap_server)
            WHERE service_address=''
            """
        )
        return

    legacy_rows = db.execute("SELECT * FROM mail_accounts").fetchall()
    db.execute("ALTER TABLE mail_accounts RENAME TO mail_accounts_legacy")
    _create_mail_accounts_table(db)
    seen: set[tuple[str, str]] = set()
    for row in legacy_rows:
        role = str(_row_get(row, "role", "source"))
        email = str(row["email"])
        imap_server = str(row["imap_server"] or "")
        service_address = str(_row_get(row, "service_address") or imap_server or email)
        service_address = _unique_service_address(email, service_address, seen)
        pop_fallback_json = _row_get(row, "pop_fallback_json")
        smtp_json = _row_get(row, "smtp_json")
        protocols_json = _row_get(row, "protocols_json") or _default_protocols_json(
            imap_server,
            pop_fallback_json,
            smtp_json,
        )
        db.execute(
            """
            INSERT INTO mail_accounts(
                email, service_address, enabled, imap_server, imap_port,
                use_ssl, password, auth_json, proxy_profile_id, imap_proxy_json,
                imap_id_json, pop_fallback_json, protocols_json, smtp_json,
                imap_timeout, target_folder, extra_json, sort_order
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email,
                service_address,
                int(row["enabled"]),
                imap_server,
                row["imap_port"],
                int(row["use_ssl"]),
                row["password"],
                row["auth_json"],
                row["proxy_profile_id"],
                row["imap_proxy_json"],
                row["imap_id_json"],
                pop_fallback_json,
                protocols_json,
                smtp_json,
                row["imap_timeout"],
                row["target_folder"] if role == "target" else _row_get(row, "target_folder"),
                row["extra_json"],
                int(row["sort_order"]),
            ),
        )
        if role == "source":
            _update_source_rule_extra(
                db,
                email,
                {"source_service_address": service_address},
            )
    db.execute("DROP TABLE mail_accounts_legacy")


def _migrate_source_rules(db: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in db.execute("PRAGMA table_info(source_rules)").fetchall()
    }
    if "enabled" in columns:
        _migrate_source_rule_status_columns(db, columns)
        _migrate_source_rule_service_addresses(db)
        return
    db.execute("ALTER TABLE source_rules ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
    db.execute(
        """
        UPDATE source_rules
        SET enabled=COALESCE(
            (
                SELECT enabled
                FROM mail_accounts
                WHERE mail_accounts.email=source_rules.email
                ORDER BY enabled DESC, sort_order, service_address
                LIMIT 1
            ),
            1
        )
        """
    )
    _migrate_source_rule_status_columns(db, columns | {"enabled"})
    _migrate_source_rule_service_addresses(db)


def _migrate_source_rule_status_columns(
    db: sqlite3.Connection,
    columns: set[str],
) -> None:
    if "last_sync_status" not in columns:
        db.execute(
            "ALTER TABLE source_rules "
            "ADD COLUMN last_sync_status TEXT NOT NULL DEFAULT 'ok'"
        )
    if "last_sync_error_summary" not in columns:
        db.execute(
            "ALTER TABLE source_rules ADD COLUMN last_sync_error_summary TEXT"
        )
    if "last_sync_error_detail" not in columns:
        db.execute(
            "ALTER TABLE source_rules ADD COLUMN last_sync_error_detail TEXT"
        )


def _drop_obsolete_rule_status_table(db: sqlite3.Connection) -> None:
    table_name = "sync_rule" + "_status"
    db.execute(f"DROP TABLE IF EXISTS {table_name}")


def _migrate_source_rule_service_addresses(db: sqlite3.Connection) -> None:
    rows = db.execute(
        """
        SELECT email, extra_json
        FROM source_rules
        """
    ).fetchall()
    for row in rows:
        extra = _object(row["extra_json"])
        updates = {}
        if not extra.get("source_service_address"):
            source_service = _service_address_for_email(db, row["email"])
            if source_service:
                updates["source_service_address"] = source_service
        target_email = str(extra.get("target_email") or "")
        if target_email and not extra.get("target_service_address"):
            target_service = _service_address_for_email(db, target_email)
            if target_service:
                updates["target_service_address"] = target_service
        if updates:
            _update_source_rule_extra(db, row["email"], updates)


def _migrate_mail_label_identity(db: sqlite3.Connection) -> None:
    info = db.execute("PRAGMA table_info(mail_label)").fetchall()
    columns = {row[1] for row in info}
    pk_columns = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
    needs_rebuild = (
        "target_email" not in columns
        or "target_service_address" not in columns
        or pk_columns != ["target_email", "target_service_address", "label_id"]
    )
    if needs_rebuild:
        legacy_rows = db.execute("SELECT * FROM mail_label").fetchall()
        default_target = _first_target_mail_account(db)
        db.execute("ALTER TABLE mail_label RENAME TO mail_label_legacy")
        _create_mail_label_table(db)
        for row in legacy_rows:
            target_email = str(
                _row_get(
                    row,
                    "target_email",
                    default_target["email"] if default_target is not None else "",
                )
                or ""
            )
            target_service_address = str(
                _row_get(
                    row,
                    "target_service_address",
                    _service_address_for_email(db, target_email),
                )
                or ""
            )
            db.execute(
                """
                INSERT OR IGNORE INTO mail_label(
                    target_email, target_service_address, label_id, label_name
                )
                VALUES(?, ?, ?, ?)
                """,
                (
                    target_email,
                    target_service_address,
                    row["label_id"],
                    row["label_name"],
                ),
            )
        db.execute("DROP TABLE mail_label_legacy")


def _migrate_legacy_synology_labels_table(db: sqlite3.Connection) -> None:
    if not _table_exists(db, "synology_labels"):
        return
    legacy_rows = db.execute("SELECT * FROM synology_labels").fetchall()
    default_target = _first_target_mail_account(db)
    for row in legacy_rows:
        target_email = str(
            _row_get(
                row,
                "target_email",
                default_target["email"] if default_target is not None else "",
            )
            or ""
        )
        target_service_address = str(
            _row_get(
                row,
                "target_service_address",
                _service_address_for_email(db, target_email),
            )
            or ""
        )
        db.execute(
            """
            INSERT OR IGNORE INTO mail_label(
                target_email, target_service_address, label_id, label_name
            )
            VALUES(?, ?, ?, ?)
            """,
            (
                target_email,
                target_service_address,
                row["label_id"],
                row["label_name"],
            ),
        )
    db.execute("DROP TABLE synology_labels")


def _migrate_global_mail_labels_to_target(db: sqlite3.Connection) -> None:
    target_row = _first_target_mail_account(db)
    if target_row is None:
        return
    global_rows = db.execute(
        """
        SELECT label_id, label_name
        FROM mail_label
        WHERE target_email='' AND target_service_address=''
        """
    ).fetchall()
    for row in global_rows:
        db.execute(
            """
            INSERT OR IGNORE INTO mail_label(
                target_email, target_service_address, label_id, label_name
            )
            VALUES(?, ?, ?, ?)
            """,
            (
                target_row["email"],
                target_row["service_address"],
                row["label_id"],
                row["label_name"],
            ),
        )
    if global_rows:
        db.execute(
            """
            DELETE FROM mail_label
            WHERE target_email='' AND target_service_address=''
            """
        )


def _migrate_folder_rules(db: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in db.execute("PRAGMA table_info(folder_rules)").fetchall()
    }
    if "target_folder" not in columns:
        db.execute("ALTER TABLE folder_rules ADD COLUMN target_folder TEXT")
    if "folder_labels_json" not in columns:
        db.execute("ALTER TABLE folder_rules ADD COLUMN folder_labels_json TEXT")
        rows = db.execute(
            """
            SELECT source_email, folder_name, folder_label
            FROM folder_rules
            WHERE folder_label IS NOT NULL AND folder_label <> ''
            """
        ).fetchall()
        for row in rows:
            db.execute(
                """
                UPDATE folder_rules
                SET folder_labels_json=?
                WHERE source_email=? AND folder_name=?
                """,
                (
                    _json([row["folder_label"]]),
                    row["source_email"],
                    row["folder_name"],
                ),
            )


def _migrate_outlook_login_settings(db: sqlite3.Connection) -> None:
    row = db.execute(
        "SELECT value_json FROM app_settings WHERE key='outlook_login'"
    ).fetchone()
    if row is None:
        return
    try:
        settings = json.loads(row["value_json"])
    except (TypeError, json.JSONDecodeError):
        settings = {}
    if not isinstance(settings, dict) or "token_dir" not in settings:
        return
    payload = {"client_id": str(settings.get("client_id") or "")}
    db.execute(
        """
        UPDATE app_settings
        SET value_json=?
        WHERE key='outlook_login'
        """,
        (_json(payload),),
    )


def _migrate_oauth_tokens_to_auth_json(db: sqlite3.Connection) -> None:
    database_path = _database_path(db)
    database_dir = Path(database_path).parent if database_path else Path.cwd()
    rows = db.execute(
        """
        SELECT email, service_address, auth_json
        FROM mail_accounts
        WHERE auth_json IS NOT NULL AND auth_json <> ''
        """
    ).fetchall()
    for row in rows:
        try:
            auth = json.loads(row["auth_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(auth, dict) or isinstance(auth.get("token"), dict):
            continue
        token_file = auth.get("token_file")
        if not token_file:
            continue
        token_path = Path(str(token_file)).expanduser()
        if not token_path.is_absolute():
            token_path = (database_dir / token_path).resolve()
        try:
            token = json.loads(token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(token, dict):
            continue
        auth["token"] = token
        auth.pop("token_file", None)
        db.execute(
            """
            UPDATE mail_accounts
            SET auth_json=?
            WHERE email=? AND service_address=?
            """,
            (
                _json(auth),
                row["email"],
                row["service_address"],
            ),
        )


def _create_mail_accounts_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE mail_accounts (
            email TEXT NOT NULL COLLATE NOCASE,
            service_address TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            enabled INTEGER NOT NULL DEFAULT 1,
            imap_server TEXT NOT NULL,
            imap_port INTEGER,
            use_ssl INTEGER NOT NULL DEFAULT 1,
            password TEXT,
            auth_json TEXT,
            proxy_profile_id INTEGER,
            imap_proxy_json TEXT,
            imap_id_json TEXT,
            pop_fallback_json TEXT,
            protocols_json TEXT,
            smtp_json TEXT,
            imap_timeout INTEGER,
            target_folder TEXT,
            extra_json TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(email, service_address),
            UNIQUE(email, service_address),
            FOREIGN KEY(proxy_profile_id) REFERENCES proxy_profiles(id)
        )
        """
    )


def _create_mail_label_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE mail_label (
            target_email TEXT NOT NULL DEFAULT '',
            target_service_address TEXT NOT NULL DEFAULT '',
            label_id TEXT NOT NULL,
            label_name TEXT NOT NULL,
            PRIMARY KEY(target_email, target_service_address, label_id),
            UNIQUE(target_email, target_service_address, label_name)
        )
        """
    )


def _table_exists(db: sqlite3.Connection, table_name: str) -> bool:
    return (
        db.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table' AND name=?
            """,
            (table_name,),
        ).fetchone()
        is not None
    )


def _row_get(row: sqlite3.Row, key: str, default: object = None) -> object:
    return row[key] if key in row.keys() else default


def _update_source_rule_extra(
    db: sqlite3.Connection,
    email: str,
    updates: dict[str, object],
) -> None:
    row = db.execute(
        """
        SELECT extra_json
        FROM source_rules
        WHERE email=?
        """,
        (email,),
    ).fetchone()
    if row is None:
        return
    extra = _object(row["extra_json"])
    changed = False
    for key, value in updates.items():
        if value and extra.get(key) != value:
            extra[key] = value
            changed = True
    if changed:
        db.execute(
            """
            UPDATE source_rules
            SET extra_json=?
            WHERE email=?
            """,
            (_json(extra), email),
        )


def _service_address_from_profile(profile: dict) -> str:
    value = profile.get("service_address") or profile.get("imap_server") or ""
    return str(value).strip()


def _default_protocols_json(
    imap_server: str,
    pop_fallback_json: object,
    smtp_json: object,
) -> str:
    protocols = []
    if imap_server:
        protocols.append("imap")
    if pop_fallback_json:
        protocols.append("pop")
    if smtp_json:
        protocols.append("smtp")
    return _json(protocols or ["imap"]) or '["imap"]'


def _unique_service_address(
    email: str,
    service_address: str,
    seen: set[tuple[str, str]],
) -> str:
    base = service_address.strip() or email
    candidate = base
    index = 2
    while (email.casefold(), candidate.casefold()) in seen:
        candidate = f"{base}#{index}"
        index += 1
    seen.add((email.casefold(), candidate.casefold()))
    return candidate


def _first_target_mail_account(db: sqlite3.Connection) -> sqlite3.Row | None:
    return db.execute(
        """
        SELECT email, service_address
        FROM mail_accounts
        ORDER BY enabled DESC, sort_order, email, service_address
        LIMIT 1
        """
    ).fetchone()


def _first_enabled_mail_account(db: sqlite3.Connection) -> sqlite3.Row | None:
    return db.execute(
        """
        SELECT *
        FROM mail_accounts
        WHERE enabled=1
        ORDER BY sort_order, email, service_address
        LIMIT 1
        """
    ).fetchone()


def _account_row_for_email(
    db: sqlite3.Connection,
    email: str,
    service_address: str = "",
) -> sqlite3.Row | None:
    service_filter = "AND service_address=?" if service_address else ""
    params: tuple[object, ...] = (
        (email, service_address) if service_address else (email,)
    )
    return db.execute(
        f"""
        SELECT *
        FROM mail_accounts
        WHERE email=? AND enabled=1
          {service_filter}
        ORDER BY sort_order, service_address
        LIMIT 1
        """,
        params,
    ).fetchone()


def _source_row_for_rule(
    db: sqlite3.Connection,
    rule: sqlite3.Row,
) -> sqlite3.Row | None:
    extra = _object(rule["extra_json"])
    service_address = str(extra.get("source_service_address") or "")
    row = _account_row_for_email(db, rule["email"], service_address)
    if row is not None:
        return row
    if service_address:
        raise SyncError(
            f"Source mailbox {rule['email']} at {service_address} is not configured"
        )
    return _account_row_for_email(db, rule["email"])


def _target_row_for_rule(
    db: sqlite3.Connection,
    rule: sqlite3.Row,
) -> sqlite3.Row:
    extra = _object(rule["extra_json"])
    target_email = str(extra.get("target_email") or "")
    target_service = str(extra.get("target_service_address") or "")
    row = (
        _account_row_for_email(db, target_email, target_service)
        if target_email
        else None
    )
    if row is not None:
        return row
    if target_email:
        detail = (
            f" at {target_service}"
            if target_service
            else ""
        )
        raise SyncError(
            f"Target mailbox {target_email}{detail} for source "
            f"{rule['email']} is not configured"
        )
    row = _first_enabled_mail_account(db)
    if row is None:
        raise SyncError(
            "No target mailbox is configured in SQLite; run import-config first"
        )
    return row


def _labels_for_target_row(
    db: sqlite3.Connection,
    target_row: sqlite3.Row,
) -> dict[str, str]:
    labels: dict[str, str] = {}
    used_names: set[str] = set()

    def add_rows(rows: list[sqlite3.Row]) -> None:
        for row in rows:
            label_id = row["label_id"]
            label_name = row["label_name"]
            if label_id in labels or label_name in used_names:
                continue
            labels[label_id] = label_name
            used_names.add(label_name)

    label_rows = db.execute(
        """
        SELECT label_id, label_name
        FROM mail_label
        WHERE target_email=? AND target_service_address=?
        ORDER BY label_id
        """,
        (target_row["email"], target_row["service_address"]),
    ).fetchall()
    add_rows(label_rows)
    if not label_rows:
        label_rows = db.execute(
            """
            SELECT label_id, label_name
            FROM mail_label
            WHERE target_email=? AND target_service_address=''
            ORDER BY label_id
            """,
            (target_row["email"],),
        ).fetchall()
        add_rows(label_rows)
    if not label_rows:
        label_rows = db.execute(
            """
            SELECT label_id, label_name
            FROM mail_label
            WHERE target_email=''
            ORDER BY label_id
            """
        ).fetchall()
        add_rows(label_rows)

    return labels


def _service_address_for_email(db: sqlite3.Connection, email: str) -> str:
    if not email:
        return ""
    row = db.execute(
        """
        SELECT service_address
        FROM mail_accounts
        WHERE email=?
        ORDER BY enabled DESC, sort_order, service_address
        LIMIT 1
        """,
        (email,),
    ).fetchone()
    return row["service_address"] if row is not None else ""


def _insert_account(
    db: sqlite3.Connection,
    role: str,
    profile: dict,
    sort_order: int,
) -> None:
    email = profile.get("email")
    if not isinstance(email, str) or not email:
        return
    proxy_profile_id = _proxy_profile_id(db, profile.get("imap_proxy"), email)
    known_keys = TARGET_KEYS if role == "target" else ACCOUNT_KEYS | SOURCE_RULE_KEYS
    extra = {key: value for key, value in profile.items() if key not in known_keys}
    imap_server = str(profile.get("imap_server") or "")
    service_address = _service_address_from_profile(profile) or imap_server or email
    pop_fallback_json = _json(profile.get("pop_fallback"))
    smtp_json = _json(profile.get("smtp"))
    protocols_json = _json(profile.get("protocols")) or _default_protocols_json(
        imap_server,
        pop_fallback_json,
        smtp_json,
    )
    db.execute(
        """
        INSERT INTO mail_accounts(
            email, service_address, enabled, imap_server, imap_port, use_ssl, password,
            auth_json, proxy_profile_id, imap_proxy_json, imap_id_json,
            pop_fallback_json, protocols_json, smtp_json, imap_timeout,
            target_folder, extra_json, sort_order
        )
        VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email, service_address) DO UPDATE SET
            enabled=excluded.enabled,
            imap_server=excluded.imap_server,
            imap_port=excluded.imap_port,
            use_ssl=excluded.use_ssl,
            password=excluded.password,
            auth_json=excluded.auth_json,
            proxy_profile_id=excluded.proxy_profile_id,
            imap_proxy_json=excluded.imap_proxy_json,
            imap_id_json=excluded.imap_id_json,
            pop_fallback_json=excluded.pop_fallback_json,
            protocols_json=excluded.protocols_json,
            smtp_json=excluded.smtp_json,
            imap_timeout=excluded.imap_timeout,
            target_folder=COALESCE(excluded.target_folder, mail_accounts.target_folder),
            extra_json=excluded.extra_json,
            sort_order=excluded.sort_order
        """,
        (
            email,
            service_address,
            imap_server,
            profile.get("imap_port"),
            1 if profile.get("use_ssl", True) else 0,
            profile.get("password"),
            _json(profile.get("auth")),
            proxy_profile_id,
            _json(profile.get("imap_id")),
            pop_fallback_json,
            protocols_json,
            smtp_json,
            profile.get("imap_timeout"),
            profile.get("folder") if role == "target" else None,
            _json(extra),
            sort_order,
        ),
    )


def _insert_source_rule(
    db: sqlite3.Connection,
    source: dict,
    *,
    target_email: str = "",
    target_service_address: str = "",
) -> None:
    email = source.get("email")
    if not isinstance(email, str) or not email:
        return
    extra = {
        key: value
        for key, value in source.items()
        if key not in ACCOUNT_KEYS and key not in SOURCE_RULE_KEYS
    }
    source_service_address = _service_address_from_profile(source)
    if source_service_address and not extra.get("source_service_address"):
        extra["source_service_address"] = source_service_address
    if target_email and not extra.get("target_email"):
        extra["target_email"] = target_email
    if target_service_address and not extra.get("target_service_address"):
        extra["target_service_address"] = target_service_address
    db.execute(
        """
        INSERT INTO source_rules(
            email, enabled, account_label, default_folder_label, deleted_label,
            sync_interval, batch_size, skip_deleted_drafts, extra_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            enabled=excluded.enabled,
            account_label=excluded.account_label,
            default_folder_label=excluded.default_folder_label,
            deleted_label=excluded.deleted_label,
            sync_interval=excluded.sync_interval,
            batch_size=excluded.batch_size,
            skip_deleted_drafts=excluded.skip_deleted_drafts,
            extra_json=excluded.extra_json
        """,
        (
            email,
            1 if source.get("enabled", True) else 0,
            source.get("account_label") or "",
            source.get("default_folder_label") or "",
            source.get("deleted_label") or "",
            source.get("sync_interval"),
            source.get("batch_size"),
            1 if source.get("skip_deleted_drafts", False) else 0,
            _json(extra),
        ),
    )
    for folder_name, label_name in _dict_items(source.get("folder_labels")):
        labels = _folder_label_list(label_name)
        _upsert_folder_rule(
            db,
            email,
            folder_name,
            folder_label=labels[0] if labels else None,
            folder_labels=labels,
        )
    for folder_name, target_folder in _dict_items(source.get("target_folders")):
        _upsert_folder_rule(
            db,
            email,
            folder_name,
            target_folder=target_folder,
        )
    for folder_name in _string_items(source.get("exclude_folders")):
        _upsert_folder_rule(db, email, folder_name, excluded=1)
    for folder_name, values in _dict_items(source.get("skip_uids")):
        _upsert_folder_rule(db, email, folder_name, skip_uids=list(values))


def _upsert_folder_rule(
    db: sqlite3.Connection,
    source_email: str,
    folder_name: str,
    *,
    target_folder: object = None,
    folder_label: object = None,
    folder_labels: Optional[Iterable[object]] = None,
    excluded: Optional[int] = None,
    skip_uids: Optional[Iterable[object]] = None,
) -> None:
    existing = db.execute(
        """
        SELECT target_folder, folder_label, folder_labels_json, excluded,
               skip_uids_json
        FROM folder_rules
        WHERE source_email=? AND folder_name=?
        """,
        (source_email, folder_name),
    ).fetchone()
    db.execute(
        """
        INSERT INTO folder_rules(
            source_email, folder_name, target_folder, folder_label,
            folder_labels_json, excluded, skip_uids_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_email, folder_name) DO UPDATE SET
            target_folder=excluded.target_folder,
            folder_label=excluded.folder_label,
            folder_labels_json=excluded.folder_labels_json,
            excluded=excluded.excluded,
            skip_uids_json=excluded.skip_uids_json
        """,
        (
            source_email,
            folder_name,
            str(target_folder).strip() if target_folder is not None else (
                existing["target_folder"] if existing else None
            ),
            folder_label if folder_label is not None else (
                existing["folder_label"] if existing else None
            ),
            _json(_folder_label_list(folder_labels)) if folder_labels is not None else (
                existing["folder_labels_json"] if existing else None
            ),
            excluded if excluded is not None else (
                int(existing["excluded"]) if existing else 0
            ),
            _json(skip_uids) if skip_uids is not None else (
                existing["skip_uids_json"] if existing else None
            ),
        ),
    )


def _target_from_row(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    profile = _account_from_row(db, row)
    profile["folder"] = row["target_folder"] or "INBOX"
    return profile


def _source_from_rows(
    db: sqlite3.Connection,
    account: sqlite3.Row,
    rule: sqlite3.Row,
) -> dict:
    profile = _account_from_row(db, account)
    rule_extra = _object(rule["extra_json"])
    profile.update(rule_extra)
    if not _truthy(
        rule_extra.get("pop_fallback_enabled"),
        default=bool(profile.get("pop_fallback")),
    ):
        profile.pop("pop_fallback", None)
    profile.update(
        {
            "account_label": rule["account_label"],
            "default_folder_label": rule["default_folder_label"],
            "deleted_label": rule["deleted_label"],
            "skip_deleted_drafts": bool(rule["skip_deleted_drafts"]),
            "folder_labels": {},
            "target_folders": {},
            "exclude_folders": [],
            "skip_uids": {},
        }
    )
    if rule["sync_interval"] is not None:
        profile["sync_interval"] = int(rule["sync_interval"])
    if rule["batch_size"] is not None:
        profile["batch_size"] = int(rule["batch_size"])
    rows = db.execute(
        """
        SELECT folder_name, target_folder, folder_label, folder_labels_json,
               excluded, skip_uids_json
        FROM folder_rules
        WHERE source_email=?
        ORDER BY folder_name
        """,
        (account["email"],),
    ).fetchall()
    for row in rows:
        labels = _folder_labels_from_row(row)
        if labels:
            profile["folder_labels"][row["folder_name"]] = (
                labels if len(labels) > 1 else labels[0]
            )
        if row["target_folder"]:
            profile["target_folders"][row["folder_name"]] = row["target_folder"]
        if row["excluded"]:
            profile["exclude_folders"].append(row["folder_name"])
        if row["skip_uids_json"] is not None:
            profile["skip_uids"][row["folder_name"]] = _array(row["skip_uids_json"])
    return profile


def _folder_labels_from_row(row: sqlite3.Row) -> list[str]:
    if row["folder_labels_json"]:
        return _folder_label_list(_array(row["folder_labels_json"]))
    return _folder_label_list(row["folder_label"])


def _account_from_row(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    profile = _object(row["extra_json"])
    profile.update(
        {
            "email": row["email"],
            "service_address": row["service_address"],
            "imap_server": row["imap_server"],
            "use_ssl": bool(row["use_ssl"]),
        }
    )
    if "protocols_json" in row.keys() and row["protocols_json"]:
        profile["protocols"] = _array(row["protocols_json"])
    if row["imap_port"] is not None:
        profile["imap_port"] = int(row["imap_port"])
    if row["password"]:
        profile["password"] = row["password"]
    for column, key in (
        ("auth_json", "auth"),
        ("imap_id_json", "imap_id"),
        ("pop_fallback_json", "pop_fallback"),
        ("smtp_json", "smtp"),
    ):
        if column not in row.keys():
            continue
        value = _object(row[column])
        if value:
            if key == "auth":
                _attach_oauth_storage(db, row, value)
            profile[key] = value
    if row["imap_timeout"] is not None:
        profile["imap_timeout"] = int(row["imap_timeout"])
    proxy = _proxy_from_account(db, row)
    if proxy is not None:
        profile["imap_proxy"] = proxy
    return profile


def _proxy_from_account(db: sqlite3.Connection, row: sqlite3.Row) -> Optional[dict]:
    if row["proxy_profile_id"] is not None:
        proxy = db.execute(
            "SELECT * FROM proxy_profiles WHERE id=?",
            (row["proxy_profile_id"],),
        ).fetchone()
        if proxy is None:
            raise SyncError(f"Missing proxy profile {row['proxy_profile_id']}")
        result = {
            "type": proxy["type"],
            "host": proxy["host"],
            "port": int(proxy["port"]),
        }
        if proxy["username"] is not None:
            result["username"] = proxy["username"]
        if proxy["password"] is not None:
            result["password"] = proxy["password"]
        result.update(_object(proxy["extra_json"]))
        return result
    return _object(row["imap_proxy_json"]) or None


def _attach_oauth_storage(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    auth: dict,
) -> None:
    if auth.get("type") != "oauth2" or auth.get("provider") != "microsoft":
        return
    state_db = _database_path(db)
    if not state_db:
        return
    auth["storage"] = {
        "type": "sqlite",
        "state_db": state_db,
        "email": row["email"],
        "service_address": row["service_address"],
    }


def _database_path(db: sqlite3.Connection) -> str:
    row = db.execute("PRAGMA database_list").fetchone()
    if row is None:
        return ""
    try:
        return str(row["file"] or "")
    except (KeyError, TypeError, IndexError):
        return str(row[2] or "")


def _proxy_profile_id(
    db: sqlite3.Connection,
    raw_proxy: object,
    context: str,
) -> Optional[int]:
    settings = parse_imap_proxy(raw_proxy, context)
    if settings is None:
        return None
    name = f"{settings.host}:{settings.port}"
    if settings.username:
        name = f"{settings.username}@{name}"
    db.execute(
        """
        INSERT OR IGNORE INTO proxy_profiles(
            name, type, host, port, username, password
        )
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            settings.proxy_type,
            settings.host,
            settings.port,
            settings.username,
            settings.password,
        ),
    )
    row = db.execute(
        """
        SELECT id FROM proxy_profiles
        WHERE type=? AND host=? AND port=?
          AND username IS ? AND password IS ?
        """,
        (
            settings.proxy_type,
            settings.host,
            settings.port,
            settings.username,
            settings.password,
        ),
    ).fetchone()
    if row is None:
        raise SyncError(f"Could not create proxy profile for {context}")
    return int(row["id"])


def _dict_items(value: object):
    if not isinstance(value, dict):
        return ()
    return value.items()


def _string_items(value: object):
    if not isinstance(value, list):
        return ()
    return [item for item in value if isinstance(item, str)]


def _folder_label_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _json(value: object) -> Optional[str]:
    if value is None:
        return None
    if value == {}:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _object(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    value = json.loads(str(raw))
    if not isinstance(value, dict):
        raise SyncError("Stored JSON object configuration is invalid")
    return value


def _truthy(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _array(raw: str) -> list:
    value = json.loads(raw)
    if not isinstance(value, list):
        raise SyncError("Stored JSON array configuration is invalid")
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
