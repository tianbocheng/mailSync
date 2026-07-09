from __future__ import annotations

import imaplib
import json
import sqlite3
import uuid
from typing import Dict, Iterable

from common.errors import SyncError
from imap.protocol import search_uids, select_mailbox
from imap.target import (
    TargetContentVerificationError,
    append_target_copy,
    delete_target_copy,
    repair_target_copy,
)
from sync.labels import (
    configured_label_ids,
    configured_label_names,
    row_folder_labels,
)
from sync.pending import (
    pending_row_message,
    quarantine_pending_operation,
    update_pending_target_uid,
)
from sync.records import upsert_message_location, upsert_state
from sync.target_identity import target_identity_filter


def recover_pending_operations(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    labels_by_name: Dict[str, str],
    source_emails: Iterable[str] | None = None,
    target_email: str = "",
    target_service_address: str = "",
    include_quarantined: bool = False,
) -> int:
    selected_sources = tuple(source_emails or ())
    statuses = ("pending", "quarantined") if include_quarantined else ("pending",)
    status_placeholders = ",".join("?" for _ in statuses)
    target_filter, target_params = target_identity_filter(
        "pending_operations",
        target_email,
        target_service_address,
    )
    if selected_sources:
        placeholders = ",".join("?" for _ in selected_sources)
        rows = db.execute(
            f"""
            SELECT * FROM pending_operations
            WHERE status IN ({status_placeholders})
              {target_filter}
              AND source_email IN ({placeholders})
            ORDER BY created_at
            """,
            (*statuses, *target_params, *selected_sources),
        ).fetchall()
    else:
        rows = db.execute(
            f"""
            SELECT * FROM pending_operations
            WHERE status IN ({status_placeholders})
              {target_filter}
            ORDER BY created_at
            """,
            (*statuses, *target_params),
        ).fetchall()
    recovered = 0
    for row in rows:
        message = pending_row_message(row)
        account_label = str(row["account_label"] or "").strip()
        if account_label not in labels_by_name:
            account_label = ""
        folder_labels = configured_label_names(
            labels_by_name,
            row_folder_labels(row),
        )
        location_folder_label = (
            row["folder_label"] if row["folder_label"] in folder_labels else ""
        )
        operation_target_folder = (
            str(row["target_folder"] or "").strip()
            if "target_folder" in row.keys()
            else ""
        ) or target_folder
        label_ids = configured_label_ids(
            labels_by_name,
            (account_label, *folder_labels),
        )

        _, current_uidvalidity = select_mailbox(
            target,
            operation_target_folder,
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
            operation_target_folder,
            new_uid,
            message,
            label_ids,
        ):
            if row["status"] == "quarantined":
                continue
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
                    operation_target_folder,
                    candidate,
                    message,
                    label_ids,
                ):
                    new_uid = candidate
                    update_pending_target_uid(db, message, candidate)
                    break
            if new_uid is None and row["status"] == "quarantined":
                continue
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
                    operation_target_folder,
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
            delete_target_copy(target, operation_target_folder, old_uid)

        recovered_run_id = "recovered-" + uuid.uuid4().hex
        upsert_state(
            db,
            message,
            current_uidvalidity,
            new_uid,
            account_label,
            folder_labels,
            recovered_run_id,
            sync_action="pending_operation_recovered",
            target_folder=operation_target_folder,
            target_email=target_email,
            target_service_address=target_service_address,
        )
        if message.folder != "__deleted__":
            upsert_message_location(
                db,
                message,
                location_folder_label,
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
