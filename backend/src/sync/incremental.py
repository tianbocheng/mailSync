from __future__ import annotations

import sqlite3
from typing import List, Sequence, Set

from common.clock import now_iso
from sync.target_identity import target_identity_filter


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
