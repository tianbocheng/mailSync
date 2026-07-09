from __future__ import annotations

import sqlite3
from typing import Optional

from common.clock import now_iso
from storage.config_db import verify_password


def find_user_by_username(
    db: sqlite3.Connection,
    username: str,
) -> Optional[sqlite3.Row]:
    return db.execute(
        """
        SELECT id, username, password_hash, password_salt, password_algorithm,
               password_iterations, role, enabled, must_change_password,
               created_at, updated_at, last_login_at
        FROM login_users
        WHERE username=?
        """,
        (username,),
    ).fetchone()


def authenticate_user(
    db: sqlite3.Connection,
    username: str,
    password: str,
) -> Optional[sqlite3.Row]:
    user = find_user_by_username(db, username)
    if user is None or not user["enabled"]:
        return None
    if user["password_algorithm"] != "pbkdf2_sha256":
        return None
    if not verify_password(
        password,
        user["password_salt"],
        user["password_hash"],
        int(user["password_iterations"]),
    ):
        return None
    db.execute(
        "UPDATE login_users SET last_login_at=?, updated_at=? WHERE id=?",
        (now_iso(), now_iso(), user["id"]),
    )
    db.commit()
    return find_user_by_username(db, username)
