from __future__ import annotations

import imaplib
import json
import sqlite3
from datetime import datetime
from email import message_from_bytes
from typing import Dict, Set, Tuple

from common.clock import now_iso
from common.errors import SyncError
from domain.message import (
    SourceMessage,
    content_hash_matches,
    normalized_content_hash,
    source_message_date,
)
from imap.protocol import select_mailbox
from imap.target import (
    TargetContentVerificationError,
    append_target_copy,
    delete_target_copy,
    ensure_target_delete_will_be_safe,
    target_fetch,
)
from sync.labels import (
    configured_label_ids,
    configured_label_names,
    normalize_folder_labels,
    row_folder_labels,
)
from sync.pending import (
    begin_pending_operation,
    quarantine_pending_operation,
    update_pending_target_uid,
)
from sync.records import upsert_state
from sync.target_identity import target_identity_filter


OUT_OF_SCOPE_KEEP = "keep"
OUT_OF_SCOPE_LABEL = "label"
OUT_OF_SCOPE_DELETE = "delete"
OUT_OF_SCOPE_POLICIES = {
    OUT_OF_SCOPE_KEEP,
    OUT_OF_SCOPE_LABEL,
    OUT_OF_SCOPE_DELETE,
}


def reconcile_deleted_messages(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    source_rule: dict,
    labels_by_name: Dict[str, str],
    run_id: str,
    target_email: str = "",
    target_service_address: str = "",
    rebuild_limit: int | None = None,
    preserved_source_folders: Set[Tuple[str, int]] | None = None,
) -> int:
    """Reconcile the exact target label set from all current source locations."""
    source_email = source_rule["email"]
    account_label_name = str(source_rule.get("account_label") or "").strip()
    if account_label_name not in labels_by_name:
        account_label_name = ""
    target_filter, target_params = target_identity_filter(
        "messages",
        target_email,
        target_service_address,
    )
    preserved_source_folders = preserved_source_folders or set()
    preserve_filter = ""
    if preserved_source_folders:
        db.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS preserved_source_folders (
                source_folder TEXT NOT NULL,
                source_uidvalidity INTEGER NOT NULL,
                PRIMARY KEY (source_folder, source_uidvalidity)
            )
            """
        )
        db.execute("DELETE FROM preserved_source_folders")
        db.executemany(
            """
            INSERT OR IGNORE INTO preserved_source_folders (
                source_folder, source_uidvalidity
            ) VALUES (?, ?)
            """,
            (
                (folder, int(uidvalidity))
                for folder, uidvalidity in preserved_source_folders
            ),
        )
        preserve_filter = """
          AND NOT EXISTS (
              SELECT 1
              FROM preserved_source_folders AS preserved
              WHERE preserved.source_folder=message_locations.source_folder
                AND preserved.source_uidvalidity=
                    message_locations.source_uidvalidity
          )
        """
    db.execute(
        f"""
        DELETE FROM message_locations
        WHERE source_email=? AND last_seen_run<>?
          {preserve_filter}
          AND content_hash IN (
              SELECT content_hash
              FROM messages
              WHERE source_email=?
                {target_filter}
          )
        """,
        (
            source_email,
            run_id,
            source_email,
            *target_params,
        ),
    )
    rows = db.execute(
        f"""
        SELECT * FROM messages
        WHERE source_email=?
          {target_filter}
        """,
        (
            source_email,
            *target_params,
        ),
    ).fetchall()
    rebuilt = 0
    for row in rows:
        row_target_folder = (
            str(row["target_folder"] or "").strip()
            if "target_folder" in row.keys()
            else ""
        ) or target_folder
        locations = db.execute(
            """
            SELECT * FROM message_locations
            WHERE source_email=? AND content_hash=?
            ORDER BY source_folder, source_uidvalidity, source_uid
            """,
            (source_email, row["content_hash"]),
        ).fetchall()
        current_labels = row_folder_labels(row)
        if locations:
            desired_labels = configured_label_names(
                labels_by_name,
                normalize_folder_labels(
                    label
                    for location in locations
                    for label in _location_source_labels(source_rule, location)
                ),
            )
            out_of_scope_policy = ""
        else:
            out_of_scope_policy = _out_of_scope_policy(source_rule)
            if out_of_scope_policy == OUT_OF_SCOPE_LABEL:
                if _target_copy_already_deleted(row):
                    _mark_out_of_scope_deleted(db, row, run_id)
                    continue
                out_of_scope_labels = _out_of_scope_labels(
                    source_rule,
                    labels_by_name,
                )
                if not out_of_scope_labels:
                    raise SyncError("移出范围标记标签未定义")
                desired_labels = out_of_scope_labels
            elif out_of_scope_policy == OUT_OF_SCOPE_DELETE:
                if _target_copy_already_deleted(row):
                    _mark_out_of_scope_deleted(db, row, run_id)
                    continue
                if _rebuild_limit_reached(
                    source_email,
                    rebuilt,
                    rebuild_limit,
                ):
                    break
                delete_target_copy(
                    target,
                    row_target_folder,
                    int(row["target_uid"]),
                )
                _mark_out_of_scope_deleted(db, row, run_id)
                db.commit()
                rebuilt += 1
                print(
                    json.dumps(
                        {
                            "action": "source_out_of_scope_deleted",
                            "source": source_email,
                            "content_hash": row["content_hash"],
                        },
                        ensure_ascii=False,
                    )
                )
                continue
            else:
                desired_labels = current_labels

        if desired_labels == current_labels:
            if locations:
                representative = locations[0]
                db.execute(
                    """
                    UPDATE messages SET
                        source_folder=?, source_uidvalidity=?, source_uid=?,
                        last_seen_run=?, updated_at=?
                    WHERE source_email=? AND content_hash=?
                    """,
                    (
                        representative["source_folder"],
                        representative["source_uidvalidity"],
                        representative["source_uid"],
                        run_id,
                        now_iso(),
                        source_email,
                        row["content_hash"],
                    ),
                )
            else:
                _mark_out_of_scope(
                    db,
                    row,
                    run_id,
                    (
                        "source_out_of_scope_labeled"
                        if out_of_scope_policy == OUT_OF_SCOPE_LABEL
                        else "source_out_of_scope"
                    ),
                )
            continue

        if _rebuild_limit_reached(source_email, rebuilt, rebuild_limit):
            break

        select_mailbox(target, row_target_folder, readonly=False)
        try:
            metadata, raw = target_fetch(
                target,
                int(row["target_uid"]),
                include_body=True,
            )
        except SyncError as exc:
            _mark_reconciliation_skipped(
                db,
                row,
                run_id,
                f"target body unavailable: {exc}",
            )
            continue
        if raw is None:
            _mark_reconciliation_skipped(
                db,
                row,
                run_id,
                "target body is missing",
            )
            continue
        expected_target_hash = (
            row["target_normalized_hash"] or row["normalized_hash"]
        )
        if expected_target_hash:
            content_matches = (
                normalized_content_hash(raw) == expected_target_hash
            )
        else:
            content_matches = content_hash_matches(raw, row["content_hash"])
        if not content_matches:
            raise SyncError(
                f"Target UID {row['target_uid']} content hash changed; "
                "refusing label reconciliation"
            )

        if locations:
            representative = locations[0]
            message_folder = representative["source_folder"]
            message_uidvalidity = int(representative["source_uidvalidity"])
            message_uid = int(representative["source_uid"])
            location_folder_label = (
                representative["folder_label"]
                if representative["folder_label"] in desired_labels
                else ""
            )
        else:
            message_folder = "__out_of_scope__"
            message_uidvalidity = 0
            message_uid = 0
            location_folder_label = desired_labels[0] if desired_labels else ""

        message = SourceMessage(
            source_email=source_email,
            folder=message_folder,
            uidvalidity=message_uidvalidity,
            uid=message_uid,
            raw=raw,
            content_hash=row["content_hash"],
            message_id=(
                message_from_bytes(raw).get("Message-ID", "").strip() or None
            ),
            internal_date=(
                datetime.fromisoformat(row["internal_date"])
                if row["internal_date"]
                else source_message_date(metadata, raw)
            ),
            system_flags=tuple(json.loads(row["source_flags"])),
        )
        label_ids = configured_label_ids(
            labels_by_name,
            (account_label_name, *desired_labels),
        )

        old_target_uid = int(row["target_uid"])
        ensure_target_delete_will_be_safe(target, old_target_uid)
        begin_pending_operation(
            db,
            target,
            row_target_folder,
            message,
            account_label_name,
            desired_labels,
            old_target_uid,
            location_folder_label,
            target_email,
            target_service_address,
        )
        try:
            target_uidvalidity, target_uid = append_target_copy(
                target,
                row_target_folder,
                message,
                label_ids,
            )
        except TargetContentVerificationError as exc:
            quarantine_pending_operation(
                db,
                message,
                exc.uid,
                str(exc),
            )
            raise
        update_pending_target_uid(db, message, target_uid)
        delete_target_copy(target, row_target_folder, old_target_uid)
        upsert_state(
            db,
            message,
            target_uidvalidity,
            target_uid,
            account_label_name,
            desired_labels,
            run_id,
            sync_action=(
                "source_labels_rebuilt"
                if locations
                else "source_out_of_scope_labeled"
            ),
            target_folder=row_target_folder,
            target_email=target_email,
            target_service_address=target_service_address,
        )
        db.execute(
            """
            DELETE FROM pending_operations
            WHERE source_email=? AND content_hash=?
            """,
            (message.source_email, message.content_hash),
        )
        db.commit()
        rebuilt += 1
        print(
            json.dumps(
                {
                    "action": (
                        "source_labels_rebuilt"
                        if locations
                        else "source_out_of_scope_labeled"
                    ),
                    "source": source_email,
                    "content_hash": row["content_hash"],
                    "folder_labels": list(desired_labels),
                },
                ensure_ascii=False,
            )
        )

    db.commit()
    return rebuilt


def _rebuild_limit_reached(
    source_email: str,
    rebuilt: int,
    rebuild_limit: int | None,
) -> bool:
    if rebuild_limit is None or rebuild_limit < 1 or rebuilt < rebuild_limit:
        return False
    print(
        json.dumps(
            {
                "event": "source_reconciliation_deferred",
                "source": source_email,
                "rebuilt": rebuilt,
                "limit": rebuild_limit,
            },
            ensure_ascii=False,
        )
    )
    return True


def _mark_reconciliation_skipped(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    run_id: str,
    reason: str,
) -> None:
    db.execute(
        """
        UPDATE messages SET
            last_sync_action='source_reconciliation_skipped',
            last_seen_run=?, updated_at=?
        WHERE source_email=? AND content_hash=?
        """,
        (
            run_id,
            now_iso(),
            row["source_email"],
            row["content_hash"],
        ),
    )
    print(
        json.dumps(
            {
                "event": "source_reconciliation_skipped",
                "source": row["source_email"],
                "content_hash": row["content_hash"],
                "target_uid": row["target_uid"],
                "reason": reason,
            },
            ensure_ascii=False,
        )
    )


def _out_of_scope_policy(source_rule: dict) -> str:
    policy = str(source_rule.get("out_of_scope_policy") or OUT_OF_SCOPE_KEEP).strip()
    return policy if policy in OUT_OF_SCOPE_POLICIES else OUT_OF_SCOPE_KEEP


def _location_source_labels(
    source_rule: dict,
    location: sqlite3.Row,
) -> tuple[str, ...]:
    mapping = source_rule.get("folder_labels", {})
    value = None
    if isinstance(mapping, dict):
        value = mapping.get(str(location["source_folder"] or ""))
    if value is None:
        value = location["folder_label"] or source_rule.get("default_folder_label", "")
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple)):
        values = tuple(str(item) for item in value)
    else:
        values = (str(value),)
    return normalize_folder_labels(values)


def _out_of_scope_labels(
    source_rule: dict,
    labels_by_name: Dict[str, str],
) -> tuple[str, ...]:
    raw = source_rule.get("out_of_scope_labels", source_rule.get("out_of_scope_label"))
    if isinstance(raw, str):
        labels = [raw]
    elif isinstance(raw, (list, tuple)):
        labels = [str(item) for item in raw]
    else:
        labels = []
    return configured_label_names(labels_by_name, labels)


def _target_copy_already_deleted(row: sqlite3.Row) -> bool:
    try:
        target_uid = int(row["target_uid"] or 0)
    except (TypeError, ValueError):
        target_uid = 0
    return (
        target_uid <= 0
        or str(row["last_sync_action"] or "") == "source_out_of_scope_deleted"
    )


def _mark_out_of_scope(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    run_id: str,
    action: str,
) -> None:
    db.execute(
        """
        UPDATE messages SET
            source_folder='__out_of_scope__', source_uidvalidity=0,
            source_uid=0, last_sync_action=?,
            last_seen_run=?, updated_at=?
        WHERE source_email=? AND content_hash=?
        """,
        (
            action,
            run_id,
            now_iso(),
            row["source_email"],
            row["content_hash"],
        ),
    )


def _mark_out_of_scope_deleted(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    run_id: str,
) -> None:
    db.execute(
        """
        UPDATE messages SET
            source_folder='__out_of_scope__', source_uidvalidity=0,
            source_uid=0, target_uidvalidity=0, target_uid=0,
            folder_label='', folder_labels_json='[]',
            last_sync_action='source_out_of_scope_deleted',
            last_seen_run=?, updated_at=?
        WHERE source_email=? AND content_hash=?
        """,
        (
            run_id,
            now_iso(),
            row["source_email"],
            row["content_hash"],
        ),
    )
