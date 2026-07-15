from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie
from typing import Optional


SESSION_COOKIE = "mailsync_session"
SESSION_TTL_SECONDS = 12 * 60 * 60


def make_session_cookie(db: sqlite3.Connection, username: str) -> str:
    expires = int(time.time()) + SESSION_TTL_SECONDS
    payload = _encode_json({"username": username, "expires": expires})
    signature = _sign(get_session_secret(db), payload)
    return (
        f"{SESSION_COOKIE}={payload}.{signature}; Path=/; HttpOnly; "
        f"SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}{_secure_suffix()}"
    )


def clear_session_cookie() -> str:
    return (
        f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; "
        "Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
        f"{_secure_suffix()}"
    )


def _secure_suffix() -> str:
    enabled = os.environ.get("MAIL_SYNC_COOKIE_SECURE", "").strip().lower()
    return "; Secure" if enabled in {"1", "true", "yes", "on"} else ""


def current_username(
    db: sqlite3.Connection,
    cookie_header: Optional[str],
) -> Optional[str]:
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None
    morsel = cookie.get(SESSION_COOKIE)
    if morsel is None:
        return None
    raw = morsel.value
    if "." not in raw:
        return None
    payload, signature = raw.rsplit(".", 1)
    expected = _sign(get_session_secret(db), payload)
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        data = _decode_json(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if int(data.get("expires", 0)) < int(time.time()):
        return None
    username = data.get("username")
    return username if isinstance(username, str) and username else None


def get_session_secret(db: sqlite3.Connection) -> str:
    row = db.execute(
        "SELECT value_json FROM app_settings WHERE key='web_session_secret'"
    ).fetchone()
    if row is not None:
        value = json.loads(row["value_json"])
        if isinstance(value, str) and value:
            return value
    secret = secrets.token_urlsafe(32)
    db.execute(
        """
        INSERT INTO app_settings(key, value_json)
        VALUES('web_session_secret', ?)
        ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json
        """,
        (json.dumps(secret),),
    )
    db.commit()
    return secret


def _sign(secret: str, payload: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _encode_json(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_json(payload: str) -> dict:
    padded = payload + "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    value = json.loads(decoded)
    if not isinstance(value, dict):
        raise ValueError("session payload must be an object")
    return value
