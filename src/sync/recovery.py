from __future__ import annotations

import imaplib
import json
import sqlite3
import uuid
from typing import Dict

from common.errors import SyncError
from imap.protocol import search_uids, select_mailbox
from imap.target import (
    TargetContentVerificationError,
    append_target_copy,
    delete_target_copy,
    repair_target_copy,
)
from sync.labels import row_folder_labels
from sync.pending import (
    pending_row_message,
    quarantine_pending_operation,
    update_pending_target_uid,
)
from sync.records import upsert_message_location, upsert_state


def recover_pending_operations(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    labels_by_name: Dict[str, str],
) -> int:
    rows = db.execute(
        """
        SELECT * FROM pending_operations
        WHERE status='pending'
        ORDER BY created_at
        """
    ).fetchall()
    recovered = 0
    for row in rows:
        message = pending_row_message(row)
        folder_labels = row_folder_labels(row)
        try:
            label_ids = [
                labels_by_name[row["account_label"]],
                *(labels_by_name[name] for name in folder_labels),
            ]
        except KeyError as exc:
            raise SyncError(
                f"Cannot recover pending operation; undefined label: {exc.args[0]}"
            ) from exc

        _, current_uidvalidity = select_mailbox(
            target,
            target_folder,
            readonly=False,
        )
        if current_uidvalidity != int(row["target_uidvalidity"]):
            raise SyncError(
                "Target UIDVALIDITY changed while an operation was pending; "
                "manual recovery is required"
            )

        new_uid = int(row["new_target_uid"]) if row["new_target_uid"] else None
        if new_uid is not None and not repair_target_copy(
            target,
            new_uid,
            message,
            label_ids,
        ):
            error = f"Target UID {new_uid} failed pending recovery verification"
            quarantine_pending_operation(db, message, new_uid, error)
            print(
                json.dumps(
                    {
                        "action": "pending_operation_quarantined",
                        "source": message.source_email,
                        "source_uid": message.uid,
                        "target_uid": new_uid,
                        "error": error,
                    },
                    ensure_ascii=False,
                )
            )
            continue

        if new_uid is None:
            candidates = [
                uid
                for uid in search_uids(target)
                if uid > int(row["target_uid_before"])
            ]
            for candidate in reversed(candidates):
                if repair_target_copy(
                    target,
                    candidate,
                    message,
                    label_ids,
                ):
                    new_uid = candidate
                    update_pending_target_uid(db, message, candidate)
                    break
            if new_uid is None and candidates:
                failed_uid = candidates[-1]
                error = (
                    f"No target candidate after UID {row['target_uid_before']} "
                    "passed content verification"
                )
                quarantine_pending_operation(
                    db,
                    message,
                    failed_uid,
                    error,
                )
                print(
                    json.dumps(
                        {
                            "action": "pending_operation_quarantined",
                            "source": message.source_email,
                            "source_uid": message.uid,
                            "target_uid": failed_uid,
                            "error": error,
                        },
                        ensure_ascii=False,
                    )
                )
                continue

        if new_uid is None:
            try:
                _, new_uid = append_target_copy(
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
                print(
                    json.dumps(
                        {
                            "action": "pending_operation_quarantined",
                            "source": message.source_email,
                            "source_uid": message.uid,
                            "target_uid": exc.uid,
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                    )
                )
                continue
            update_pending_target_uid(db, message, new_uid)

        old_uid = int(row["old_target_uid"]) if row["old_target_uid"] else None
        if old_uid is not None and old_uid != new_uid:
            delete_target_copy(target, target_folder, old_uid)

        recovered_run_id = "recovered-" + uuid.uuid4().hex
        upsert_state(
            db,
            message,
            current_uidvalidity,
            new_uid,
            row["account_label"],
            folder_labels,
            recovered_run_id,
        )
        if message.folder != "__deleted__":
            upsert_message_location(
                db,
                message,
                row["folder_label"],
                recovered_run_id,
            )
        db.execute(
            """
            DELETE FROM pending_operations
            WHERE source_email=? AND content_hash=?
            """,
            (message.source_email, message.content_hash),
        )
        db.commit()
        recovered += 1
        print(
            json.dumps(
                {
                    "action": "pending_operation_recovered",
                    "source": message.source_email,
                    "target_uid": new_uid,
                },
                ensure_ascii=False,
            )
        )
    return recovered

