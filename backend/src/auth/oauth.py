from __future__ import annotations

import imaplib
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

from common.errors import SyncError
from common.files import load_json, resolve_path
from config.validation import MICROSOFT_IMAP_SCOPE, oauth_settings


OAUTH_EXPIRY_SKEW_SECONDS = 120
TARGET_PROFILE_KEY = "__target__"
OAUTH_TOKEN_LOCKS: Dict[str, threading.Lock] = {}
OAUTH_TOKEN_LOCKS_GUARD = threading.Lock()


def microsoft_oauth_urls(auth: dict) -> Tuple[str, str]:
    tenant = urllib.parse.quote(str(auth.get("tenant", "common")), safe="")
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    return f"{base}/devicecode", f"{base}/token"


def microsoft_oauth_scope(auth: dict) -> str:
    scopes = auth.get("scopes", [MICROSOFT_IMAP_SCOPE, "offline_access"])
    if not isinstance(scopes, list) or not all(isinstance(value, str) for value in scopes):
        raise SyncError("auth.scopes must be an array of strings")
    required = {MICROSOFT_IMAP_SCOPE, "offline_access"}
    if not required.issubset(scopes):
        raise SyncError(
            "Microsoft Outlook OAuth scopes must include "
            f"{MICROSOFT_IMAP_SCOPE} and offline_access"
        )
    return " ".join(scopes)


def oauth_error_message(payload: dict, fallback: str) -> str:
    code = payload.get("error")
    description = payload.get("error_description")
    if code and description:
        return f"{code}: {str(description).splitlines()[0]}"
    return str(code or description or fallback)


def post_form_json(
    url: str,
    fields: dict,
    return_oauth_error: bool = False,
) -> dict:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        if return_oauth_error and payload.get("error"):
            return payload
        raise SyncError(
            f"Microsoft OAuth request failed: "
            f"{oauth_error_message(payload, str(exc))}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SyncError(f"Microsoft OAuth request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise SyncError("Microsoft OAuth returned an invalid response")
    return payload


def read_oauth_token(auth: dict) -> dict:
    token = auth.get("token")
    if isinstance(token, dict):
        return dict(token)
    if token is not None:
        raise SyncError("auth.token must be an object")

    token_file_value = auth.get("token_file")
    if not token_file_value:
        return {}
    token_file = Path(str(token_file_value))
    if not token_file.exists():
        return {}
    try:
        token = load_json(token_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"Cannot read OAuth token file {token_file}: {exc}") from exc
    if not isinstance(token, dict):
        raise SyncError(f"OAuth token file must contain an object: {token_file}")
    return token


def write_oauth_token(auth: dict, token: dict) -> None:
    auth["token"] = dict(token)
    if _write_oauth_token_to_database(auth, token):
        return

    token_file_value = auth.get("token_file")
    if not token_file_value:
        return
    token_file = Path(str(token_file_value))
    token_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = token_file.with_name(f".{token_file.name}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(token, ensure_ascii=False, indent=2) + "\n"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, token_file)
        os.chmod(token_file, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def normalized_token_response(payload: dict, previous: Optional[dict] = None) -> dict:
    access_token = payload.get("access_token")
    if not access_token:
        raise SyncError(
            "Microsoft OAuth did not return an access token: "
            f"{oauth_error_message(payload, 'unknown response')}"
        )
    now = int(time.time())
    token = dict(payload)
    token["obtained_at"] = now
    token["expires_at"] = now + int(payload.get("expires_in", 3600))
    if not token.get("refresh_token") and previous and previous.get("refresh_token"):
        token["refresh_token"] = previous["refresh_token"]
    return token


def refresh_microsoft_token(auth: dict, token: dict) -> dict:
    refresh_token = token.get("refresh_token") or auth.get("refresh_token")
    if not refresh_token:
        raise SyncError(
            "Outlook OAuth authorization is missing or expired; run "
            "'authorize-outlook --source <email>'"
        )
    _, token_url = microsoft_oauth_urls(auth)
    fields = {
        "client_id": auth["client_id"],
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": microsoft_oauth_scope(auth),
    }
    if auth.get("client_secret"):
        fields["client_secret"] = auth["client_secret"]
    refreshed = normalized_token_response(post_form_json(token_url, fields), token)
    write_oauth_token(auth, refreshed)
    return refreshed


def microsoft_access_token(profile: dict, force_refresh: bool = False) -> str:
    auth = oauth_settings(profile)
    if auth is None:
        raise SyncError(f"OAuth is not configured for {profile['email']}")
    token_path = _oauth_token_lock_key(auth, profile)
    with OAUTH_TOKEN_LOCKS_GUARD:
        token_lock = OAUTH_TOKEN_LOCKS.setdefault(token_path, threading.Lock())
    with token_lock:
        token = read_oauth_token(auth)
        expires_at = int(token.get("expires_at", 0))
        if (
            force_refresh
            or not token.get("access_token")
            or expires_at <= int(time.time()) + OAUTH_EXPIRY_SKEW_SECONDS
        ):
            token = refresh_microsoft_token(auth, token)
        return str(token["access_token"])


def authenticate_oauth(client: imaplib.IMAP4, profile: dict) -> None:
    email = profile["email"]

    def authenticate(access_token: str) -> None:
        sasl = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
        typ, _ = client.authenticate("XOAUTH2", lambda _: sasl.encode("utf-8"))
        if typ != "OK":
            raise imaplib.IMAP4.error(f"IMAP OAuth authentication failed for {email}")

    try:
        authenticate(microsoft_access_token(profile))
    except imaplib.IMAP4.error:
        # The cached token can be revoked before its advertised expiry. Refresh
        # once before surfacing the authentication error.
        authenticate(microsoft_access_token(profile, force_refresh=True))


def credential_profiles(config: dict) -> Dict[str, dict]:
    profiles: Dict[str, dict] = {}
    target = config.get("target")
    if target and target.get("email"):
        profiles[TARGET_PROFILE_KEY] = dict(target)
    for candidate in config.get("sources", []):
        email = candidate.get("email")
        if not email:
            continue
        if email in profiles:
            raise SyncError(f"Duplicate mailbox configuration for {email}")
        profiles[email] = dict(candidate)
    return profiles


def configured_mailbox_profiles(config: dict) -> list[dict]:
    profiles = []
    target = config.get("target")
    if isinstance(target, dict):
        profiles.append(target)
    for source in config.get("sources", []):
        if not isinstance(source, dict):
            continue
        profiles.append(source)
        target = source.get("target")
        if isinstance(target, dict):
            profiles.append(target)
    return profiles


def resolve_auth_token_paths(config: dict, base: Path) -> None:
    configured_paths: Dict[Path, str] = {}
    for profile in configured_mailbox_profiles(config):
        if not profile:
            continue
        auth = oauth_settings(profile)
        if auth is None:
            continue
        if not auth.get("token_file"):
            continue
        token_file = resolve_path(base, str(auth["token_file"])).resolve()
        owner = configured_paths.get(token_file)
        if owner is not None:
            if owner == profile["email"]:
                auth["token_file"] = str(token_file)
                continue
            raise SyncError(
                f"OAuth token file {token_file} is shared by {owner} and "
                f"{profile['email']}; use a separate token file per mailbox"
            )
        configured_paths[token_file] = profile["email"]
        auth["token_file"] = str(token_file)


def command_authorize_outlook(
    config: dict,
    profiles: Dict[str, dict],
    source_email: str,
) -> int:
    if not any(rule.get("email") == source_email for rule in config.get("sources", [])):
        raise SyncError(f"No source rule for {source_email}")
    profile = profiles[source_email]
    auth = oauth_settings(profile)
    if auth is None:
        raise SyncError(f"OAuth2 is not configured for {source_email}")

    device_url, token_url = microsoft_oauth_urls(auth)
    device = post_form_json(
        device_url,
        {
            "client_id": auth["client_id"],
            "scope": microsoft_oauth_scope(auth),
        },
    )
    for field in ("device_code", "user_code", "verification_uri", "expires_in"):
        if not device.get(field):
            raise SyncError(f"Microsoft device authorization omitted {field}")

    print(
        json.dumps(
            {
                "event": "outlook_authorization_required",
                "source": source_email,
                "verification_uri": device["verification_uri"],
                "user_code": device["user_code"],
                "message": device.get("message"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    interval = max(1, int(device.get("interval", 5)))
    deadline = time.monotonic() + int(device["expires_in"])
    fields = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": auth["client_id"],
        "device_code": device["device_code"],
    }
    while time.monotonic() < deadline:
        time.sleep(interval)
        payload = post_form_json(token_url, fields, return_oauth_error=True)
        error = payload.get("error")
        if not error:
            token = normalized_token_response(payload)
            if not token.get("refresh_token"):
                raise SyncError(
                    "Microsoft did not return a refresh token; verify that "
                    "offline_access is allowed for the application"
                )
            write_oauth_token(auth, token)
            destination = auth.get("token_file") or "database"
            print(
                json.dumps(
                    {
                        "event": "outlook_authorization_succeeded",
                        "source": source_email,
                        "token_storage": destination,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 0
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise SyncError(
            "Outlook authorization failed: "
            f"{oauth_error_message(payload, 'unknown response')}"
        )
    raise SyncError("Outlook authorization expired; run authorize-outlook again")


def _oauth_token_lock_key(auth: dict, profile: dict) -> str:
    storage = auth.get("storage")
    if isinstance(storage, dict) and storage.get("type") == "sqlite":
        return ":".join(
            [
                "sqlite",
                str(storage.get("state_db") or ""),
                str(storage.get("email") or profile.get("email") or ""),
                str(storage.get("service_address") or ""),
            ]
        )
    if auth.get("token_file"):
        return str(Path(str(auth["token_file"])).resolve())
    return f"memory:{profile.get('email', '')}:{auth.get('client_id', '')}"


def _write_oauth_token_to_database(auth: dict, token: dict) -> bool:
    storage = auth.get("storage")
    if not isinstance(storage, dict) or storage.get("type") != "sqlite":
        return False
    state_db = str(storage.get("state_db") or "")
    email = str(storage.get("email") or "")
    service_address = str(storage.get("service_address") or "")
    if not state_db or not email:
        return False

    connection = sqlite3.connect(state_db, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        service_filter = "AND service_address=?" if service_address else ""
        params: tuple[object, ...] = (
            (email, service_address) if service_address else (email,)
        )
        row = connection.execute(
            f"""
            SELECT email, service_address, auth_json
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
        stored = _json_object(row["auth_json"])
        for key in ("type", "provider", "client_id", "tenant", "client_secret", "scopes"):
            if auth.get(key):
                stored[key] = auth[key]
        stored["token"] = token
        stored.pop("token_file", None)
        connection.execute(
            """
            UPDATE mail_accounts
            SET auth_json=?
            WHERE email=? AND service_address=?
            """,
            (
                json.dumps(stored, ensure_ascii=False),
                row["email"],
                row["service_address"],
            ),
        )
        connection.commit()
        auth.pop("token_file", None)
        return True
    finally:
        connection.close()


def _json_object(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
