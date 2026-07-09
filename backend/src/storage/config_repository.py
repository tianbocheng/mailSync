from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Sequence

from common.errors import SyncError
from sync.settings import DEFAULT_SYNC_BATCH_SIZE, DEFAULT_SYNC_INTERVAL_SECONDS


LABEL_ID_RE = re.compile(r"^[^\s()\"]+$")
SYNC_MODES = {"realtime", "scheduled"}
DATE_POLICIES = {"auto", "standard", "qq_internaldate"}
OUT_OF_SCOPE_POLICIES = {"keep", "label", "delete"}
PROXY_TYPES = {"http_connect", "socks5"}
COMMON_MAILBOX_FEATURES = {
    "all_time_range_supported",
    "imap_id_required",
    "imap_login",
    "imap_noop",
    "list_folders",
    "modified_utf7_folders",
}
SOURCE_MAILBOX_FEATURES = {
    "source_all_time_range_supported",
    "idle_supported",
    "pop_login",
    "pop_list",
    "synthesize_missing_date_header",
}
TARGET_MAILBOX_FEATURES = {
    "appenduid_supported",
    "append_internal_date_supported",
    "append_read_after_write",
    "date_header_roundtrip",
    "display_time_uses_append_time_without_internal_date",
    "effective_display_time_uses_append_time",
    "leading_received_header_added",
    "nul_rewrite_normalized",
    "omit_append_internal_date",
    "special_content_roundtrip",
    "target_folder_selectable",
    "target_all_time_range_supported",
    "uid_expunge_supported",
    "uidnext_available",
    "web_display_time_uses_import_time",
}


def dashboard_summary(db: sqlite3.Connection, state_path: Path) -> Dict[str, Any]:
    counts = {
        name: _count(db, table)
        for name, table in (
            ("accounts", "mail_accounts"),
            ("sources", "source_rules"),
            ("proxies", "proxy_profiles"),
            ("labels", "mail_label"),
            ("messages", "messages"),
            ("pending", "pending_operations"),
        )
    }
    schema_version = int(db.execute("PRAGMA user_version").fetchone()[0])
    return {
        "state_path": str(state_path),
        "schema_version": schema_version,
        "counts": counts,
        "recent_messages": recent_messages(db),
        "pending_operations": pending_operations(db),
    }


def list_accounts(db: sqlite3.Connection) -> List[sqlite3.Row]:
    return db.execute(
        """
        SELECT a.email, a.enabled, a.imap_server, a.imap_port,
               a.use_ssl, a.proxy_profile_id, p.name AS proxy_name,
               r.account_label, r.sync_interval, r.batch_size
        FROM mail_accounts a
        LEFT JOIN proxy_profiles p ON p.id = a.proxy_profile_id
        LEFT JOIN source_rules r ON r.email = a.email
        ORDER BY a.enabled DESC, a.sort_order, a.email, a.service_address
        """
    ).fetchall()


def list_proxy_profiles(db: sqlite3.Connection) -> List[sqlite3.Row]:
    return db.execute(
        """
        SELECT p.id, p.name, p.type, p.host, p.port, p.username,
               p.password IS NOT NULL AND p.password <> '' AS has_password,
               COUNT(a.email) AS account_count
        FROM proxy_profiles p
        LEFT JOIN mail_accounts a ON a.proxy_profile_id = p.id
        GROUP BY p.id
        ORDER BY p.sort_order, p.name
        """
    ).fetchall()


def save_proxy_profile(
    db: sqlite3.Connection,
    *,
    proxy_id: str,
    name: str,
    proxy_type: str,
    host: str,
    port: str,
    username: str,
    password: str,
) -> int:
    parsed_id = _optional_int(proxy_id, "代理", minimum=1)
    name = _clean_required(name, "代理名称")
    proxy_type = proxy_type.strip()
    host = _clean_required(host, "代理主机")
    parsed_port = _optional_int(port, "代理端口", minimum=1, maximum=65535)
    username_value = username.strip() or None
    password_value = password or None
    if proxy_type not in PROXY_TYPES:
        raise SyncError("代理类型无效")
    if parsed_port is None:
        raise SyncError("代理端口是必填项")

    try:
        if parsed_id is None:
            sort_order = (
                db.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM proxy_profiles"
                ).fetchone()[0]
                or 1
            )
            cursor = db.execute(
                """
                INSERT INTO proxy_profiles(
                    name, type, host, port, username, password, sort_order
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    proxy_type,
                    host,
                    parsed_port,
                    username_value,
                    password_value,
                    sort_order,
                ),
            )
            db.commit()
            return int(cursor.lastrowid)

        existing = db.execute(
            "SELECT * FROM proxy_profiles WHERE id=?",
            (parsed_id,),
        ).fetchone()
        if existing is None:
            raise SyncError("代理配置不存在")
        if password_value is None:
            password_value = existing["password"]
        db.execute(
            """
            UPDATE proxy_profiles
            SET name=?, type=?, host=?, port=?, username=?, password=?
            WHERE id=?
            """,
            (
                name,
                proxy_type,
                host,
                parsed_port,
                username_value,
                password_value,
                parsed_id,
            ),
        )
        db.commit()
        return parsed_id
    except sqlite3.IntegrityError as exc:
        raise SyncError("相同的代理配置已存在") from exc


def delete_proxy_profile(db: sqlite3.Connection, proxy_id: str) -> None:
    parsed_id = _optional_int(proxy_id, "代理", minimum=1)
    if parsed_id is None:
        raise SyncError("代理配置不存在")
    usage = db.execute(
        "SELECT COUNT(*) FROM mail_accounts WHERE proxy_profile_id=?",
        (parsed_id,),
    ).fetchone()[0]
    if usage:
        raise SyncError("代理已被邮箱使用，不能删除")
    cursor = db.execute("DELETE FROM proxy_profiles WHERE id=?", (parsed_id,))
    if cursor.rowcount == 0:
        raise SyncError("代理配置不存在")
    db.commit()


def list_source_rules(db: sqlite3.Connection) -> List[sqlite3.Row]:
    rows = db.execute(
        """
        SELECT s.email, s.account_label, s.default_folder_label,
               s.deleted_label, s.sync_interval, s.batch_size,
               s.skip_deleted_drafts, s.extra_json, s.enabled,
               a.imap_server,
               s.last_sync_status, s.last_sync_error_summary,
               s.last_sync_error_detail,
               COUNT(f.folder_name) AS folder_rule_count
        FROM source_rules s
        LEFT JOIN mail_accounts a
          ON a.rowid=(
              SELECT rowid
              FROM mail_accounts
              WHERE email=s.email
              ORDER BY enabled DESC, sort_order, service_address
              LIMIT 1
          )
        LEFT JOIN folder_rules f ON f.source_email = s.email
        GROUP BY s.email
        ORDER BY s.email
        """
    ).fetchall()
    return [_source_rule_summary(row) for row in rows]


def list_folder_rules(db: sqlite3.Connection) -> List[sqlite3.Row]:
    return db.execute(
        """
        SELECT source_email, folder_name, target_folder, folder_label, excluded,
               folder_labels_json, skip_uids_json
        FROM folder_rules
        ORDER BY source_email, folder_name
        """
    ).fetchall()


def sync_rules_detail(
    db: sqlite3.Connection,
    selected_email: str | None = None,
    selected_target_email: str | None = None,
    selected_source_service_address: str | None = None,
    selected_target_service_address: str | None = None,
) -> Dict[str, Any]:
    rules = list_source_rules(db)
    source_accounts = list_source_accounts(db)
    targets = list_target_accounts(db)
    target = selected_target_account(
        db,
        selected_target_email,
        selected_target_service_address,
    )

    source_email = selected_email or ""
    if not source_email and rules:
        source_email = str(rules[0]["email"])
    if not source_email and source_accounts:
        source_email = str(source_accounts[0]["email"])

    selected = None
    if source_email:
        selected = sync_rule_detail(
            db,
            source_email,
            selected_target_email=selected_target_email,
            selected_source_service_address=selected_source_service_address,
            selected_target_service_address=selected_target_service_address,
        )
        target_email = selected["target_email"] if selected else ""
        target_service_address = selected["target_service_address"] if selected else ""
    else:
        target_email = target["email"] if target is not None else ""
        target_service_address = target["service_address"] if target is not None else ""

    return {
        "rules": rules,
        "rule_details": [
            detail
            for detail in (
                sync_rule_detail(
                    db,
                    str(row["email"]),
                    selected_target_email=selected_target_email,
                    selected_source_service_address=(
                        selected_source_service_address
                        if str(row["email"]) == source_email
                        else None
                    ),
                    selected_target_service_address=selected_target_service_address,
                )
                for row in rules
            )
            if detail is not None
        ],
        "source_accounts": source_accounts,
        "target_accounts": targets,
        "selected": selected,
        "selected_email": source_email,
        "labels": list_synology_labels(
            db,
            target_email,
            target_service_address,
        ),
        "all_labels": list_all_synology_labels(db),
        "available_sources": _available_rule_sources(source_accounts, rules),
        "defaults": _sync_rule_defaults(),
    }


def _sync_rule_defaults() -> dict[str, int]:
    return {
        "sync_interval": _env_default_int(
            "SYNC_INTERVAL",
            DEFAULT_SYNC_INTERVAL_SECONDS,
        ),
        "batch_size": _env_default_int(
            "SYNC_BATCH_SIZE",
            DEFAULT_SYNC_BATCH_SIZE,
        ),
    }


def _env_default_int(name: str, fallback: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(fallback))))
    except (TypeError, ValueError):
        return fallback


def list_source_accounts(db: sqlite3.Connection) -> List[sqlite3.Row]:
    return db.execute(
        """
        SELECT a.email, a.service_address, a.enabled, a.imap_server, a.imap_port,
               a.use_ssl, a.proxy_profile_id, p.name AS proxy_name,
               a.pop_fallback_json, a.imap_id_json, a.imap_timeout,
               a.sort_order
        FROM mail_accounts a
        LEFT JOIN proxy_profiles p ON p.id = a.proxy_profile_id
        ORDER BY a.enabled DESC, a.sort_order, a.email, a.service_address
        """
    ).fetchall()


def list_mailbox_accounts(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """
        SELECT a.email, a.service_address, a.enabled,
               a.imap_server, a.imap_port,
               a.use_ssl, a.proxy_profile_id, p.name AS proxy_name,
               a.password IS NOT NULL AND a.password <> '' AS has_password,
               a.auth_json, a.pop_fallback_json, a.protocols_json,
               a.smtp_json, a.imap_timeout, a.extra_json,
               a.target_folder, a.sort_order,
               COUNT(l.label_id) AS label_count
        FROM mail_accounts a
        LEFT JOIN proxy_profiles p ON p.id = a.proxy_profile_id
        LEFT JOIN mail_label l
          ON l.target_email = a.email
         AND l.target_service_address = a.service_address
        GROUP BY a.email, a.service_address
        ORDER BY a.enabled DESC, a.sort_order, a.email, a.service_address
        """
    ).fetchall()
    result = []
    for row in rows:
        item = _mailbox_summary(row)
        binding = _mailbox_rule_binding(
            db,
            str(row["email"] or ""),
            str(row["service_address"] or ""),
        )
        item["rule_binding"] = binding or ""
        item["is_rule_bound"] = binding is not None
        result.append(item)
    return result


def selected_mailbox_account(
    db: sqlite3.Connection,
    role: str | None,
    email: str | None,
    service_address: str | None = None,
) -> dict | None:
    if not email:
        return None
    service_address = (service_address or "").strip()
    service_filter = "AND a.service_address=?" if service_address else ""
    params: tuple[object, ...] = (
        (email, service_address) if service_address else (email,)
    )
    row = db.execute(
        f"""
        SELECT a.*, p.name AS proxy_name,
               a.password IS NOT NULL AND a.password <> '' AS has_password
        FROM mail_accounts a
        LEFT JOIN proxy_profiles p ON p.id = a.proxy_profile_id
        WHERE a.email=?
          {service_filter}
        ORDER BY a.enabled DESC, a.sort_order, a.service_address
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return None
    return _mailbox_detail(row)


def mailbox_account_profile(
    db: sqlite3.Connection,
    role: str,
    email: str,
    service_address: str | None = None,
) -> dict:
    service_address = (service_address or "").strip()
    service_filter = "AND service_address=?" if service_address else ""
    params: tuple[object, ...] = (
        (email, service_address) if service_address else (email,)
    )
    row = db.execute(
        f"""
        SELECT *
        FROM mail_accounts
        WHERE email=?
          {service_filter}
        ORDER BY enabled DESC, sort_order, service_address
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        raise SyncError("Mailbox is not configured")
    protocols = _mailbox_protocols(row)
    if "imap" not in protocols:
        raise SyncError("IMAP is not enabled for this mailbox")
    profile = _account_profile_from_row(db, row)
    if role == "target":
        profile["folder"] = row["target_folder"] or "INBOX"
    return profile


def save_mailbox_account(
    db: sqlite3.Connection,
    *,
    original_role: str,
    original_email: str,
    role: str,
    email: str,
    enabled: bool,
    auth_provider: str,
    imap_server: str,
    imap_port: str,
    use_ssl: bool,
    password: str,
    oauth_client_id: str,
    oauth_token_file: str,
    target_folder: str,
    imap_timeout: str,
    proxy_profile_id: str,
    pop_fallback_enabled: bool,
    pop_server: str,
    pop_port: str,
    pop_use_ssl: bool,
    pop_timeout: str,
    original_service_address: str = "",
    service_address: str = "",
    imap_enabled: bool = True,
    smtp_enabled: bool = False,
    smtp_server: str = "",
    smtp_port: str = "",
    smtp_use_ssl: bool = True,
    smtp_timeout: str = "",
    oauth_token: dict | None = None,
    probe_results_json: str = "",
    realtime_poll_interval: str | None = None,
) -> str:
    original_role = original_role.strip()
    original_email = original_email.strip()
    original_service_address = original_service_address.strip()
    email = email.strip()
    if not email and original_email:
        email = original_email
    auth_provider = auth_provider.strip() or "manual"
    imap_server = imap_server.strip()
    service_address = service_address.strip()
    target_folder = target_folder.strip() or "INBOX"
    if not email:
        raise SyncError("Mailbox email is required")
    if auth_provider not in {"manual", "outlook"}:
        raise SyncError("Mailbox auth type is invalid")
    if auth_provider == "outlook":
        if not imap_server:
            imap_server = "outlook.office365.com"
        if not imap_port.strip():
            imap_port = "993"
        use_ssl = True

    existing = None
    if original_email:
        service_filter = (
            "AND service_address=?" if original_service_address else ""
        )
        params: tuple[object, ...] = (
            (original_email, original_service_address)
            if original_service_address
            else (original_email,)
        )
        existing = db.execute(
            f"""
            SELECT *
            FROM mail_accounts
            WHERE email=?
              {service_filter}
            ORDER BY enabled DESC, sort_order, service_address
            LIMIT 1
            """,
            params,
        ).fetchone()

    if not service_address:
        service_address = original_service_address or imap_server
    if existing is not None and not service_address:
        service_address = existing["service_address"] or existing["imap_server"] or ""
    if (
        existing is not None
        and pop_fallback_enabled
        and not pop_server.strip()
        and not imap_enabled
        and "imap" in _mailbox_protocols(existing)
    ):
        imap_enabled = True
        if not imap_server:
            imap_server = existing["imap_server"] or existing["service_address"] or ""
        if not imap_port.strip() and existing["imap_port"]:
            imap_port = str(existing["imap_port"])
        use_ssl = bool(existing["use_ssl"])
    if not service_address:
        raise SyncError("Service address is required")
    if imap_enabled and not imap_server:
        imap_server = service_address
    if imap_enabled and not imap_server:
        raise SyncError("IMAP server is required when IMAP is enabled")

    parsed_port = _optional_int(imap_port, "IMAP port", minimum=1, maximum=65535)
    parsed_timeout = _optional_int(imap_timeout, "IMAP timeout", minimum=1)
    parsed_proxy_id = _optional_int(proxy_profile_id, "Proxy profile", minimum=1)
    if parsed_proxy_id is not None and not _proxy_exists(db, parsed_proxy_id):
        raise SyncError("Selected proxy profile does not exist")

    if existing is not None and original_email != email:
        if _source_rule_exists(db, original_email):
            raise SyncError("Remove the sync task before changing source email")

    existing_email = existing["email"] if existing is not None else original_email
    existing_service = (
        existing["service_address"]
        if existing is not None
        else original_service_address
    )
    conflict = db.execute(
        """
        SELECT 1
        FROM mail_accounts
        WHERE email=? AND service_address=?
          AND NOT (email=? AND service_address=?)
        LIMIT 1
        """,
        (
            email,
            service_address,
            existing_email,
            existing_service,
        ),
    ).fetchone()
    if conflict is not None:
        raise SyncError(
            "A mailbox with this email and service address already exists"
        )
    imap_server_for_storage = (
        imap_server
        or (existing["imap_server"] if existing is not None else "")
        or service_address
    )

    saved_password = password if password else (
        existing["password"] if existing is not None else None
    )
    auth_json = _auth_json(
        auth_provider,
        oauth_client_id,
        oauth_token_file,
        existing["auth_json"] if existing is not None else None,
        oauth_token,
    )
    if auth_json is None and not saved_password:
        raise SyncError("Password is required for manual IMAP accounts")
    pop_fallback_json = _pop_fallback_json(
        enabled=pop_fallback_enabled,
        server=pop_server,
        port=pop_port,
        use_ssl=pop_use_ssl,
        timeout=pop_timeout,
    )
    smtp_json = _smtp_json(
        enabled=smtp_enabled,
        server=smtp_server,
        port=smtp_port,
        use_ssl=smtp_use_ssl,
        timeout=smtp_timeout,
    )
    protocols_json = _protocols_json(
        imap_enabled=imap_enabled,
        pop_enabled=pop_fallback_json is not None,
        smtp_enabled=smtp_json is not None,
    )

    imap_id_json = existing["imap_id_json"] if existing is not None else None
    extra = _json_object(existing["extra_json"]) if existing is not None else {}
    if realtime_poll_interval is not None:
        parsed_realtime_poll_interval = _optional_int(
            realtime_poll_interval,
            "Realtime poll interval",
            minimum=1,
            maximum=3600,
        )
        if parsed_realtime_poll_interval is None:
            extra.pop("realtime_poll_interval", None)
        else:
            extra["realtime_poll_interval"] = parsed_realtime_poll_interval
    probe_results = _parse_probe_results_json(probe_results_json)
    if probe_results:
        _merge_mailbox_probe_results(extra, probe_results)
    extra_json = _json_dump(extra)
    sort_order = int(existing["sort_order"]) if existing is not None else 0
    if existing is None:
        sort_order = _next_mailbox_sort_order(db)

    try:
        with db:
            if (
                existing is not None
                and (existing_email != email or existing_service != service_address)
            ):
                db.execute(
                    """
                    UPDATE mail_label
                    SET target_email=?, target_service_address=?
                    WHERE target_email=? AND target_service_address=?
                    """,
                    (email, service_address, existing_email, existing_service),
                )
                _rename_rule_target_references(
                    db,
                    existing_email,
                    new_email=email,
                    old_service_address=existing_service,
                    new_service_address=service_address,
                )
                _rename_rule_source_reference(
                    db,
                    existing_email,
                    old_service_address=existing_service,
                    new_service_address=service_address,
                )
            values = (
                email,
                service_address,
                1 if enabled else 0,
                imap_server_for_storage,
                parsed_port,
                1 if use_ssl else 0,
                saved_password,
                auth_json,
                parsed_proxy_id,
                imap_id_json,
                pop_fallback_json,
                protocols_json,
                smtp_json,
                parsed_timeout,
                target_folder,
                extra_json,
                sort_order,
            )
            if existing is not None:
                db.execute(
                    """
                    UPDATE mail_accounts
                    SET email=?, service_address=?, enabled=?, imap_server=?,
                        imap_port=?, use_ssl=?, password=?, auth_json=?,
                        proxy_profile_id=?, imap_proxy_json=NULL,
                        imap_id_json=?, pop_fallback_json=?,
                        protocols_json=?, smtp_json=?, imap_timeout=?,
                        target_folder=?, extra_json=?, sort_order=?
                    WHERE email=? AND service_address=?
                    """,
                    values + (existing_email, existing_service),
                )
            else:
                db.execute(
                    """
                    INSERT INTO mail_accounts(
                        email, service_address, enabled, imap_server,
                        imap_port, use_ssl, password, auth_json,
                        proxy_profile_id, imap_proxy_json, imap_id_json,
                        pop_fallback_json, protocols_json, smtp_json,
                        imap_timeout, target_folder, extra_json, sort_order
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
    except sqlite3.IntegrityError as exc:
        raise SyncError(
            "A mailbox with this email and service address already exists"
        ) from exc
    return email


def save_mailbox_probe_results(
    db: sqlite3.Connection,
    email: str,
    service_address: str,
    probe_results: dict,
) -> bool:
    email = email.strip()
    service_address = service_address.strip()
    if not email:
        return False
    service_filter = "AND service_address=?" if service_address else ""
    params: tuple[object, ...] = (
        (email, service_address) if service_address else (email,)
    )
    row = db.execute(
        f"""
        SELECT email, service_address, extra_json
        FROM mail_accounts
        WHERE email=?
          {service_filter}
        ORDER BY enabled DESC, sort_order, service_address
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return False
    extra = _json_object(row["extra_json"])
    _merge_mailbox_probe_results(extra, probe_results)
    db.execute(
        """
        UPDATE mail_accounts
        SET extra_json=?
        WHERE email=? AND service_address=?
        """,
        (
            _json_dump(extra),
            row["email"],
            row["service_address"],
        ),
    )
    db.commit()
    return True


def delete_mailbox_account(
    db: sqlite3.Connection,
    *,
    role: str,
    email: str,
    service_address: str = "",
) -> None:
    role = role.strip()
    email = email.strip()
    service_address = service_address.strip()
    service_filter = "AND service_address=?" if service_address else ""
    params: tuple[object, ...] = (
        (email, service_address) if service_address else (email,)
    )
    row = db.execute(
        f"""
        SELECT service_address
        FROM mail_accounts
        WHERE email=?
          {service_filter}
        ORDER BY enabled DESC, sort_order, service_address
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        raise SyncError("Mailbox no longer exists")
    service_address = row["service_address"]
    binding = _mailbox_rule_binding(db, email, service_address)
    if binding == "source":
        raise SyncError("邮箱已绑定同步规则，不能删除")
    if binding == "target":
        raise SyncError("邮箱已绑定同步规则，不能删除")
    with db:
        db.execute(
            """
            DELETE FROM mail_label
            WHERE target_email=? AND target_service_address=?
            """,
            (email, service_address),
        )
        db.execute(
            """
            DELETE FROM mail_accounts
            WHERE email=? AND service_address=?
            """,
            (email, service_address),
        )


def sync_rule_detail(
    db: sqlite3.Connection,
    source_email: str,
    *,
    selected_target_email: str | None = None,
    selected_source_service_address: str | None = None,
    selected_target_service_address: str | None = None,
) -> dict | None:
    rule = db.execute(
        """
        SELECT *
        FROM source_rules
        WHERE email=?
        """,
        (source_email,),
    ).fetchone()
    rule_extra = _json_object(rule["extra_json"]) if rule is not None else {}
    source_service_address = str(
        selected_source_service_address
        or rule_extra.get("source_service_address")
        or ""
    )
    service_filter = "AND a.service_address=?" if source_service_address else ""
    account_params: tuple[object, ...] = (
        (source_email, source_service_address)
        if source_service_address
        else (source_email,)
    )
    account = db.execute(
        f"""
        SELECT a.email, a.enabled, a.imap_server, a.imap_port,
               a.service_address, a.use_ssl, a.proxy_profile_id,
               p.name AS proxy_name,
               a.password IS NOT NULL AND a.password <> '' AS has_password,
               a.auth_json IS NOT NULL AND a.auth_json <> '' AS has_auth,
               a.pop_fallback_json, a.imap_id_json, a.imap_timeout,
               a.sort_order
        FROM mail_accounts a
        LEFT JOIN proxy_profiles p ON p.id = a.proxy_profile_id
        WHERE a.email=?
          {service_filter}
        ORDER BY a.enabled DESC, a.sort_order, a.service_address
        LIMIT 1
        """,
        account_params,
    ).fetchone()
    if account is None:
        return None
    source_service_address = account["service_address"] or ""
    default_target = selected_target_account(
        db,
        selected_target_email,
        selected_target_service_address,
    )
    target_email = (
        str(rule_extra.get("target_email") or "")
        or (default_target["email"] if default_target is not None else "")
    )
    target_service_address = str(rule_extra.get("target_service_address") or "")
    if selected_target_email and rule is None:
        target_email = selected_target_email
        target_service_address = selected_target_service_address or ""
    target_account = selected_target_account(
        db,
        target_email,
        target_service_address,
    )
    if target_account is not None:
        target_service_address = target_account["service_address"] or ""
    pop_fallback = _json_object(account["pop_fallback_json"])
    pop_fallback_available = bool(pop_fallback)
    pop_fallback_enabled = pop_fallback_available and _json_truthy(
        rule_extra.get("pop_fallback_enabled"),
        default=pop_fallback_available,
    )
    status = _source_rule_health(db, source_email)
    folder_rows = db.execute(
        """
        SELECT source_email, folder_name, target_folder, folder_label, excluded,
               folder_labels_json, skip_uids_json
        FROM folder_rules
        WHERE source_email=?
        ORDER BY folder_name
        """,
        (source_email,),
    ).fetchall()
    folder_display_metadata = _folder_display_metadata_from_extra(rule_extra)
    return {
        "account": account,
        "exists": rule is not None,
        "email": source_email,
        "source_service_address": source_service_address,
        "target_email": target_email,
        "target_service_address": target_service_address,
        "enabled": bool(_row_or_default(rule, "enabled", 1)),
        "account_label": _row_or_default(
            rule,
            "account_label",
            f"SYNC:{source_email}",
        ),
        "default_folder_label": _row_or_default(
            rule,
            "default_folder_label",
            "",
        ),
        "deleted_label": _row_or_default(rule, "deleted_label", ""),
        "sync_interval": _row_or_default(rule, "sync_interval", ""),
        "batch_size": _row_or_default(rule, "batch_size", ""),
        "skip_deleted_drafts": bool(
            _row_or_default(rule, "skip_deleted_drafts", 0)
        ),
        "sync_mode": str(rule_extra.get("sync_mode") or "realtime"),
        "date_policy": str(rule_extra.get("date_policy") or "auto"),
        "out_of_scope_policy": _out_of_scope_policy(rule_extra),
        "out_of_scope_label": (_out_of_scope_labels(rule_extra) or [""])[0],
        "out_of_scope_labels": _out_of_scope_labels(rule_extra),
        "pop_fallback": pop_fallback,
        "pop_fallback_available": pop_fallback_available,
        "pop_fallback_enabled": pop_fallback_enabled,
        "folder_rules": [
            _folder_rule_form_row(row, folder_display_metadata)
            for row in folder_rows
        ],
        "has_sync_logs": _sync_rule_has_logs(db, source_email),
        **status,
    }


def save_sync_rule(
    db: sqlite3.Connection,
    *,
    original_email: str,
    source_email: str,
    target_email: str,
    enabled: bool,
    account_label: str,
    default_folder_label: str,
    deleted_label: str,
    sync_interval: str,
    batch_size: str,
    skip_deleted_drafts: bool,
    sync_mode: str,
    date_policy: str,
    pop_fallback_enabled: bool,
    pop_server: str,
    pop_port: str,
    pop_use_ssl: bool,
    pop_timeout: str,
    folder_rules: Sequence[dict],
    out_of_scope_policy: str = "keep",
    out_of_scope_label: str | Sequence[str] = "",
    source_service_address: str = "",
    target_service_address: str = "",
) -> str:
    original_email = original_email.strip()
    source_email = source_email.strip()
    target_email = target_email.strip()
    source_service_address = source_service_address.strip()
    target_service_address = target_service_address.strip()
    if not source_email:
        raise SyncError("Source mailbox is required")
    source_service_address = _require_source_account(
        db,
        source_email,
        source_service_address,
    )
    target_service_address = _require_target(
        db,
        target_email,
        target_service_address,
    )
    if not source_service_address:
        raise SyncError("Source mailbox is not configured")

    existing = None
    if original_email:
        existing = db.execute(
            """
            SELECT account_label, default_folder_label, extra_json
            FROM source_rules
            WHERE email=?
            """,
            (original_email,),
        ).fetchone()
    existing_extra = _json_object(existing["extra_json"]) if existing is not None else {}

    duplicate = _sync_rule_pair_duplicate(
        db,
        original_email=original_email if existing is not None else "",
        source_email=source_email,
        source_service_address=source_service_address,
        target_email=target_email,
        target_service_address=target_service_address,
    )
    if duplicate is not None:
        raise SyncError("该来源邮箱和目标邮箱的同步规则已存在，不能重复添加")

    if existing is not None and _sync_rule_has_logs(db, original_email):
        extra = existing_extra
        current_source_service = str(
            extra.get("source_service_address")
            or _mailbox_service_address(db, original_email)
            or ""
        ).strip()
        current_target_email, current_target_service = _rule_target_identity(
            db,
            extra,
        )
        if _mailbox_identity_changed(
            original_email,
            current_source_service,
            source_email,
            source_service_address,
        ):
            raise SyncError("已有同步日志，不能修改来源邮箱")
        if _mailbox_identity_changed(
            current_target_email,
            current_target_service,
            target_email,
            target_service_address,
        ):
            raise SyncError("已有同步日志，不能修改目标邮箱")

    account_label = account_label.strip()
    default_folder_label = default_folder_label.strip()
    deleted_label = deleted_label.strip()
    parsed_interval = _optional_int(sync_interval, "Sync interval", minimum=1)
    parsed_batch = _optional_int(batch_size, "Batch size", minimum=1)
    if sync_mode not in SYNC_MODES:
        raise SyncError("Sync mode is invalid")
    if date_policy not in DATE_POLICIES:
        raise SyncError("Date handling policy is invalid")
    out_of_scope_policy = _out_of_scope_policy(
        {"out_of_scope_policy": out_of_scope_policy}
    )
    out_of_scope_labels = _unique_texts(_folder_label_list(out_of_scope_label))
    if out_of_scope_policy == "label":
        if not out_of_scope_labels:
            raise SyncError("请选择移出范围后的标记标签")
        for label in out_of_scope_labels:
            if not _target_label_exists(
                db,
                target_email,
                target_service_address,
                label,
            ):
                raise SyncError("移出范围标记标签不属于目标邮箱")
    else:
        out_of_scope_labels = []

    source_pop_fallback_json = _source_pop_fallback_json(
        db,
        source_email,
        source_service_address,
    )
    submitted_pop_fallback_json = (
        _pop_fallback_json(
            enabled=True,
            server=pop_server,
            port=pop_port,
            use_ssl=pop_use_ssl,
            timeout=pop_timeout,
        )
        if pop_fallback_enabled and pop_server.strip()
        else None
    )
    effective_pop_fallback_json = (
        submitted_pop_fallback_json or source_pop_fallback_json
    )
    pop_fallback_active = bool(pop_fallback_enabled and effective_pop_fallback_json)
    normalized_folders = _normalize_folder_rules(folder_rules)
    folder_display_metadata = _merged_folder_display_metadata(
        existing_extra,
        folder_rules,
        [folder["folder_name"] for folder in normalized_folders],
    )

    if original_email and original_email != source_email:
        duplicate = db.execute(
            "SELECT 1 FROM source_rules WHERE email=?",
            (source_email,),
        ).fetchone()
        if duplicate is not None:
            raise SyncError("Selected source mailbox already has a sync task")

    extra = dict(existing_extra)
    extra.update(
        {
            "target_email": target_email,
            "target_service_address": target_service_address,
            "source_service_address": source_service_address,
            "sync_mode": sync_mode,
            "date_policy": date_policy,
            "out_of_scope_policy": out_of_scope_policy,
            "out_of_scope_label": out_of_scope_labels[0] if out_of_scope_labels else "",
            "out_of_scope_labels": out_of_scope_labels,
            "pop_fallback_enabled": pop_fallback_active,
        }
    )
    if folder_display_metadata:
        extra["folder_display_metadata"] = folder_display_metadata
    else:
        extra.pop("folder_display_metadata", None)

    with db:
        if submitted_pop_fallback_json is not None:
            db.execute(
                """
                UPDATE mail_accounts
                SET pop_fallback_json=?
                WHERE email=? AND service_address=?
                """,
                (
                    submitted_pop_fallback_json,
                    source_email,
                    source_service_address,
                ),
            )
        if original_email and original_email != source_email:
            db.execute(
                "DELETE FROM folder_rules WHERE source_email=?",
                (original_email,),
            )
            db.execute(
                "DELETE FROM source_rules WHERE email=?",
                (original_email,),
            )
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
                source_email,
                1 if enabled else 0,
                account_label,
                default_folder_label,
                deleted_label,
                parsed_interval,
                parsed_batch,
                1 if skip_deleted_drafts else 0,
                _json_dump(extra),
            ),
        )
        db.execute(
            "DELETE FROM folder_rules WHERE source_email=?",
            (source_email,),
        )
        for folder in normalized_folders:
            db.execute(
                """
                INSERT INTO folder_rules(
                    source_email, folder_name, target_folder, folder_label, excluded,
                    folder_labels_json, skip_uids_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_email,
                    folder["folder_name"],
                    folder["target_folder"],
                    folder["folder_label"],
                    1 if folder["excluded"] else 0,
                    folder["folder_labels_json"],
                    folder["skip_uids_json"],
                ),
            )
    return source_email


def set_sync_rule_enabled(
    db: sqlite3.Connection,
    source_email: str,
    enabled: bool,
) -> None:
    if not _source_rule_exists(db, source_email):
        raise SyncError("Sync task no longer exists")
    with db:
        db.execute(
            """
            UPDATE source_rules
            SET enabled=?
            WHERE email=?
            """,
            (1 if enabled else 0, source_email),
        )


def delete_sync_rule(db: sqlite3.Connection, source_email: str) -> None:
    if not _source_rule_exists(db, source_email):
        raise SyncError("Sync task no longer exists")
    with db:
        db.execute(
            "DELETE FROM message_locations WHERE source_email=?",
            (source_email,),
        )
        db.execute(
            "DELETE FROM pending_operations WHERE source_email=?",
            (source_email,),
        )
        db.execute(
            "DELETE FROM messages WHERE source_email=?",
            (source_email,),
        )
        db.execute(
            "DELETE FROM folder_rules WHERE source_email=?",
            (source_email,),
        )
        db.execute("DELETE FROM source_rules WHERE email=?", (source_email,))


def delete_sync_rule_logs(
    db: sqlite3.Connection,
    source_email: str,
    success_hashes: Sequence[str] = (),
    failed_hashes: Sequence[str] = (),
) -> dict[str, int]:
    source_email = source_email.strip()
    if not source_email:
        raise SyncError("源邮箱不能为空")
    success_hashes = _clean_content_hashes(success_hashes)
    failed_hashes = _clean_content_hashes(failed_hashes)
    if not success_hashes and not failed_hashes:
        raise SyncError("请先选择要删除的日志")

    deleted_success = 0
    deleted_failed = 0
    with db:
        for content_hash in success_hashes:
            db.execute(
                """
                DELETE FROM message_locations
                WHERE source_email=? AND content_hash=?
                """,
                (source_email, content_hash),
            )
            cursor = db.execute(
                """
                DELETE FROM messages
                WHERE source_email=? AND content_hash=?
                """,
                (source_email, content_hash),
            )
            deleted_success += max(0, cursor.rowcount)
        for content_hash in failed_hashes:
            cursor = db.execute(
                """
                DELETE FROM pending_operations
                WHERE source_email=? AND content_hash=?
                """,
                (source_email, content_hash),
            )
            deleted_failed += max(0, cursor.rowcount)
    return {
        "deleted_success": deleted_success,
        "deleted_failed": deleted_failed,
    }


def _clean_content_hashes(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if len(text) > 128 or any(char.isspace() for char in text):
            raise SyncError("日志标识无效")
        if text not in result:
            result.append(text)
    return result


def source_account_profile(db: sqlite3.Connection, source_email: str) -> dict:
    row = db.execute(
        """
        SELECT *
        FROM mail_accounts
        WHERE email=?
        ORDER BY enabled DESC, sort_order, service_address
        LIMIT 1
        """,
        (source_email,),
    ).fetchone()
    if row is None:
        raise SyncError("Source mailbox is not configured")
    return _account_profile_from_row(db, row)


def list_target_accounts(db: sqlite3.Connection) -> List[sqlite3.Row]:
    return db.execute(
        """
        SELECT a.email, a.service_address, a.enabled, a.imap_server, a.imap_port,
               a.use_ssl, a.proxy_profile_id, p.name AS proxy_name,
               a.password IS NOT NULL AND a.password <> '' AS has_password,
               a.auth_json IS NOT NULL AND a.auth_json <> '' AS has_auth,
               a.imap_timeout, a.target_folder, a.sort_order
        FROM mail_accounts a
        LEFT JOIN proxy_profiles p ON p.id = a.proxy_profile_id
        ORDER BY a.enabled DESC, a.sort_order, a.email, a.service_address
        """
    ).fetchall()


def selected_target_account(
    db: sqlite3.Connection,
    target_email: str | None = None,
    target_service_address: str | None = None,
) -> sqlite3.Row | None:
    if target_email:
        target_service_address = (target_service_address or "").strip()
        service_filter = "AND a.service_address=?" if target_service_address else ""
        params: tuple[object, ...] = (
            (target_email, target_service_address)
            if target_service_address
            else (target_email,)
        )
        row = db.execute(
            f"""
            SELECT a.email, a.service_address, a.enabled, a.imap_server, a.imap_port,
                   a.use_ssl, a.proxy_profile_id, p.name AS proxy_name,
                   a.password IS NOT NULL AND a.password <> '' AS has_password,
                   a.auth_json IS NOT NULL AND a.auth_json <> '' AS has_auth,
                   a.imap_timeout, a.target_folder, a.sort_order
            FROM mail_accounts a
            LEFT JOIN proxy_profiles p ON p.id = a.proxy_profile_id
            WHERE a.email=?
              {service_filter}
            ORDER BY a.enabled DESC, a.sort_order, a.service_address
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is not None:
            return row
    return db.execute(
        """
        SELECT a.email, a.service_address, a.enabled, a.imap_server, a.imap_port,
               a.use_ssl, a.proxy_profile_id, p.name AS proxy_name,
               a.password IS NOT NULL AND a.password <> '' AS has_password,
               a.auth_json IS NOT NULL AND a.auth_json <> '' AS has_auth,
               a.imap_timeout, a.target_folder, a.sort_order
        FROM mail_accounts a
        LEFT JOIN proxy_profiles p ON p.id = a.proxy_profile_id
        ORDER BY a.enabled DESC, a.sort_order, a.email, a.service_address
        LIMIT 1
        """
    ).fetchone()


def target_mailbox_detail(
    db: sqlite3.Connection,
    target_email: str | None = None,
    selected_role: str | None = None,
    dialog: str | None = None,
    selected_service_address: str | None = None,
) -> Dict[str, Any]:
    mailboxes = list_mailbox_accounts(db)
    selected = selected_mailbox_account(
        db,
        selected_role,
        target_email,
        selected_service_address,
    )
    if selected is None and mailboxes:
        selected = selected_mailbox_account(
            db,
            None,
            str(mailboxes[0]["email"]),
            str(mailboxes[0]["service_address"]),
        )
    selected_email = selected["email"] if selected is not None else ""
    selected_service = selected["service_address"] if selected is not None else ""
    return {
        "mailboxes": mailboxes,
        "targets": list_target_accounts(db),
        "target": selected,
        "mailbox": selected,
        "labels": list_synology_labels(db, selected_email, selected_service),
        "proxies": list_proxy_profiles(db),
        "dialog": dialog or "",
    }


def target_account_profile(
    db: sqlite3.Connection,
    target_email: str,
) -> dict:
    row = db.execute(
        """
        SELECT *
        FROM mail_accounts
        WHERE email=?
        ORDER BY enabled DESC, sort_order, service_address
        LIMIT 1
        """,
        (target_email,),
    ).fetchone()
    if row is None:
        raise SyncError("Target mailbox is not configured")
    profile = _json_object(row["extra_json"])
    profile.update(
        {
            "email": row["email"],
            "service_address": row["service_address"],
            "imap_server": row["imap_server"],
            "use_ssl": bool(row["use_ssl"]),
            "folder": row["target_folder"] or "INBOX",
        }
    )
    protocols = _mailbox_protocols(row)
    if protocols:
        profile["protocols"] = protocols
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
        value = _json_object(row[column])
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


def save_target_account(
    db: sqlite3.Connection,
    *,
    original_email: str,
    email: str,
    enabled: bool,
    imap_server: str,
    imap_port: str,
    use_ssl: bool,
    password: str,
    target_folder: str,
    imap_timeout: str,
    proxy_profile_id: str,
) -> str:
    return save_mailbox_account(
        db,
        original_role="target",
        original_email=original_email,
        role="target",
        email=email,
        enabled=enabled,
        auth_provider="manual",
        imap_server=imap_server,
        imap_port=imap_port,
        use_ssl=use_ssl,
        password=password,
        oauth_client_id="",
        oauth_token_file="",
        target_folder=target_folder,
        imap_timeout=imap_timeout,
        proxy_profile_id=proxy_profile_id,
        pop_fallback_enabled=False,
        pop_server="",
        pop_port="",
        pop_use_ssl=True,
        pop_timeout="",
    )


def list_synology_labels(
    db: sqlite3.Connection,
    target_email: str | None = None,
    target_service_address: str | None = None,
) -> List[dict[str, Any]]:
    if target_email is None:
        target = selected_target_account(db)
        target_email = target["email"] if target is not None else ""
        target_service_address = (
            target["service_address"] if target is not None else ""
        )
    if target_service_address is None:
        target_service_address = _mailbox_service_address(db, target_email)
    rows = db.execute(
        """
        SELECT l.target_email, l.target_service_address, l.label_id, l.label_name
        FROM mail_label
        AS l
        WHERE l.target_email=? AND l.target_service_address=?
        ORDER BY l.label_name
        """,
        (target_email, target_service_address),
    ).fetchall()
    return [
        {
            "target_email": row["target_email"],
            "target_service_address": row["target_service_address"],
            "label_id": row["label_id"],
            "label_name": row["label_name"],
            "usage_count": _label_usage_count(db, row["label_name"]),
        }
        for row in rows
    ]


def list_all_synology_labels(db: sqlite3.Connection) -> List[sqlite3.Row]:
    return db.execute(
        """
        SELECT target_email, target_service_address, label_id, label_name
        FROM mail_label
        ORDER BY target_email, target_service_address, label_name
        """
    ).fetchall()


def create_synology_label(
    db: sqlite3.Connection,
    *,
    target_email: str,
    target_service_address: str | None = None,
    label_id: str,
    label_name: str,
) -> None:
    target_service_address = _require_label_owner(
        db,
        target_email,
        target_service_address,
    )
    label_id = _clean_label_id(label_id)
    label_name = _clean_label_name(label_name)
    try:
        with db:
            db.execute(
                """
                INSERT INTO mail_label(
                    target_email, target_service_address, label_id, label_name
                )
                VALUES(?, ?, ?, ?)
                """,
                (target_email, target_service_address, label_id, label_name),
            )
    except sqlite3.IntegrityError as exc:
        raise SyncError("Label ID or display name already exists") from exc


def update_synology_label(
    db: sqlite3.Connection,
    *,
    target_email: str,
    target_service_address: str | None = None,
    original_label_id: str,
    label_id: str,
    label_name: str,
) -> None:
    target_service_address = _require_label_owner(
        db,
        target_email,
        target_service_address,
    )
    original_label_id = _clean_label_id(original_label_id)
    label_id = _clean_label_id(label_id)
    label_name = _clean_label_name(label_name)
    existing = db.execute(
        """
        SELECT label_name
        FROM mail_label
        WHERE target_email=? AND target_service_address=? AND label_id=?
        """,
        (target_email, target_service_address, original_label_id),
    ).fetchone()
    if existing is None:
        raise SyncError("Label mapping no longer exists")
    try:
        with db:
            db.execute(
                """
                UPDATE mail_label
                SET label_id=?, label_name=?
                WHERE target_email=? AND target_service_address=? AND label_id=?
                """,
                (
                    label_id,
                    label_name,
                    target_email,
                    target_service_address,
                    original_label_id,
                ),
            )
            if label_name != existing["label_name"]:
                _rename_label_references(db, existing["label_name"], label_name)
    except sqlite3.IntegrityError as exc:
        raise SyncError("Label ID or display name already exists") from exc


def delete_synology_label(
    db: sqlite3.Connection,
    *,
    target_email: str,
    target_service_address: str | None = None,
    label_id: str,
) -> None:
    target_service_address = _require_label_owner(
        db,
        target_email,
        target_service_address,
    )
    label_id = _clean_label_id(label_id)
    row = db.execute(
        """
        SELECT label_name
        FROM mail_label
        WHERE target_email=? AND target_service_address=? AND label_id=?
        """,
        (target_email, target_service_address, label_id),
    ).fetchone()
    if row is None:
        raise SyncError("Label mapping no longer exists")
    usage_count = _label_usage_count(db, row["label_name"])
    if usage_count:
        raise SyncError(
            f"Label is still used by {usage_count} rule reference(s)"
        )
    with db:
        db.execute(
            """
            DELETE FROM mail_label
            WHERE target_email=? AND target_service_address=? AND label_id=?
            """,
            (target_email, target_service_address, label_id),
        )


def sync_rule_logs(
    db: sqlite3.Connection,
    source_email: str,
    success_page: int = 1,
    failed_page: int = 1,
    page_size: int = 10,
    keyword: str = "",
) -> Dict[str, Any]:
    source_email = source_email.strip()
    if not source_email:
        raise SyncError("源邮箱不能为空")
    keyword = keyword.strip()
    success_page = max(1, int(success_page))
    failed_page = max(1, int(failed_page))
    page_size = min(100, max(1, int(page_size)))
    sync_source_context = _sync_rule_log_source_context(db, source_email)
    (
        target_folders,
        default_target_folder,
        default_target_email,
        default_target_service,
    ) = _sync_rule_log_target_context(
        db,
        source_email,
    )
    feature_context = _sync_rule_log_feature_context(
        db,
        source_email,
        default_target_email,
        default_target_service,
    )
    success_total = count_recent_messages(
        db,
        source_email=source_email,
        keyword=keyword,
    )
    failed_total = count_pending_operations(
        db,
        source_email=source_email,
        keyword=keyword,
    )
    return {
        "source_email": source_email,
        "keyword": keyword,
        "success": _rows_with_sync_metadata(
            recent_messages(
                db,
                limit=page_size,
                source_email=source_email,
                offset=(success_page - 1) * page_size,
                keyword=keyword,
            ),
            sync_source_context,
            target_folders,
            default_target_folder,
            default_target_email,
            default_target_service,
            feature_context,
        ),
        "failed": _rows_with_sync_metadata(
            pending_operations(
                db,
                limit=page_size,
                source_email=source_email,
                offset=(failed_page - 1) * page_size,
                keyword=keyword,
            ),
            sync_source_context,
            target_folders,
            default_target_folder,
            default_target_email,
            default_target_service,
            feature_context,
        ),
        "pagination": {
            "success": {
                "page": success_page,
                "page_size": page_size,
                "total": success_total,
            },
            "failed": {
                "page": failed_page,
                "page_size": page_size,
                "total": failed_total,
            },
        },
    }


def _rows_with_sync_metadata(
    rows: Sequence[sqlite3.Row],
    sync_source_context: dict,
    target_folders: dict[str, str],
    default_target_folder: str,
    default_target_email: str,
    default_target_service: str,
    feature_context: dict,
) -> list[dict]:
    result = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        source_folder = str(item.get("source_folder") or "")
        item["mail_time"] = str(
            item.get("internal_date") or item.get("updated_at") or ""
        )
        item["sync_source"] = _sync_rule_log_source(item, sync_source_context)
        item["target_folder"] = (
            str(item.get("target_folder") or "")
            or target_folders.get(source_folder)
            or default_target_folder
            or "INBOX"
        )
        item["target_email"] = (
            str(item.get("target_email") or "")
            or default_target_email
        )
        item["target_service_address"] = (
            str(item.get("target_service_address") or "")
            or default_target_service
        )
        item["remarks"] = _sync_rule_log_remarks(item, feature_context)
        item["failure_reason"] = _sync_rule_log_failure_reason(
            item,
            feature_context,
        )
        result.append(item)
    return result


def _sync_rule_log_remarks(item: dict, feature_context: dict | None = None) -> list[str]:
    remarks: list[str] = []
    feature_context = feature_context or {}
    action = _sync_rule_log_action_remark(item.get("last_sync_action"))
    if action:
        remarks.append(action)
    if str(item.get("source_folder") or "") == "__out_of_scope__":
        remarks.append("来源已移出同步范围")
    if str(item.get("source_folder") or "") == "__deleted__":
        remarks.append("源邮件已删除")
    if _sync_log_flag_is_false(item.get("source_date_verified")):
        remarks.append("待源时间校验")
    if _sync_log_flag_is_false(item.get("target_date_header_verified")):
        remarks.append("待目标时间校验")
    if item.get("old_target_uid"):
        remarks.append("目标副本待替换")
    if item.get("new_target_uid"):
        remarks.append("目标 UID 已生成")
    source_features = _sync_log_features(feature_context, "source")
    target_features = _sync_log_features(feature_context, "target")
    source_all_time = _sync_log_feature_bool(
        source_features,
        "all_time_range_supported",
        "source_all_time_range_supported",
    )
    target_all_time = _sync_log_feature_bool(
        target_features,
        "all_time_range_supported",
        "target_all_time_range_supported",
    )
    if source_features.get("idle_supported") is False:
        remarks.append("准实时同步降级为轮询")
    if source_all_time is False:
        remarks.append("来源全时间范围受限")
    if target_all_time is False:
        remarks.append("目标全时间范围受限")
    if target_features.get("leading_received_header_added") is True:
        remarks.append("目标补写到达记录")
    elif target_features.get("web_display_time_uses_import_time") is True:
        remarks.append("目标网页时间显示导入时间")
    return _unique_texts(remarks)


def _sync_rule_log_failure_reason(item: dict, feature_context: dict | None) -> str:
    detail = str(item.get("last_error") or "").strip()
    if not detail:
        return ""
    target_features = _sync_log_features(feature_context or {}, "target")
    target_all_time = _sync_log_feature_bool(
        target_features,
        "all_time_range_supported",
        "target_all_time_range_supported",
    )
    reason = _sync_log_business_error(detail)
    notes: list[str] = []
    detail_lower = detail.casefold()
    read_after_write_problem = any(
        fragment in detail_lower
        for fragment in (
            "target body unavailable",
            "target fetch failed",
            "does not exist",
            "读取不到邮件正文",
            "fetch 失败",
        )
    )
    if (
        read_after_write_problem
        and target_all_time is True
    ):
        notes.append("历史邮件读取表现与全时间段范围检测结果不一致")
    if (
        read_after_write_problem
        and target_all_time is False
    ):
        notes.append("目标邮箱全时间段范围受限，历史邮件可能写入后不可读")
    if notes:
        return "；".join(_unique_texts([reason, *notes]))
    return reason


def _sync_log_business_error(detail: str) -> str:
    text = detail.splitlines()[0].strip()
    folded = text.casefold()
    if "target uid" in folded and "failed content verification" in folded:
        if "target body unavailable" in folded:
            return "目标邮箱写入后读取不到邮件内容"
        if "target content does not match source" in folded:
            return "目标邮箱返回的邮件内容与来源不一致"
        if "missing labels" in folded:
            return "目标邮箱标签写入不完整"
        return "目标邮箱写入后校验失败"
    if "append failed" in folded:
        return "目标邮箱写入失败"
    if "expected one appended uid" in folded:
        return "目标邮箱写入后无法唯一定位新邮件"
    if "target fetch failed" in folded:
        return "目标邮箱读取失败"
    if "target body unavailable" in folded:
        return "目标邮箱读取不到邮件内容"
    return text[:180] if len(text) > 180 else text


def _sync_log_features(feature_context: dict, scope: str) -> dict:
    values = feature_context.get(scope)
    return values if isinstance(values, dict) else {}


def _sync_log_feature_bool(features: dict, *keys: str) -> bool | None:
    for key in keys:
        if key in features:
            return bool(features.get(key))
    return None


def _sync_rule_log_action_remark(value: object) -> str:
    return {
        "date_rebuilt": "时间修正",
        "rebuilt": "目标副本重建",
        "folder_label_added": "补充文件夹标签",
        "source_location_added": "补充源位置",
        "source_out_of_scope": "来源已移出同步范围",
        "source_out_of_scope_labeled": "来源已移出同步范围",
        "source_out_of_scope_deleted": "目标已同步删除",
        "source_message_unavailable_seen": "源邮件本轮可见但内容不可读",
        "pending_operation_recovered": "失败恢复",
        "source_labels_rebuilt": "源标签修复",
    }.get(str(value or ""), "")


def _sync_log_flag_is_false(value: object) -> bool:
    return value is not None and str(value).strip().lower() in {"0", "false"}


def _unique_texts(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _sync_rule_log_source_context(
    db: sqlite3.Connection,
    source_email: str,
) -> dict:
    row = db.execute(
        """
        SELECT s.extra_json, a.pop_fallback_json
        FROM source_rules s
        LEFT JOIN mail_accounts a
          ON a.rowid=(
              SELECT rowid
              FROM mail_accounts
              WHERE email=s.email
              ORDER BY enabled DESC, sort_order, service_address
              LIMIT 1
          )
        WHERE s.email=?
        """,
        (source_email,),
    ).fetchone()
    if row is None:
        return {"sync_mode": "realtime", "pop_fallback_enabled": False}
    extra = _json_object(row["extra_json"])
    pop_fallback = _json_object(row["pop_fallback_json"])
    pop_fallback_enabled = bool(pop_fallback) and _json_truthy(
        extra.get("pop_fallback_enabled"),
        default=bool(pop_fallback),
    )
    return {
        "sync_mode": str(extra.get("sync_mode") or "realtime"),
        "pop_fallback_enabled": pop_fallback_enabled,
    }


def _sync_rule_log_source(item: dict, context: dict) -> str:
    source = str(item.get("last_sync_source") or "").strip()
    if source:
        return {
            "imap_idle": "IMAP 推送",
            "imap_idle_compensation": "IMAP 补偿",
            "imap_poll": "IMAP 轮询",
            "imap_scan": "IMAP 扫描",
            "imap_scheduled": "IMAP 定时",
            "pop_fallback": "POP 补偿",
        }.get(source, "IMAP 同步")
    if context.get("pop_fallback_enabled"):
        return "POP 补偿"
    if str(context.get("sync_mode") or "realtime") == "scheduled":
        return "IMAP 定时"
    if str(item.get("source_folder") or "").casefold() != "inbox":
        return "IMAP 扫描"
    return "IMAP 同步"


def _sync_rule_log_target_context(
    db: sqlite3.Connection,
    source_email: str,
) -> tuple[dict[str, str], str, str, str]:
    rule = db.execute(
        """
        SELECT extra_json
        FROM source_rules
        WHERE email=?
        """,
        (source_email,),
    ).fetchone()
    extra = _json_object(rule["extra_json"]) if rule is not None else {}
    target = selected_target_account(
        db,
        str(extra.get("target_email") or ""),
        str(extra.get("target_service_address") or ""),
    )
    default_target_folder = (
        str(target["target_folder"] or "INBOX") if target is not None else "INBOX"
    )
    default_target_email = str(target["email"] or "") if target is not None else ""
    default_target_service = (
        str(target["service_address"] or "") if target is not None else ""
    )
    rows = db.execute(
        """
        SELECT folder_name, target_folder
        FROM folder_rules
        WHERE source_email=?
        """,
        (source_email,),
    ).fetchall()
    return (
        {
            str(row["folder_name"] or ""): str(
                row["target_folder"] or default_target_folder,
            )
            for row in rows
        },
        default_target_folder,
        default_target_email,
        default_target_service,
    )


def _sync_rule_log_feature_context(
    db: sqlite3.Connection,
    source_email: str,
    target_email: str,
    target_service_address: str,
) -> dict[str, dict]:
    source_rule = db.execute(
        """
        SELECT email, extra_json
        FROM source_rules
        WHERE email=?
        """,
        (source_email,),
    ).fetchone()
    source_service = (
        _rule_source_service(db, source_rule)
        if source_rule is not None
        else ""
    )
    source_groups = _mailbox_feature_groups_for_identity(
        db,
        source_email,
        source_service,
    )
    target_groups = _mailbox_feature_groups_for_identity(
        db,
        target_email,
        target_service_address,
    )
    return {
        "source": {
            **_json_dict(source_groups.get("common")),
            **_json_dict(source_groups.get("source")),
        },
        "target": {
            **_json_dict(target_groups.get("common")),
            **_json_dict(target_groups.get("target")),
        },
    }


def _mailbox_feature_groups_for_identity(
    db: sqlite3.Connection,
    email: str,
    service_address: str = "",
) -> dict:
    email = str(email or "").strip()
    service_address = str(service_address or "").strip()
    if not email:
        return {"common": {}, "source": {}, "target": {}}
    service_filter = "AND service_address=?" if service_address else ""
    params: tuple[object, ...] = (
        (email, service_address) if service_address else (email,)
    )
    row = db.execute(
        f"""
        SELECT extra_json
        FROM mail_accounts
        WHERE email=?
          {service_filter}
        ORDER BY enabled DESC, sort_order, service_address
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return {"common": {}, "source": {}, "target": {}}
    return _mailbox_feature_groups(_json_object(row["extra_json"]))


def recent_messages(
    db: sqlite3.Connection,
    limit: int = 8,
    source_email: str | None = None,
    offset: int = 0,
    keyword: str = "",
) -> List[sqlite3.Row]:
    where, params = _sync_log_where_clause(
        source_email,
        keyword,
        (
            "subject",
            "source_email",
            "source_folder",
            "source_uid",
            "internal_date",
            "target_uid",
            "account_label",
            "folder_label",
            "folder_labels_json",
            "target_folder",
            "target_email",
            "target_service_address",
            "last_sync_action",
            "last_sync_source",
        ),
    )
    params.extend((limit, offset))
    return db.execute(
        f"""
        SELECT source_email, content_hash, subject, source_folder, source_uid,
               target_uid, account_label, folder_label, folder_labels_json,
               target_folder, target_email, target_service_address, internal_date,
               source_date_verified, target_date_header_verified,
               last_sync_action, last_sync_source, updated_at
        FROM messages
        {where}
        ORDER BY julianday(COALESCE(NULLIF(internal_date, ''), updated_at)) DESC,
                 COALESCE(NULLIF(internal_date, ''), updated_at) DESC,
                 updated_at DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()


def count_recent_messages(
    db: sqlite3.Connection,
    source_email: str | None = None,
    keyword: str = "",
) -> int:
    where, params = _sync_log_where_clause(
        source_email,
        keyword,
        (
            "subject",
            "source_email",
            "source_folder",
            "source_uid",
            "internal_date",
            "target_uid",
            "account_label",
            "folder_label",
            "folder_labels_json",
            "target_folder",
            "target_email",
            "target_service_address",
            "last_sync_action",
            "last_sync_source",
        ),
    )
    return int(
        db.execute(
            f"SELECT COUNT(*) FROM messages {where}",
            params,
        ).fetchone()[0]
    )


def pending_operations(
    db: sqlite3.Connection,
    limit: int = 8,
    source_email: str | None = None,
    offset: int = 0,
    keyword: str = "",
) -> List[sqlite3.Row]:
    where, params = _sync_log_where_clause(
        source_email,
        keyword,
        (
            "subject",
            "source_email",
            "source_folder",
            "source_uid",
            "internal_date",
            "status",
            "last_error",
            "account_label",
            "folder_label",
            "folder_labels_json",
            "old_target_uid",
            "target_uid_before",
            "new_target_uid",
            "target_folder",
            "target_email",
            "target_service_address",
            "last_sync_source",
        ),
    )
    params.extend((limit, offset))
    return db.execute(
        f"""
        SELECT source_email, content_hash, subject, source_folder, source_uid,
               internal_date, status, last_error, account_label, folder_label,
               folder_labels_json, old_target_uid, new_target_uid,
               target_folder, target_email, target_service_address,
               last_sync_source, updated_at
        FROM pending_operations
        {where}
        ORDER BY julianday(COALESCE(NULLIF(internal_date, ''), updated_at)) DESC,
                 COALESCE(NULLIF(internal_date, ''), updated_at) DESC,
                 updated_at DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()


def count_pending_operations(
    db: sqlite3.Connection,
    source_email: str | None = None,
    keyword: str = "",
) -> int:
    where, params = _sync_log_where_clause(
        source_email,
        keyword,
        (
            "subject",
            "source_email",
            "source_folder",
            "source_uid",
            "internal_date",
            "status",
            "last_error",
            "account_label",
            "folder_label",
            "folder_labels_json",
            "old_target_uid",
            "target_uid_before",
            "new_target_uid",
            "target_folder",
            "target_email",
            "target_service_address",
            "last_sync_source",
        ),
    )
    return int(
        db.execute(
            f"SELECT COUNT(*) FROM pending_operations {where}",
            params,
        ).fetchone()[0]
    )


def _sync_log_where_clause(
    source_email: str | None,
    keyword: str,
    searchable_columns: Sequence[str],
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if source_email:
        clauses.append("source_email=?")
        params.append(source_email)
    keyword = keyword.strip()
    if keyword:
        pattern = _like_pattern(keyword)
        clauses.append(
            "("
            + " OR ".join(
                f"COALESCE(CAST({column} AS TEXT), '') LIKE ? ESCAPE '\\'"
                for column in searchable_columns
            )
            + ")"
        )
        params.extend([pattern] * len(searchable_columns))
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", params)


def _like_pattern(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _count(db: sqlite3.Connection, table: str) -> int:
    return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _clean_label_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise SyncError("Label ID is required")
    if not LABEL_ID_RE.match(value):
        raise SyncError("Label ID cannot contain spaces, quotes, or parentheses")
    return value


def _clean_label_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise SyncError("Display name is required")
    return value


def _require_target(
    db: sqlite3.Connection,
    target_email: str,
    target_service_address: str = "",
) -> str:
    if not target_email:
        raise SyncError("Target mailbox is required")
    target_service_address = target_service_address.strip()
    service_filter = "AND service_address=?" if target_service_address else ""
    params: tuple[object, ...] = (
        (target_email, target_service_address)
        if target_service_address
        else (target_email,)
    )
    row = db.execute(
        f"""
        SELECT service_address
        FROM mail_accounts
        WHERE email=?
          {service_filter}
        ORDER BY enabled DESC, sort_order, service_address
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        raise SyncError("Target mailbox is not configured")
    return row["service_address"] or ""


def _require_source_account(
    db: sqlite3.Connection,
    source_email: str,
    source_service_address: str = "",
) -> str:
    if not source_email:
        raise SyncError("Source mailbox is required")
    source_service_address = source_service_address.strip()
    service_filter = "AND service_address=?" if source_service_address else ""
    params: tuple[object, ...] = (
        (source_email, source_service_address)
        if source_service_address
        else (source_email,)
    )
    row = db.execute(
        f"""
        SELECT service_address
        FROM mail_accounts
        WHERE email=?
          {service_filter}
        ORDER BY enabled DESC, sort_order, service_address
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        raise SyncError("Source mailbox is not configured")
    return row["service_address"] or ""


def _label_usage_count(db: sqlite3.Connection, label_name: str) -> int:
    source_count = int(
        db.execute(
            """
            SELECT COUNT(*)
            FROM source_rules
            WHERE account_label=?
               OR default_folder_label=?
               OR deleted_label=?
            """,
            (label_name, label_name, label_name),
        ).fetchone()[0]
    )
    folder_count = 0
    rows = db.execute(
        """
        SELECT folder_label, folder_labels_json
        FROM folder_rules
        WHERE folder_label=?
           OR (folder_labels_json IS NOT NULL AND folder_labels_json <> '')
        """,
        (label_name,),
    ).fetchall()
    for row in rows:
        labels = _folder_label_list(row["folder_label"])
        labels.extend(_folder_label_list(_json_array(row["folder_labels_json"])))
        if label_name in _unique_texts(labels):
            folder_count += 1
    extra_count = 0
    rows = db.execute(
        """
        SELECT extra_json
        FROM source_rules
        WHERE extra_json IS NOT NULL AND extra_json <> ''
        """
    ).fetchall()
    for row in rows:
        extra = _json_object(row["extra_json"])
        if _out_of_scope_policy(extra) == "label" and label_name in _out_of_scope_labels(extra):
            extra_count += 1
    return source_count + folder_count + extra_count


def _target_label_exists(
    db: sqlite3.Connection,
    target_email: str,
    target_service_address: str,
    label_name: str,
) -> bool:
    return (
        db.execute(
            """
            SELECT 1
            FROM mail_label
            WHERE target_email=? AND target_service_address=? AND label_name=?
            LIMIT 1
            """,
            (target_email, target_service_address, label_name),
        ).fetchone()
        is not None
    )


def _rename_label_references(
    db: sqlite3.Connection,
    old_name: str,
    new_name: str,
) -> None:
    for column in ("account_label", "default_folder_label", "deleted_label"):
        db.execute(
            f"""
            UPDATE source_rules
            SET {column}=?
            WHERE {column}=?
            """,
            (new_name, old_name),
        )
    db.execute(
        """
        UPDATE folder_rules
        SET folder_label=?
        WHERE folder_label=?
        """,
        (new_name, old_name),
    )
    rows = db.execute(
        """
        SELECT source_email, folder_name, folder_labels_json
        FROM folder_rules
        WHERE folder_labels_json IS NOT NULL AND folder_labels_json <> ''
        """
    ).fetchall()
    for row in rows:
        labels = _folder_label_list(_json_array(row["folder_labels_json"]))
        if old_name not in labels:
            continue
        labels = [new_name if label == old_name else label for label in labels]
        db.execute(
            """
            UPDATE folder_rules
            SET folder_labels_json=?
            WHERE source_email=? AND folder_name=?
            """,
            (
                _json_dump(labels),
                row["source_email"],
                row["folder_name"],
            ),
        )
    rows = db.execute(
        """
        SELECT email, extra_json
        FROM source_rules
        WHERE extra_json IS NOT NULL AND extra_json <> ''
        """
    ).fetchall()
    for row in rows:
        extra = _json_object(row["extra_json"])
        labels = _out_of_scope_labels(extra)
        if old_name not in labels:
            continue
        labels = [new_name if label == old_name else label for label in labels]
        extra["out_of_scope_label"] = labels[0] if labels else ""
        extra["out_of_scope_labels"] = labels
        db.execute(
            """
            UPDATE source_rules
            SET extra_json=?
            WHERE email=?
            """,
            (_json_dump(extra), row["email"]),
        )


def _mailbox_summary(row: sqlite3.Row) -> dict:
    auth = _json_object(row["auth_json"])
    pop = _json_object(row["pop_fallback_json"])
    smtp = _json_object(row["smtp_json"]) if "smtp_json" in row.keys() else {}
    extra = _json_object(row["extra_json"]) if "extra_json" in row.keys() else {}
    probe_results = _mailbox_probe_results(extra)
    mailbox_features = _mailbox_features(extra)
    mailbox_feature_groups = _mailbox_feature_groups(extra)
    protocols = _mailbox_protocols(row)
    auth_provider = "manual"
    if auth.get("type") == "oauth2" and auth.get("provider") == "microsoft":
        auth_provider = "outlook"
    return {
        "email": row["email"],
        "service_address": row["service_address"] or row["imap_server"] or "",
        "enabled": bool(row["enabled"]),
        "imap_server": row["imap_server"] or "",
        "imap_port": row["imap_port"] or "",
        "use_ssl": bool(row["use_ssl"]),
        "proxy_name": row["proxy_name"] or "Direct",
        "has_password": bool(row["has_password"]),
        "has_auth": bool(auth),
        "auth_provider": auth_provider,
        "oauth_token_saved": bool(auth.get("token") or auth.get("token_file")),
        "protocols": protocols,
        "protocol_summary": _protocol_summary(protocols),
        "imap_enabled": "imap" in protocols,
        "pop_enabled": "pop" in protocols or bool(pop),
        "smtp_enabled": "smtp" in protocols or bool(smtp),
        "label_count": row["label_count"] if "label_count" in row.keys() else 0,
        "imap_timeout": row["imap_timeout"] or "",
        "target_folder": row["target_folder"] or "",
        "sort_order": row["sort_order"],
        "probe_results": probe_results,
        "mailbox_features": mailbox_features,
        "mailbox_feature_groups": mailbox_feature_groups,
        "probe_tested_at": probe_results.get("tested_at", ""),
        "realtime_poll_interval": _mailbox_realtime_poll_interval(extra),
    }


def _mailbox_detail(row: sqlite3.Row) -> dict:
    auth = _json_object(row["auth_json"])
    pop = _json_object(row["pop_fallback_json"])
    smtp = _json_object(row["smtp_json"]) if "smtp_json" in row.keys() else {}
    summary = _mailbox_summary(row)
    summary.update(
        {
            "proxy_profile_id": row["proxy_profile_id"] or "",
            "oauth_client_id": auth.get("client_id", ""),
            "oauth_token_saved": bool(auth.get("token") or auth.get("token_file")),
            "pop_server": pop.get("server") or pop.get("host") or "",
            "pop_port": pop.get("port") or "",
            "pop_use_ssl": bool(pop.get("use_ssl", True)),
            "pop_timeout": pop.get("timeout") or "",
            "smtp_server": smtp.get("server") or smtp.get("host") or "",
            "smtp_port": smtp.get("port") or "",
            "smtp_use_ssl": bool(smtp.get("use_ssl", True)),
            "smtp_timeout": smtp.get("timeout") or "",
        }
    )
    return summary


def _mailbox_protocols(row: sqlite3.Row) -> list[str]:
    protocols = []
    if "protocols_json" in row.keys():
        protocols = [
            str(value).lower()
            for value in _json_array(row["protocols_json"])
            if str(value).lower() in {"imap", "pop", "smtp"}
        ]
    if not protocols:
        if row["imap_server"]:
            protocols.append("imap")
        if _json_object(row["pop_fallback_json"]):
            protocols.append("pop")
        if "smtp_json" in row.keys() and _json_object(row["smtp_json"]):
            protocols.append("smtp")
    return protocols or ["imap"]


def _protocol_summary(protocols: Sequence[str]) -> str:
    labels = {"imap": "IMAP", "pop": "POP", "smtp": "SMTP"}
    return " / ".join(labels.get(protocol, protocol.upper()) for protocol in protocols)


def _auth_json(
    provider: str,
    client_id: str,
    token_file: str,
    existing_raw: str | None,
    token: dict | None = None,
) -> str | None:
    if provider == "manual":
        return None
    existing = _json_object(existing_raw)
    client_id = client_id.strip() or str(existing.get("client_id") or "")
    token_file = token_file.strip() or str(existing.get("token_file") or "")
    saved_token = token if isinstance(token, dict) else _json_object(existing.get("token"))
    if not client_id:
        raise SyncError("Outlook client ID is required")
    if not saved_token and not token_file:
        raise SyncError("Outlook OAuth token is required")
    payload = {
        "type": "oauth2",
        "provider": "microsoft",
        "client_id": client_id,
    }
    for key in ("tenant", "client_secret", "scopes"):
        if existing.get(key):
            payload[key] = existing[key]
    if saved_token:
        payload["token"] = saved_token
    else:
        payload["token_file"] = token_file
    return _json_dump(payload)


def _next_mailbox_sort_order(db: sqlite3.Connection) -> int:
    value = db.execute(
        """
        SELECT COALESCE(MAX(sort_order), 0) + 1
        FROM mail_accounts
        """
    ).fetchone()[0]
    return int(value)


def _rename_rule_target_references(
    db: sqlite3.Connection,
    old_email: str,
    new_email: str,
    old_service_address: str = "",
    new_service_address: str = "",
) -> None:
    rows = db.execute(
        """
        SELECT email, extra_json
        FROM source_rules
        WHERE extra_json IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        extra = _json_object(row["extra_json"])
        if extra.get("target_email") != old_email:
            continue
        if old_service_address and extra.get("target_service_address") not in {
            "",
            None,
            old_service_address,
        }:
            continue
        extra["target_email"] = new_email
        if new_service_address:
            extra["target_service_address"] = new_service_address
        db.execute(
            """
            UPDATE source_rules
            SET extra_json=?
            WHERE email=?
            """,
            (_json_dump(extra), row["email"]),
        )


def _rename_rule_source_reference(
    db: sqlite3.Connection,
    email: str,
    *,
    old_service_address: str = "",
    new_service_address: str = "",
) -> None:
    if not new_service_address:
        return
    rows = db.execute(
        """
        SELECT email, extra_json
        FROM source_rules
        WHERE email=?
        """,
        (email,),
    ).fetchall()
    for row in rows:
        extra = _json_object(row["extra_json"])
        if old_service_address and extra.get("source_service_address") not in {
            "",
            None,
            old_service_address,
        }:
            continue
        extra["source_service_address"] = new_service_address
        db.execute(
            """
            UPDATE source_rules
            SET extra_json=?
            WHERE email=?
            """,
            (_json_dump(extra), row["email"]),
        )


def _require_label_owner(
    db: sqlite3.Connection,
    email: str,
    service_address: str | None = None,
) -> str:
    service_address = (service_address or "").strip()
    if not email:
        raise SyncError("Mailbox is required")
    service_filter = "AND service_address=?" if service_address else ""
    params: tuple[object, ...] = (
        (email, service_address) if service_address else (email,)
    )
    row = db.execute(
        f"""
        SELECT service_address
        FROM mail_accounts
        WHERE email=?
          {service_filter}
        ORDER BY enabled DESC, sort_order, service_address
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        raise SyncError("Mailbox is not configured")
    return row["service_address"]


def _mailbox_service_address(db: sqlite3.Connection, email: str | None) -> str:
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


def _mailbox_rule_binding(
    db: sqlite3.Connection,
    email: str,
    service_address: str,
) -> str | None:
    identity = _mailbox_identity(email, service_address)
    rows = db.execute(
        """
        SELECT email, extra_json
        FROM source_rules
        """
    ).fetchall()
    for row in rows:
        extra = _json_object(row["extra_json"])
        if _mailbox_identity(row["email"], _rule_source_service(db, row)) == identity:
            return "source"
        target_email, target_service = _rule_target_identity(db, extra)
        if _mailbox_identity(target_email, target_service) == identity:
            return "target"
    return None


def _rule_source_service(
    db: sqlite3.Connection,
    rule: sqlite3.Row,
) -> str:
    extra = _json_object(rule["extra_json"])
    source_service = str(extra.get("source_service_address") or "").strip()
    if source_service:
        return source_service
    source_row = _account_row_for_email(db, str(rule["email"] or ""))
    if source_row is None:
        return ""
    return str(source_row["service_address"] or "").strip()


def _mailbox_identity(email: str, service_address: str) -> tuple[str, str]:
    return (
        email.strip().casefold(),
        service_address.strip().casefold(),
    )


def _sync_rule_has_logs(db: sqlite3.Connection, source_email: str) -> bool:
    source_email = source_email.strip()
    if not source_email:
        return False
    for table in ("messages", "pending_operations"):
        row = db.execute(
            f"SELECT 1 FROM {table} WHERE source_email=? LIMIT 1",
            (source_email,),
        ).fetchone()
        if row is not None:
            return True
    return False


def _rule_target_identity(
    db: sqlite3.Connection,
    extra: dict[str, object],
) -> tuple[str, str]:
    target_email = str(extra.get("target_email") or "").strip()
    target_service = str(extra.get("target_service_address") or "").strip()
    if target_email:
        if not target_service:
            target_service = _mailbox_service_address(db, target_email)
        return target_email, target_service
    target = selected_target_account(db)
    if target is None:
        return "", ""
    return (
        str(target["email"] or "").strip(),
        str(target["service_address"] or "").strip(),
    )


def _sync_rule_pair_duplicate(
    db: sqlite3.Connection,
    *,
    original_email: str,
    source_email: str,
    source_service_address: str,
    target_email: str,
    target_service_address: str,
) -> sqlite3.Row | None:
    source_identity = _mailbox_identity(source_email, source_service_address)
    target_identity = _mailbox_identity(target_email, target_service_address)
    skipped_email = original_email.strip().casefold()
    rows = db.execute(
        """
        SELECT email, extra_json
        FROM source_rules
        ORDER BY email
        """
    ).fetchall()
    for row in rows:
        row_email = str(row["email"] or "")
        if skipped_email and row_email.strip().casefold() == skipped_email:
            continue
        if _mailbox_identity(
            row_email,
            _rule_source_service(db, row),
        ) != source_identity:
            continue
        target = _rule_target_identity(db, _json_object(row["extra_json"]))
        if _mailbox_identity(*target) == target_identity:
            return row
    return None


def _mailbox_identity_changed(
    old_email: str,
    old_service_address: str,
    new_email: str,
    new_service_address: str,
) -> bool:
    return _mailbox_identity(old_email, old_service_address) != _mailbox_identity(
        new_email,
        new_service_address,
    )


def _source_rule_summary(row: sqlite3.Row) -> dict:
    extra = _json_object(row["extra_json"])
    return {
        "email": row["email"],
        "enabled": bool(row["enabled"]),
        "imap_server": row["imap_server"] or "",
        "account_label": row["account_label"],
        "default_folder_label": row["default_folder_label"],
        "deleted_label": row["deleted_label"],
        "sync_interval": row["sync_interval"],
        "batch_size": row["batch_size"],
        "skip_deleted_drafts": bool(row["skip_deleted_drafts"]),
        "folder_rule_count": row["folder_rule_count"],
        "source_service_address": str(extra.get("source_service_address") or ""),
        "target_email": str(extra.get("target_email") or ""),
        "target_service_address": str(extra.get("target_service_address") or ""),
        "sync_mode": str(extra.get("sync_mode") or "realtime"),
        "date_policy": str(extra.get("date_policy") or "auto"),
        "out_of_scope_policy": _out_of_scope_policy(extra),
        "out_of_scope_label": (_out_of_scope_labels(extra) or [""])[0],
        "out_of_scope_labels": _out_of_scope_labels(extra),
        "sync_status": row["last_sync_status"] or "ok",
        "sync_error_summary": row["last_sync_error_summary"] or "",
        "sync_error_detail": row["last_sync_error_detail"] or "",
    }


def _source_rule_health(db: sqlite3.Connection, source_email: str) -> dict:
    row = db.execute(
        """
        SELECT last_sync_status, last_sync_error_summary,
               last_sync_error_detail
        FROM source_rules
        WHERE email=?
        """,
        (source_email,),
    ).fetchone()
    if row is None:
        return {
            "sync_status": "ok",
            "sync_error_summary": "",
            "sync_error_detail": "",
        }
    return {
        "sync_status": row["last_sync_status"] or "ok",
        "sync_error_summary": row["last_sync_error_summary"] or "",
        "sync_error_detail": row["last_sync_error_detail"] or "",
    }


def _available_rule_sources(
    source_accounts: Sequence[sqlite3.Row],
    rules: Sequence[dict],
) -> list[sqlite3.Row]:
    used = {str(row["email"]) for row in rules}
    return [row for row in source_accounts if row["email"] not in used]


def _row_or_default(
    row: sqlite3.Row | None,
    key: str,
    default: object,
) -> object:
    if row is None:
        return default
    value = row[key]
    return default if value is None else value


def _out_of_scope_policy(values: dict) -> str:
    policy = str(values.get("out_of_scope_policy") or "keep").strip()
    return policy if policy in OUT_OF_SCOPE_POLICIES else "keep"


def _out_of_scope_labels(values: dict) -> list[str]:
    labels = _unique_texts(_folder_label_list(values.get("out_of_scope_labels")))
    if labels:
        return labels
    return _unique_texts(_folder_label_list(values.get("out_of_scope_label")))


def _folder_rule_form_row(
    row: sqlite3.Row,
    folder_display_metadata: dict[str, dict] | None = None,
) -> dict:
    skip_uids = ""
    if row["skip_uids_json"]:
        skip_uids = ", ".join(str(value) for value in _json_array(row["skip_uids_json"]))
    labels = _folder_label_list(_json_array(row["folder_labels_json"])) if row["folder_labels_json"] else _folder_label_list(row["folder_label"])
    metadata = (folder_display_metadata or {}).get(str(row["folder_name"] or ""), {})
    return {
        "folder_name": row["folder_name"],
        "display_name": str(metadata.get("display_name") or ""),
        "attributes": _folder_attribute_list(metadata.get("attributes")),
        "target_folder": row["target_folder"] or "",
        "folder_label": labels[0] if labels else "",
        "folder_labels": labels,
        "excluded": bool(row["excluded"]),
        "skip_uids": skip_uids,
    }


def _merged_folder_display_metadata(
    extra: dict,
    folder_rules: Sequence[dict],
    folder_names: Sequence[str],
) -> dict[str, dict]:
    allowed = {str(name or "") for name in folder_names if str(name or "")}
    metadata = {
        name: value
        for name, value in _folder_display_metadata_from_extra(extra).items()
        if name in allowed
    }
    metadata.update(_folder_display_metadata_from_rules(folder_rules, allowed))
    return metadata


def _folder_display_metadata_from_extra(extra: dict) -> dict[str, dict]:
    raw = extra.get("folder_display_metadata")
    if not isinstance(raw, dict):
        return {}
    return _normalize_folder_display_metadata(raw)


def _folder_display_metadata_from_rules(
    folder_rules: Sequence[dict],
    allowed: set[str],
) -> dict[str, dict]:
    raw: dict[str, dict] = {}
    for row in folder_rules:
        folder_name = str(row.get("folder_name") or "").strip()
        if not folder_name or folder_name not in allowed:
            continue
        display_name = str(row.get("display_name") or "").strip()
        attributes = _folder_attribute_list(row.get("attributes"))
        if display_name or attributes:
            raw[folder_name] = {
                "display_name": display_name,
                "attributes": attributes,
            }
    return _normalize_folder_display_metadata(raw)


def _normalize_folder_display_metadata(raw: dict) -> dict[str, dict]:
    metadata: dict[str, dict] = {}
    for folder_name, value in raw.items():
        name = str(folder_name or "").strip()
        if not name or not isinstance(value, dict):
            continue
        display_name = str(value.get("display_name") or "").strip()
        attributes = _folder_attribute_list(value.get("attributes"))
        if display_name or attributes:
            metadata[name] = {
                "display_name": display_name,
                "attributes": attributes,
            }
    return metadata


def _folder_attribute_list(value: object) -> list[str]:
    if isinstance(value, str):
        values: Sequence[object] = [value]
    elif isinstance(value, Sequence):
        values = value
    else:
        values = []
    return _unique_texts([str(item).strip() for item in values])


def _clean_required(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise SyncError(f"{label} is required")
    return value


def _source_account_exists(db: sqlite3.Connection, source_email: str) -> bool:
    return (
        db.execute(
            """
            SELECT 1
            FROM mail_accounts
            WHERE email=?
            """,
            (source_email,),
        ).fetchone()
        is not None
    )


def _source_pop_fallback_json(
    db: sqlite3.Connection,
    source_email: str,
    source_service_address: str = "",
) -> str | None:
    source_service_address = source_service_address.strip()
    service_filter = "AND service_address=?" if source_service_address else ""
    params: tuple[object, ...] = (
        (source_email, source_service_address)
        if source_service_address
        else (source_email,)
    )
    row = db.execute(
        f"""
        SELECT pop_fallback_json
        FROM mail_accounts
        WHERE email=?
          {service_filter}
        ORDER BY enabled DESC, sort_order, service_address
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return None
    value = row["pop_fallback_json"]
    return str(value) if value else None


def _source_rule_exists(db: sqlite3.Connection, source_email: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM source_rules WHERE email=?",
            (source_email,),
        ).fetchone()
        is not None
    )


def _pop_fallback_json(
    *,
    enabled: bool,
    server: str,
    port: str,
    use_ssl: bool,
    timeout: str,
) -> str | None:
    if not enabled:
        return None
    server = server.strip()
    if not server:
        return None
    parsed_port = _optional_int(port, "POP port", minimum=1, maximum=65535)
    parsed_timeout = _optional_int(timeout, "POP timeout", minimum=1)
    return _json_dump(
        {
            "server": server,
            "port": parsed_port if parsed_port is not None else 995,
            "use_ssl": bool(use_ssl),
            "timeout": parsed_timeout if parsed_timeout is not None else 300,
        }
    )


def _smtp_json(
    *,
    enabled: bool,
    server: str,
    port: str,
    use_ssl: bool,
    timeout: str,
) -> str | None:
    if not enabled:
        return None
    server = server.strip()
    if not server:
        raise SyncError("SMTP server is required")
    parsed_port = _optional_int(port, "SMTP port", minimum=1, maximum=65535)
    parsed_timeout = _optional_int(timeout, "SMTP timeout", minimum=1)
    return _json_dump(
        {
            "server": server,
            "port": parsed_port if parsed_port is not None else 465,
            "use_ssl": bool(use_ssl),
            "timeout": parsed_timeout if parsed_timeout is not None else 300,
        }
    )


def _protocols_json(
    *,
    imap_enabled: bool,
    pop_enabled: bool,
    smtp_enabled: bool,
) -> str:
    protocols = []
    if imap_enabled:
        protocols.append("imap")
    if pop_enabled:
        protocols.append("pop")
    if smtp_enabled:
        protocols.append("smtp")
    if not protocols:
        raise SyncError("Enable at least one mailbox protocol")
    return _json_dump(protocols) or '["imap"]'


def _normalize_folder_rules(
    folder_rules: Sequence[dict],
) -> list[dict]:
    normalized = []
    seen: set[str] = set()
    for raw in folder_rules:
        folder_name = str(raw.get("folder_name") or "").strip()
        target_folder = str(raw.get("target_folder") or "").strip()
        folder_labels = _folder_label_list(
            raw.get("folder_labels", raw.get("folder_label"))
        )
        folder_label = folder_labels[0] if folder_labels else ""
        if raw.get("delete"):
            continue
        if not folder_name and not folder_label and not raw.get("skip_uids"):
            continue
        if not folder_name:
            raise SyncError("Source folder name is required for each mapping")
        key = folder_name.casefold()
        if key in seen:
            raise SyncError(f"Duplicate source folder mapping: {folder_name}")
        seen.add(key)
        skip_uids = _parse_skip_uids(str(raw.get("skip_uids") or ""), folder_name)
        normalized.append(
            {
                "folder_name": folder_name,
                "target_folder": target_folder or None,
                "folder_label": folder_label or None,
                "folder_labels_json": (
                    _json_dump(folder_labels) if folder_labels else None
                ),
                "excluded": bool(raw.get("excluded")),
                "skip_uids_json": _json_dump(skip_uids) if skip_uids else None,
            }
        )
    return normalized


def _parse_skip_uids(value: str, folder_name: str) -> list[int]:
    value = value.strip()
    if not value:
        return []
    tokens = [
        token
        for token in re.split(r"[\s,]+", value)
        if token
    ]
    try:
        parsed = [int(token) for token in tokens]
    except ValueError as exc:
        raise SyncError(f"Skip UIDs for {folder_name} must be numbers") from exc
    if any(uid < 1 for uid in parsed):
        raise SyncError(f"Skip UIDs for {folder_name} must be positive numbers")
    return parsed


def _account_profile_from_row(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    profile = _json_object(row["extra_json"])
    profile.update(
        {
            "email": row["email"],
            "service_address": row["service_address"],
            "imap_server": row["imap_server"],
            "use_ssl": bool(row["use_ssl"]),
        }
    )
    protocols = _mailbox_protocols(row)
    if protocols:
        profile["protocols"] = protocols
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
        value = _json_object(row[column])
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


def _optional_int(
    value: str,
    label: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise SyncError(f"{label} must be a number") from exc
    if parsed < minimum:
        raise SyncError(f"{label} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise SyncError(f"{label} must be at most {maximum}")
    return parsed


def _proxy_exists(db: sqlite3.Connection, proxy_profile_id: int) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM proxy_profiles WHERE id=?",
            (proxy_profile_id,),
        ).fetchone()
        is not None
    )


def _proxy_from_account(db: sqlite3.Connection, row: sqlite3.Row) -> dict | None:
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
        result.update(_json_object(proxy["extra_json"]))
        return result
    return _json_object(row["imap_proxy_json"]) or None


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


def _json_object(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    value = json.loads(str(raw))
    if not isinstance(value, dict):
        raise SyncError("Stored JSON object configuration is invalid")
    return value


def _parse_probe_results_json(raw: str) -> dict:
    raw = str(raw or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncError("邮箱检测结果格式无效") from exc
    if not isinstance(value, dict):
        raise SyncError("邮箱检测结果格式无效")
    return value


def _merge_mailbox_probe_results(extra: dict, probe_results: dict) -> None:
    normalized = _mailbox_probe_results({"probe_results": probe_results})
    if not normalized:
        return
    feature_groups = _mailbox_feature_groups(
        {
            "mailbox_feature_groups": normalized.get("feature_groups"),
            "mailbox_features": normalized.get("features"),
        }
    )
    features = _flatten_mailbox_feature_groups(feature_groups)
    extra["probe_results"] = normalized
    extra["mailbox_feature_groups"] = feature_groups
    extra["mailbox_features"] = features


def _mailbox_probe_results(extra: dict) -> dict:
    value = extra.get("probe_results")
    if not isinstance(value, dict):
        return {}
    items = value.get("items")
    features = value.get("features")
    feature_groups = value.get("feature_groups")
    normalized_features = features if isinstance(features, dict) else {}
    normalized_groups = _mailbox_feature_groups(
        {
            "mailbox_feature_groups": feature_groups,
            "mailbox_features": normalized_features,
        }
    )
    return {
        "tested_at": str(value.get("tested_at") or ""),
        "protocol": str(value.get("protocol") or ""),
        "items": items if isinstance(items, list) else [],
        "features": normalized_features,
        "feature_groups": normalized_groups,
    }


def _mailbox_features(extra: dict) -> dict:
    value = extra.get("mailbox_features")
    if isinstance(value, dict):
        return dict(value)
    groups = extra.get("mailbox_feature_groups")
    if isinstance(groups, dict):
        return _flatten_mailbox_feature_groups(_mailbox_feature_groups(extra))
    probe_results = extra.get("probe_results")
    if isinstance(probe_results, dict) and isinstance(probe_results.get("features"), dict):
        return dict(probe_results["features"])
    return {}


def _mailbox_feature_groups(extra: dict) -> dict:
    groups = extra.get("mailbox_feature_groups")
    if isinstance(groups, dict):
        return {
            "common": _json_dict(groups.get("common")),
            "source": _json_dict(groups.get("source")),
            "target": _json_dict(groups.get("target")),
        }
    return _group_mailbox_features(_mailbox_features_without_groups(extra))


def _mailbox_realtime_poll_interval(extra: dict) -> int | str:
    value = extra.get("realtime_poll_interval")
    if value in (None, ""):
        return ""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return ""
    return parsed if parsed >= 1 else ""


def _mailbox_features_without_groups(extra: dict) -> dict:
    value = extra.get("mailbox_features")
    if isinstance(value, dict):
        return dict(value)
    probe_results = extra.get("probe_results")
    if isinstance(probe_results, dict) and isinstance(probe_results.get("features"), dict):
        return dict(probe_results["features"])
    return {}


def _json_dict(value: object) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _group_mailbox_features(features: dict) -> dict:
    return {
        "common": {
            key: value
            for key, value in features.items()
            if key in COMMON_MAILBOX_FEATURES
        },
        "source": {
            key: value
            for key, value in features.items()
            if key in SOURCE_MAILBOX_FEATURES
        },
        "target": {
            key: value
            for key, value in features.items()
            if key in TARGET_MAILBOX_FEATURES
        },
    }


def _flatten_mailbox_feature_groups(groups: dict) -> dict:
    flattened: dict = {}
    for scope in ("common", "source", "target"):
        values = groups.get(scope)
        if isinstance(values, dict):
            flattened.update(values)
    return flattened


def _json_truthy(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _json_array(raw: str | None) -> list:
    if not raw:
        return []
    value = json.loads(raw)
    if not isinstance(value, list):
        raise SyncError("Stored JSON array configuration is invalid")
    return value


def _folder_label_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _json_dump(value: object) -> str | None:
    if value in (None, {}, []):
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
