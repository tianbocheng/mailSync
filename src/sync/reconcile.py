from __future__ import annotations

import imaplib
import json
import sqlite3
from datetime import datetime
from email import message_from_bytes
from typing import Dict

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
from sync.labels import normalize_folder_labels, row_folder_labels
from sync.pending import (
    begin_pending_operation,
    quarantine_pending_operation,
    update_pending_target_uid,
)
from sync.records import upsert_state


def reconcile_deleted_messages(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    source_rule: dict,
    labels_by_name: Dict[str, str],
    run_id: str,
) -> int:
    """Reconcile the exact target label set from all current source locations."""
    source_email = source_rule["email"]
    deleted_label_name = source_rule["deleted_label"]
    account_label_name = source_rule["account_label"]
    db.execute(
        """
        DELETE FROM message_locations
        WHERE source_email=? AND last_seen_run<>?
        """,
        (source_email, run_id),
    )
    rows = db.execute(
        "SELECT * FROM messages WHERE source_email=?",
        (source_email,),
    ).fetchall()
    rebuilt = 0
    for row in rows:
        locations = db.execute(
            """
            SELECT * FROM message_locations
            WHERE source_email=? AND content_hash=?
            ORDER BY source_folder, source_uidvalidity, source_uid
            """,
            (source_email, row["content_hash"]),
        ).fetchall()
        desired_labels = normalize_folder_labels(
            location["folder_label"] for location in locations
        )
        if not desired_labels:
            desired_labels = (deleted_label_name,)
        current_labels = row_folder_labels(row)

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
                db.execute(
                    """
                    UPDATE messages SET
                        source_folder='__deleted__', source_uidvalidity=0,
                        source_uid=0, last_seen_run=?, updated_at=?
                    WHERE source_email=? AND content_hash=?
                    """,
                    (run_id, now_iso(), source_email, row["content_hash"]),
                )
            continue

        select_mailbox(target, target_folder, readonly=False)
        metadata, raw = target_fetch(
            target,
            int(row["target_uid"]),
            include_body=True,
        )
        if raw is None:
            raise SyncError(
                f"Cannot rebuild source message {row['content_hash']}: "
                "target body is missing"
            )
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
            location_folder_label = representative["folder_label"]
        else:
            message_folder = "__deleted__"
            message_uidvalidity = 0
            message_uid = 0
            location_folder_label = deleted_label_name

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
        try:
            label_ids = [
                labels_by_name[account_label_name],
                *(labels_by_name[name] for name in desired_labels),
            ]
        except KeyError as exc:
            raise SyncError(
                f"Undefined Synology label during reconciliation: {exc.args[0]}"
            ) from exc

        old_target_uid = int(row["target_uid"])
        ensure_target_delete_will_be_safe(target, old_target_uid)
        begin_pending_operation(
            db,
            target,
            target_folder,
            message,
            account_label_name,
            desired_labels,
            old_target_uid,
            location_folder_label,
        )
        try:
            target_uidvalidity, target_uid = append_target_copy(
                target,
                target_folder,
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
        delete_target_copy(target, target_folder, old_target_uid)
        upsert_state(
            db,
            message,
            target_uidvalidity,
            target_uid,
            account_label_name,
            desired_labels,
            run_id,
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
                    "action": "source_labels_rebuilt",
                    "source": source_email,
                    "content_hash": row["content_hash"],
                    "folder_labels": list(desired_labels),
                },
                ensure_ascii=False,
            )
        )

    db.commit()
    return rebuilt

