from __future__ import annotations

import hashlib
import sqlite3
from typing import List, Sequence, Set, Tuple

from common.clock import now_iso
from sync.target_identity import target_identity_filter, target_identity_params


MailboxStatus = Tuple[int, int, int]


def uid_list_signature(source_uids: Sequence[int]) -> str:
    payload = ",".join(str(uid) for uid in sorted(int(uid) for uid in source_uids))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def incremental_candidate_uids(
    db: sqlite3.Connection,
    source_email: str,
    source_folder: str,
    source_uidvalidity: int,
    source_uids: Sequence[int],
    target_email: str = "",
    target_service_address: str = "",
) -> List[int]:
    if not str(target_email or "").strip():
        known_uids = {
            int(row[0])
            for row in db.execute(
                """
                SELECT source_uid
                FROM message_locations
                WHERE source_email=? AND source_folder=?
                  AND source_uidvalidity=?
                """,
                (source_email, source_folder, source_uidvalidity),
            ).fetchall()
        }
        return [uid for uid in source_uids if uid not in known_uids]
    target_filter, target_params = target_identity_filter(
        "messages",
        target_email,
        target_service_address,
    )
    known_uids = {
        int(row[0])
        for row in db.execute(
            f"""
            SELECT locations.source_uid
            FROM message_locations AS locations
            JOIN messages
              ON messages.source_email=locations.source_email
             AND messages.content_hash=locations.content_hash
            WHERE locations.source_email=? AND locations.source_folder=?
              AND locations.source_uidvalidity=?
              {target_filter}
            """,
            (
                source_email,
                source_folder,
                source_uidvalidity,
                *target_params,
            ),
        ).fetchall()
    }
    return [uid for uid in source_uids if uid not in known_uids]


def verified_known_location_uids(
    db: sqlite3.Connection,
    source_email: str,
    source_folder: str,
    source_uidvalidity: int,
    target_email: str = "",
    target_service_address: str = "",
) -> Set[int]:
    target_filter, target_params = target_identity_filter(
        "messages",
        target_email,
        target_service_address,
    )
    return {
        int(row[0])
        for row in db.execute(
            f"""
            SELECT locations.source_uid
            FROM message_locations AS locations
            JOIN messages
              ON messages.source_email=locations.source_email
             AND messages.content_hash=locations.content_hash
            WHERE locations.source_email=?
              AND locations.source_folder=?
              AND locations.source_uidvalidity=?
              AND messages.internal_date IS NOT NULL
              AND messages.source_date_verified=1
              {target_filter}
            """,
            (
                source_email,
                source_folder,
                source_uidvalidity,
                *target_params,
            ),
        ).fetchall()
    }


def folder_scan_state_unchanged(
    db: sqlite3.Connection,
    source_email: str,
    source_folder: str,
    status: MailboxStatus,
    scan_signature: str = "",
    target_email: str = "",
    target_service_address: str = "",
) -> bool:
    uidvalidity, uidnext, messages = status
    if uidnext <= 0:
        return False
    target_email_key, target_service_key = target_identity_params(
        target_email,
        target_service_address,
    )
    row = db.execute(
        """
        SELECT source_uidvalidity, source_uidnext, source_messages,
               uid_list_signature, scan_signature
        FROM source_folder_scan_state
        WHERE source_email=? AND source_folder=?
          AND target_email=? AND target_service_address=?
        """,
        (
            source_email,
            source_folder,
            target_email_key,
            target_service_key,
        ),
    ).fetchone()
    if row is None:
        return False
    return (
        int(row["source_uidvalidity"]) == int(uidvalidity)
        and int(row["source_uidnext"]) == int(uidnext)
        and int(row["source_messages"]) == int(messages)
        and str(row["scan_signature"] or "") == str(scan_signature or "")
    )


def folder_uid_list_state_unchanged(
    db: sqlite3.Connection,
    source_email: str,
    source_folder: str,
    status: MailboxStatus,
    source_uids: Sequence[int],
    scan_signature: str = "",
    target_email: str = "",
    target_service_address: str = "",
) -> bool:
    uidvalidity, uidnext, messages = status
    if uidnext > 0:
        return False
    target_email_key, target_service_key = target_identity_params(
        target_email,
        target_service_address,
    )
    row = db.execute(
        """
        SELECT source_uidvalidity, source_messages,
               uid_list_signature, scan_signature
        FROM source_folder_scan_state
        WHERE source_email=? AND source_folder=?
          AND target_email=? AND target_service_address=?
        """,
        (
            source_email,
            source_folder,
            target_email_key,
            target_service_key,
        ),
    ).fetchone()
    if row is None:
        return False
    return (
        int(row["source_uidvalidity"]) == int(uidvalidity)
        and int(row["source_messages"]) == int(messages)
        and str(row["scan_signature"] or "") == str(scan_signature or "")
        and str(row["uid_list_signature"] or "")
        == uid_list_signature(source_uids)
    )


def upsert_folder_scan_state(
    db: sqlite3.Connection,
    source_email: str,
    source_folder: str,
    status: MailboxStatus,
    run_id: str,
    scan_signature: str = "",
    target_email: str = "",
    target_service_address: str = "",
    source_uids: Sequence[int] | None = None,
) -> None:
    uidvalidity, uidnext, messages = status
    uid_signature = ""
    if uidnext <= 0:
        if source_uids is None:
            return
        uid_signature = uid_list_signature(source_uids)
    if uidnext <= 0 and int(messages) != len(source_uids or ()):
        return
    target_email_key, target_service_key = target_identity_params(
        target_email,
        target_service_address,
    )
    db.execute(
        """
        INSERT INTO source_folder_scan_state (
            source_email, source_folder, target_email, target_service_address,
            source_uidvalidity, source_uidnext, source_messages,
            uid_list_signature, scan_signature, last_complete_run, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (
            source_email, source_folder, target_email, target_service_address
        ) DO UPDATE SET
            source_uidvalidity=excluded.source_uidvalidity,
            source_uidnext=excluded.source_uidnext,
            source_messages=excluded.source_messages,
            uid_list_signature=excluded.uid_list_signature,
            scan_signature=excluded.scan_signature,
            last_complete_run=excluded.last_complete_run,
            updated_at=excluded.updated_at
        """,
        (
            source_email,
            source_folder,
            target_email_key,
            target_service_key,
            uidvalidity,
            uidnext,
            messages,
            uid_signature,
            str(scan_signature or ""),
            run_id,
            now_iso(),
        ),
    )


def mark_folder_locations_seen(
    db: sqlite3.Connection,
    source_email: str,
    source_folder: str,
    source_uidvalidity: int,
    run_id: str,
    target_email: str = "",
    target_service_address: str = "",
) -> int:
    updated_at = now_iso()
    target_filter, target_params = target_identity_filter(
        "messages",
        target_email,
        target_service_address,
    )
    count = int(
        db.execute(
            f"""
            SELECT COUNT(*)
            FROM message_locations AS locations
            JOIN messages
              ON messages.source_email=locations.source_email
             AND messages.content_hash=locations.content_hash
            WHERE locations.source_email=?
              AND locations.source_folder=?
              AND locations.source_uidvalidity=?
              {target_filter}
            """,
            (
                source_email,
                source_folder,
                source_uidvalidity,
                *target_params,
            ),
        ).fetchone()[0]
    )
    if count == 0:
        return 0
    db.execute(
        f"""
        UPDATE message_locations
        SET last_seen_run=?, updated_at=?
        WHERE source_email=? AND source_folder=?
          AND source_uidvalidity=?
          AND content_hash IN (
              SELECT content_hash
              FROM messages
              WHERE source_email=?
                {target_filter}
          )
        """,
        (
            run_id,
            updated_at,
            source_email,
            source_folder,
            source_uidvalidity,
            source_email,
            *target_params,
        ),
    )
    db.execute(
        f"""
        UPDATE messages
        SET last_seen_run=?, updated_at=?
        WHERE source_email=?
          {target_filter}
          AND content_hash IN (
              SELECT content_hash
              FROM message_locations
              WHERE source_email=? AND source_folder=?
                AND source_uidvalidity=?
          )
        """,
        (
            run_id,
            updated_at,
            source_email,
            *target_params,
            source_email,
            source_folder,
            source_uidvalidity,
        ),
    )
    return count


def bulk_mark_locations_seen(
    db: sqlite3.Connection,
    source_email: str,
    source_folder: str,
    source_uidvalidity: int,
    source_uids: Sequence[int],
    run_id: str,
    target_email: str = "",
    target_service_address: str = "",
) -> None:
    if not source_uids:
        return
    updated_at = now_iso()
    target_filter, target_params = target_identity_filter(
        "messages",
        target_email,
        target_service_address,
    )
    db.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS current_folder_uids (
            source_uid INTEGER PRIMARY KEY
        )
        """
    )
    db.execute("DELETE FROM current_folder_uids")
    db.executemany(
        "INSERT OR IGNORE INTO current_folder_uids(source_uid) VALUES (?)",
        ((uid,) for uid in source_uids),
    )
    db.execute(
        f"""
        UPDATE message_locations
        SET last_seen_run=?, updated_at=?
        WHERE source_email=? AND source_folder=?
          AND source_uidvalidity=?
          AND source_uid IN (SELECT source_uid FROM current_folder_uids)
          AND content_hash IN (
              SELECT content_hash
              FROM messages
              WHERE source_email=?
                {target_filter}
          )
        """,
        (
            run_id,
            updated_at,
            source_email,
            source_folder,
            source_uidvalidity,
            source_email,
            *target_params,
        ),
    )
    db.execute(
        f"""
        UPDATE messages
        SET last_seen_run=?, updated_at=?
        WHERE source_email=?
          {target_filter}
          AND content_hash IN (
              SELECT content_hash
              FROM message_locations
              WHERE source_email=? AND source_folder=?
                AND source_uidvalidity=?
                AND source_uid IN (
                    SELECT source_uid FROM current_folder_uids
                )
          )
        """,
        (
            run_id,
            updated_at,
            source_email,
            *target_params,
            source_email,
            source_folder,
            source_uidvalidity,
        ),
    )
