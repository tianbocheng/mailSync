from __future__ import annotations

import sqlite3
from typing import List, Sequence, Set

from common.clock import now_iso


def incremental_candidate_uids(
    db: sqlite3.Connection,
    source_email: str,
    source_folder: str,
    source_uidvalidity: int,
    source_uids: Sequence[int],
) -> List[int]:
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


def verified_known_location_uids(
    db: sqlite3.Connection,
    source_email: str,
    source_folder: str,
    source_uidvalidity: int,
) -> Set[int]:
    return {
        int(row[0])
        for row in db.execute(
            """
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
            """,
            (source_email, source_folder, source_uidvalidity),
        ).fetchall()
    }


def bulk_mark_locations_seen(
    db: sqlite3.Connection,
    source_email: str,
    source_folder: str,
    source_uidvalidity: int,
    source_uids: Sequence[int],
    run_id: str,
) -> None:
    if not source_uids:
        return
    updated_at = now_iso()
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
        """
        UPDATE message_locations
        SET last_seen_run=?, updated_at=?
        WHERE source_email=? AND source_folder=?
          AND source_uidvalidity=?
          AND source_uid IN (SELECT source_uid FROM current_folder_uids)
        """,
        (
            run_id,
            updated_at,
            source_email,
            source_folder,
            source_uidvalidity,
        ),
    )
    db.execute(
        """
        UPDATE messages
        SET last_seen_run=?, updated_at=?
        WHERE source_email=?
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
            source_email,
            source_folder,
            source_uidvalidity,
        ),
    )

