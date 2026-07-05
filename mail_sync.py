#!/usr/bin/env python3
"""Incrementally mirror source IMAP mail into one target INBOX.

Synology MailPlus label removals are not reliably reflected by standard IMAP.
When a source message changes folders, this synchronizer therefore appends and
verifies a fresh target copy with the desired labels before deleting the old
target copy.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import imaplib
import io
import json
import os
import poplib
import re
import sqlite3
import ssl
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


SYSTEM_FLAGS = {
    r"\Seen",
    r"\Answered",
    r"\Flagged",
    r"\Draft",
}
LIST_RE = re.compile(
    rb"^\((?P<flags>[^)]*)\)\s+"
    rb"(?P<delimiter>NIL|\"(?:[^\"\\]|\\.)*\")\s+"
    rb"(?P<mailbox>.+)$"
)
INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')
FLAGS_RE = re.compile(rb"FLAGS \((.*?)\)")
UID_RE = re.compile(rb"\bUID (\d+)\b")
RFC822_SIZE_RE = re.compile(rb"\bRFC822\.SIZE (\d+)\b")
APPENDUID_RE = re.compile(rb"\[APPENDUID (\d+) (\d+)\]")
MICROSOFT_IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"
OAUTH_EXPIRY_SKEW_SECONDS = 120
TARGET_PROFILE_KEY = "__target__"
NO_CHANGE_ACTIONS = {
    "unchanged",
    "duplicate_location_ignored",
    "quarantined",
    "source_location_added",
}


@dataclass(frozen=True)
class FolderInfo:
    raw_name: str
    display_name: str
    attributes: Tuple[str, ...]


@dataclass(frozen=True)
class SourceMessage:
    source_email: str
    folder: str
    uidvalidity: int
    uid: int
    raw: bytes
    content_hash: str
    message_id: Optional[str]
    internal_date: Optional[datetime]
    system_flags: Tuple[str, ...]


class SyncError(RuntimeError):
    pass


class SourceMessageUnavailable(SyncError):
    pass


class TargetContentVerificationError(SyncError):
    def __init__(self, uid: int):
        self.uid = uid
        super().__init__(f"Target UID {uid} failed content verification")


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_path(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def quote_imap_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def quote_mailbox(name: str) -> str:
    return quote_imap_string(name)


def decode_modified_utf7(value: str) -> str:
    """Decode IMAP modified UTF-7 without third-party dependencies."""
    import base64

    result: List[str] = []
    index = 0
    while index < len(value):
        amp = value.find("&", index)
        if amp < 0:
            result.append(value[index:])
            break
        result.append(value[index:amp])
        end = value.find("-", amp)
        if end < 0:
            result.append(value[amp:])
            break
        token = value[amp + 1 : end]
        if not token:
            result.append("&")
        else:
            padded = token.replace(",", "/")
            padded += "=" * ((4 - len(padded) % 4) % 4)
            result.append(base64.b64decode(padded).decode("utf-16-be"))
        index = end + 1
    return "".join(result)


def unquote_imap(value: bytes) -> str:
    value = value.strip()
    if value.startswith(b'"') and value.endswith(b'"'):
        value = value[1:-1]
        value = re.sub(rb"\\(.)", rb"\1", value)
    return value.decode("ascii", "strict")


def parse_list_row(row: bytes) -> FolderInfo:
    match = LIST_RE.match(row)
    if not match:
        raise SyncError(f"Unrecognized IMAP LIST row: {row!r}")
    raw_name = unquote_imap(match.group("mailbox"))
    attributes = tuple(
        item.decode("ascii", "replace")
        for item in match.group("flags").split()
    )
    return FolderInfo(
        raw_name=raw_name,
        display_name=decode_modified_utf7(raw_name),
        attributes=attributes,
    )


def oauth_settings(profile: dict) -> Optional[dict]:
    auth = profile.get("auth")
    if auth is None:
        return None
    if not isinstance(auth, dict):
        raise SyncError(f"auth must be an object for {profile.get('email', '<unknown>')}")
    auth_type = auth.get("type", "password")
    if auth_type == "password":
        return None
    if auth_type != "oauth2" or auth.get("provider", "microsoft") != "microsoft":
        raise SyncError(
            f"Unsupported authentication configuration for "
            f"{profile.get('email', '<unknown>')}"
        )
    for field in ("client_id", "token_file"):
        if not auth.get(field):
            raise SyncError(
                f"Missing auth.{field} for {profile.get('email', '<unknown>')}"
            )
    return auth


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
    token_file = Path(auth["token_file"])
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
    token_file = Path(auth["token_file"])
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


def connect(profile: dict) -> imaplib.IMAP4:
    host = profile["imap_server"]
    port = int(profile.get("imap_port", 993 if profile.get("use_ssl", True) else 143))
    if profile.get("use_ssl", True):
        client: imaplib.IMAP4 = imaplib.IMAP4_SSL(host, port)
    else:
        client = imaplib.IMAP4(host, port)
    if oauth_settings(profile) is not None:
        authenticate_oauth(client, profile)
    else:
        if not profile.get("password"):
            raise SyncError(f"Missing password for {profile['email']}")
        typ, _ = client.login(profile["email"], profile["password"])
        if typ != "OK":
            raise SyncError(f"IMAP login failed for {profile['email']}")
    identity = profile.get("imap_id")
    if identity:
        fields = []
        for key, value in identity.items():
            escaped_key = str(key).replace("\\", "\\\\").replace('"', '\\"')
            escaped_value = str(value).replace("\\", "\\\\").replace('"', '\\"')
            fields.extend((f'"{escaped_key}"', f'"{escaped_value}"'))
        typ, response = client.xatom("ID", "(" + " ".join(fields) + ")")
        if typ != "OK":
            raise SyncError(
                f"IMAP ID rejected for {profile['email']}: {response!r}"
            )
    return client


def safe_logout(client: imaplib.IMAP4) -> None:
    try:
        client.logout()
    except (imaplib.IMAP4.error, OSError, EOFError):
        pass


def ensure_source_connection(
    profile: dict,
    client: imaplib.IMAP4,
    folder: str,
    expected_uidvalidity: int,
) -> imaplib.IMAP4:
    try:
        typ, _ = client.noop()
        if typ == "OK":
            return client
    except (imaplib.IMAP4.error, OSError, EOFError):
        pass

    safe_logout(client)
    replacement = connect(profile)
    try:
        _, uidvalidity = select_mailbox(
            replacement,
            folder,
            readonly=True,
        )
        if uidvalidity != expected_uidvalidity:
            raise SyncError(
                f"Source UIDVALIDITY changed while reconnecting "
                f"{profile['email']} {folder}"
            )
        return replacement
    except Exception:
        safe_logout(replacement)
        raise


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


def resolve_auth_token_paths(config: dict, base: Path) -> None:
    configured_paths: Dict[Path, str] = {}
    profiles = [config.get("target")] + list(config.get("sources", []))
    for profile in profiles:
        if not profile:
            continue
        auth = oauth_settings(profile)
        if auth is None:
            continue
        token_file = resolve_path(base, str(auth["token_file"])).resolve()
        owner = configured_paths.get(token_file)
        if owner is not None:
            raise SyncError(
                f"OAuth token file {token_file} is shared by {owner} and "
                f"{profile['email']}; use a separate token file per mailbox"
            )
        configured_paths[token_file] = profile["email"]
        auth["token_file"] = str(token_file)


def list_folders(client: imaplib.IMAP4) -> List[FolderInfo]:
    typ, rows = client.list()
    if typ != "OK":
        raise SyncError("IMAP LIST failed")
    return [parse_list_row(row) for row in rows or []]


def selectable_source_folders(
    folders: Sequence[FolderInfo],
    source_rule: dict,
) -> List[FolderInfo]:
    excluded = {name.casefold() for name in source_rule.get("exclude_folders", [])}
    selected = []
    for folder in folders:
        attrs = {attr.casefold() for attr in folder.attributes}
        if r"\noselect" in attrs or r"\drafts" in attrs:
            continue
        if folder.raw_name.casefold() in excluded or folder.display_name.casefold() in excluded:
            continue
        selected.append(folder)
    return selected


def response_int(client: imaplib.IMAP4, name: str) -> int:
    values = client.untagged_responses.get(name.upper(), [])
    if not values:
        typ, response = client.response(name)
        values = response or []
    if not values:
        raise SyncError(f"Server did not return {name}")
    match = re.search(rb"\d+", values[-1])
    if not match:
        raise SyncError(f"Invalid {name}: {values[-1]!r}")
    return int(match.group())


def parse_fetch_tuple(rows: Sequence[object]) -> Tuple[bytes, bytes]:
    metadata_parts = []
    body = None
    for row in rows:
        if isinstance(row, tuple) and len(row) == 2:
            if isinstance(row[0], bytes):
                metadata_parts.append(row[0])
            if body is None and isinstance(row[1], bytes):
                body = row[1]
        elif isinstance(row, bytes):
            # Some IMAP servers return attributes following a BODY literal
            # (notably INTERNALDATE and FLAGS) as a separate response item.
            metadata_parts.append(row)
    if body is None:
        raise SyncError("IMAP FETCH did not return a message body")
    return b" ".join(metadata_parts), body


def fetch_message_metadata(
    client: imaplib.IMAP4,
    uids: Sequence[int],
) -> Dict[int, Tuple[Optional[datetime], Tuple[str, ...]]]:
    """Fetch lightweight source metadata without downloading message bodies."""
    results: Dict[int, Tuple[Optional[datetime], Tuple[str, ...]]] = {}
    for offset in range(0, len(uids), 200):
        batch = uids[offset : offset + 200]
        if not batch:
            continue
        typ, rows = client.uid(
            "fetch",
            ",".join(str(uid) for uid in batch),
            "(UID INTERNALDATE FLAGS)",
        )
        if typ != "OK":
            raise SyncError("IMAP metadata FETCH failed")
        for row in rows or []:
            metadata = row[0] if isinstance(row, tuple) else row
            if not isinstance(metadata, bytes):
                continue
            uid_match = UID_RE.search(metadata)
            if uid_match:
                results[int(uid_match.group(1))] = (
                    parse_internal_date(metadata),
                    parse_flags(metadata),
                )
    return results


def fetch_date_header_presence(
    client: imaplib.IMAP4,
    uids: Sequence[int],
) -> Dict[int, bool]:
    """Check Date header presence without downloading complete messages."""
    results: Dict[int, bool] = {}
    for offset in range(0, len(uids), 200):
        batch = uids[offset : offset + 200]
        if not batch:
            continue
        typ, rows = client.uid(
            "fetch",
            ",".join(str(uid) for uid in batch),
            "(UID BODY.PEEK[HEADER.FIELDS (DATE)])",
        )
        if typ != "OK":
            raise SyncError("IMAP Date header FETCH failed")
        for row in rows or []:
            if not isinstance(row, tuple) or len(row) != 2:
                continue
            metadata, header = row
            if not isinstance(metadata, bytes) or not isinstance(header, bytes):
                continue
            uid_match = UID_RE.search(metadata)
            if uid_match:
                results[int(uid_match.group(1))] = (
                    message_from_bytes(header).get("Date") is not None
                )
    return results


def parse_internal_date(metadata: bytes) -> Optional[datetime]:
    match = INTERNALDATE_RE.search(metadata)
    if not match:
        return None
    return datetime.strptime(
        match.group(1).decode("ascii"),
        "%d-%b-%Y %H:%M:%S %z",
    )


def parse_message_date(raw: bytes) -> Optional[datetime]:
    """Return the RFC message date when a server omits IMAP INTERNALDATE."""
    value = message_from_bytes(raw).get("Date")
    if not value:
        return None
    try:
        # Compat32 can return email.header.Header for malformed or programmatically
        # constructed messages; parsedate_to_datetime requires a string.
        parsed = parsedate_to_datetime(str(value))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def source_message_date(metadata: bytes, raw: bytes) -> Optional[datetime]:
    return parse_internal_date(metadata) or parse_message_date(raw)


def is_qq_source(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].casefold()
    return domain in {"qq.com", "foxmail.com"}


def needs_qq_date_header(message: SourceMessage) -> bool:
    """Identify QQ system messages whose missing Date confuses MailPlus."""
    parsed = message_from_bytes(message.raw)
    return parsed.get("Date") is None and (
        is_qq_source(message.source_email)
        or parsed.get("X-QQ-mid") is not None
    )


def target_message_raw(message: SourceMessage) -> bytes:
    """Return the deterministic representation written to the target mailbox."""
    if message.internal_date is None or not needs_qq_date_header(message):
        return message.raw
    date_header = f"Date: {format_datetime(message.internal_date)}\r\n".encode(
        "ascii"
    )
    return date_header + message.raw


def parse_flags(metadata: bytes) -> Tuple[str, ...]:
    match = FLAGS_RE.search(metadata)
    if not match:
        return ()
    flags = [
        value.decode("ascii", "replace")
        for value in match.group(1).split()
    ]
    return tuple(sorted(flag for flag in flags if flag in SYSTEM_FLAGS))


def normalize_message_content(raw: bytes) -> bytes:
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    # Synology MailPlus rewrites NUL bytes in APPENDed messages as 0x80.
    return normalized.replace(b"\x00", b"\x80")


def normalized_content_hash(raw: bytes) -> str:
    return hashlib.sha256(normalize_message_content(raw)).hexdigest()


def content_hash_matches(
    raw: bytes,
    expected_hash: str,
    expected_raw: Optional[bytes] = None,
) -> bool:
    """Compare message content while tolerating IMAP CRLF normalization."""
    if hashlib.sha256(raw).hexdigest() == expected_hash:
        return True
    if expected_raw is not None:
        return normalize_message_content(raw) == normalize_message_content(expected_raw)
    return normalized_content_hash(raw) == expected_hash


def pop_retrieve_raw(client: poplib.POP3, message_number: int) -> bytes:
    """Stream one POP message into a compact bytes buffer."""
    client._putcmd(f"RETR {message_number}")
    client._getresp()
    output = io.BytesIO()
    while True:
        line, _ = client._getline()
        if line == b".":
            break
        if line.startswith(b".."):
            line = line[1:]
        output.write(line)
        output.write(b"\r\n")
    return output.getvalue()


def fetch_pop_message_by_identity(
    profile: dict,
    message_id: str,
    expected_size: int,
) -> bytes:
    settings = profile.get("pop_fallback")
    if not isinstance(settings, dict):
        raise SyncError(f"POP fallback is not configured for {profile['email']}")
    if not profile.get("password"):
        raise SyncError(f"POP fallback requires a password for {profile['email']}")

    host = settings.get("server") or settings.get("host")
    if not host:
        raise SyncError(f"POP fallback server is missing for {profile['email']}")
    port = int(settings.get("port", 995))
    timeout = int(settings.get("timeout", 300))
    use_ssl = bool(settings.get("use_ssl", True))
    client: poplib.POP3
    if use_ssl:
        client = poplib.POP3_SSL(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        client = poplib.POP3(host, port, timeout=timeout)

    try:
        client.user(profile["email"])
        client.pass_(profile["password"])
        _, rows, _ = client.list()
        candidates = []
        for row in rows:
            number_raw, size_raw = row.split()
            if int(size_raw) == expected_size:
                candidates.append(int(number_raw))

        for number in candidates:
            _, header_lines, _ = client.top(number, 0)
            header = b"\r\n".join(header_lines) + b"\r\n\r\n"
            candidate_id = (
                message_from_bytes(header).get("Message-ID", "").strip()
            )
            if candidate_id != message_id:
                continue
            raw = pop_retrieve_raw(client, number)
            if len(raw) != expected_size:
                raise SyncError(
                    f"POP message size changed for {profile['email']}: "
                    f"expected {expected_size}, received {len(raw)}"
                )
            return raw
    finally:
        try:
            client.quit()
        except (poplib.error_proto, OSError, EOFError):
            pass

    raise SyncError(
        f"POP fallback could not find Message-ID {message_id} "
        f"with size {expected_size}"
    )


def fetch_source_message(
    client: imaplib.IMAP4,
    source_email: str,
    folder: str,
    uidvalidity: int,
    uid: int,
    source_profile: Optional[dict] = None,
) -> SourceMessage:
    typ, rows = client.uid(
        "fetch",
        str(uid),
        "(BODY.PEEK[] INTERNALDATE FLAGS)",
    )
    if typ != "OK":
        raise SyncError(f"FETCH failed for {source_email} {folder} UID {uid}")
    try:
        metadata, raw = parse_fetch_tuple(rows or [])
    except SyncError as exc:
        if (
            folder != "INBOX"
            or source_profile is None
            or not source_profile.get("pop_fallback")
        ):
            raise SourceMessageUnavailable(
                f"Source message has no fetchable body: "
                f"{source_email} {folder} UID {uid}"
            ) from exc

        typ, header_rows = client.uid(
            "fetch",
            str(uid),
            (
                "(BODY.PEEK[HEADER.FIELDS "
                "(MESSAGE-ID SUBJECT FROM TO DATE)] "
                "RFC822.SIZE INTERNALDATE FLAGS)"
            ),
        )
        if typ != "OK":
            raise SyncError(
                f"Header FETCH failed for POP fallback: "
                f"{source_email} {folder} UID {uid}"
            )
        try:
            header_metadata, header = parse_fetch_tuple(header_rows or [])
        except SyncError as exc:
            raise SourceMessageUnavailable(
                f"Source message has no fetchable body or headers: "
                f"{source_email} {folder} UID {uid}"
            ) from exc
        parsed_header = message_from_bytes(header)
        message_id = parsed_header.get("Message-ID", "").strip()
        size_match = RFC822_SIZE_RE.search(header_metadata)
        if not message_id or not size_match:
            raise SyncError(
                f"POP fallback requires Message-ID and RFC822.SIZE: "
                f"{source_email} {folder} UID {uid}"
            )
        expected_size = int(size_match.group(1))
        raw = fetch_pop_message_by_identity(
            source_profile,
            message_id,
            expected_size,
        )
        metadata = header_metadata
        print(
            json.dumps(
                {
                    "event": "source_message_pop_fallback",
                    "source": source_email,
                    "folder": folder,
                    "uid": uid,
                    "bytes": len(raw),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    parsed = message_from_bytes(raw)
    message_id = parsed.get("Message-ID")
    return SourceMessage(
        source_email=source_email,
        folder=folder,
        uidvalidity=uidvalidity,
        uid=uid,
        raw=raw,
        content_hash=hashlib.sha256(raw).hexdigest(),
        message_id=message_id.strip() if message_id else None,
        internal_date=source_message_date(metadata, raw),
        system_flags=parse_flags(metadata),
    )


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            source_email TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            normalized_hash TEXT,
            target_normalized_hash TEXT,
            message_id TEXT,
            source_folder TEXT NOT NULL,
            source_uidvalidity INTEGER NOT NULL,
            source_uid INTEGER NOT NULL,
            source_flags TEXT NOT NULL,
            internal_date TEXT,
            source_date_verified INTEGER NOT NULL DEFAULT 0,
            target_date_header_verified INTEGER NOT NULL DEFAULT 0,
            target_uidvalidity INTEGER NOT NULL,
            target_uid INTEGER NOT NULL,
            account_label TEXT NOT NULL,
            folder_label TEXT NOT NULL,
            folder_labels_json TEXT,
            last_seen_run TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_email, content_hash)
        );
        DROP INDEX IF EXISTS messages_source_location;
        CREATE INDEX IF NOT EXISTS messages_source_location
        ON messages(source_email, source_folder, source_uidvalidity, source_uid);
        CREATE TABLE IF NOT EXISTS pending_operations (
            source_email TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            message_id TEXT,
            source_folder TEXT NOT NULL,
            source_uidvalidity INTEGER NOT NULL,
            source_uid INTEGER NOT NULL,
            source_flags TEXT NOT NULL,
            internal_date TEXT,
            raw_message BLOB NOT NULL,
            account_label TEXT NOT NULL,
            folder_label TEXT NOT NULL,
            folder_labels_json TEXT,
            old_target_uid INTEGER,
            target_uidvalidity INTEGER NOT NULL,
            target_uid_before INTEGER NOT NULL,
            new_target_uid INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_email, content_hash)
        );
        CREATE TABLE IF NOT EXISTS message_locations (
            source_email TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source_folder TEXT NOT NULL,
            source_uidvalidity INTEGER NOT NULL,
            source_uid INTEGER NOT NULL,
            folder_label TEXT NOT NULL,
            last_seen_run TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (
                source_email, source_folder, source_uidvalidity, source_uid
            ),
            FOREIGN KEY (source_email, content_hash)
            REFERENCES messages(source_email, content_hash)
            ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS message_locations_content
        ON message_locations(source_email, content_hash);
        """
    )
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(messages)").fetchall()
    }
    if "message_id" not in columns:
        connection.execute("ALTER TABLE messages ADD COLUMN message_id TEXT")
        connection.commit()
    if "normalized_hash" not in columns:
        connection.execute("ALTER TABLE messages ADD COLUMN normalized_hash TEXT")
    if "target_normalized_hash" not in columns:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN target_normalized_hash TEXT"
        )
    if "folder_labels_json" not in columns:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN folder_labels_json TEXT"
        )
    if "source_date_verified" not in columns:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN "
            "source_date_verified INTEGER NOT NULL DEFAULT 0"
        )
    if "target_date_header_verified" not in columns:
        # Existing non-QQ copies need no header migration. Existing QQ and
        # Foxmail copies are audited once by repair-dates.
        connection.execute(
            "ALTER TABLE messages ADD COLUMN "
            "target_date_header_verified INTEGER NOT NULL DEFAULT 1"
        )
        connection.execute(
            """
            UPDATE messages SET target_date_header_verified=0
            WHERE lower(source_email) LIKE '%@qq.com'
               OR lower(source_email) LIKE '%@foxmail.com'
            """
        )
    pending_columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(pending_operations)"
        ).fetchall()
    }
    if "status" not in pending_columns:
        connection.execute(
            "ALTER TABLE pending_operations "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
        )
    if "last_error" not in pending_columns:
        connection.execute(
            "ALTER TABLE pending_operations ADD COLUMN last_error TEXT"
        )
    if "folder_labels_json" not in pending_columns:
        connection.execute(
            "ALTER TABLE pending_operations ADD COLUMN folder_labels_json TEXT"
        )
    for row in connection.execute(
        """
        SELECT source_email, content_hash, folder_label
        FROM messages
        WHERE folder_labels_json IS NULL
        """
    ).fetchall():
        connection.execute(
            """
            UPDATE messages SET folder_labels_json=?
            WHERE source_email=? AND content_hash=?
            """,
            (
                json.dumps([row["folder_label"]], ensure_ascii=False),
                row["source_email"],
                row["content_hash"],
            ),
        )
    for row in connection.execute(
        """
        SELECT source_email, content_hash, folder_label
        FROM pending_operations
        WHERE folder_labels_json IS NULL
        """
    ).fetchall():
        connection.execute(
            """
            UPDATE pending_operations SET folder_labels_json=?
            WHERE source_email=? AND content_hash=?
            """,
            (
                json.dumps([row["folder_label"]], ensure_ascii=False),
                row["source_email"],
                row["content_hash"],
            ),
        )
    connection.execute(
        """
        INSERT OR IGNORE INTO message_locations (
            source_email, content_hash, source_folder, source_uidvalidity,
            source_uid, folder_label, last_seen_run, updated_at
        )
        SELECT source_email, content_hash, source_folder, source_uidvalidity,
               source_uid, folder_label, last_seen_run, updated_at
        FROM messages
        WHERE source_folder <> '__deleted__'
        """
    )
    connection.commit()
    return connection


@contextmanager
def process_lock(state_path: Path):
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncError(
                f"Another synchronizer is already running ({lock_path})"
            ) from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_mailbox(
    client: imaplib.IMAP4,
    folder: str,
    readonly: bool,
) -> Tuple[int, int]:
    typ, data = client.select(quote_mailbox(folder), readonly=readonly)
    if typ != "OK":
        raise SyncError(f"Cannot select mailbox {folder}")
    count = int(data[0]) if data and data[0] else 0
    return count, response_int(client, "UIDVALIDITY")


def search_uids(client: imaplib.IMAP4, criteria: str = "ALL") -> List[int]:
    typ, data = client.uid("search", None, criteria)
    if typ != "OK":
        raise SyncError(f"UID SEARCH {criteria} failed")
    return [int(value) for value in (data[0].split() if data and data[0] else [])]


def label_id_map(raw: dict) -> Dict[str, str]:
    inverse: Dict[str, str] = {}
    for internal_id, display_name in raw.items():
        if display_name in inverse:
            raise SyncError(f"Duplicate label name in mapping: {display_name}")
        inverse[display_name] = internal_id
    return inverse


def source_folder_label(source_rule: dict, folder: FolderInfo) -> str:
    mapping = source_rule.get("folder_labels", {})
    return (
        mapping.get(folder.raw_name)
        or mapping.get(folder.display_name)
        or source_rule["default_folder_label"]
    )


def source_folder_skipped_uids(
    source_rule: dict,
    folder: FolderInfo,
) -> Set[int]:
    configured = source_rule.get("skip_uids", {})
    if not isinstance(configured, dict):
        raise SyncError("skip_uids must be an object keyed by source folder")
    values = configured.get(
        folder.raw_name,
        configured.get(folder.display_name, []),
    )
    if not isinstance(values, list):
        raise SyncError(
            f"skip_uids for {folder.display_name} must be an array"
        )
    try:
        return {int(value) for value in values}
    except (TypeError, ValueError) as exc:
        raise SyncError(
            f"skip_uids for {folder.display_name} must contain integers"
        ) from exc


def is_deleted_draft_message(
    source_rule: dict,
    folder: FolderInfo,
    message: SourceMessage,
) -> bool:
    """Identify unsent drafts that Outlook moves into its Deleted folder."""
    if not source_rule.get("skip_deleted_drafts", False):
        return False
    if source_folder_label(source_rule, folder) != source_rule.get("deleted_label"):
        return False
    if r"\Draft" in message.system_flags:
        return True
    parsed = message_from_bytes(message.raw)
    return not any(
        (
            parsed.get("Message-ID"),
            parsed.get("Date"),
            parsed.get_all("Received"),
        )
    )


def purge_single_location_message(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    existing: sqlite3.Row,
) -> bool:
    """Remove an already-synced message when its sole source location is skipped."""
    location_count = db.execute(
        """
        SELECT COUNT(*) FROM message_locations
        WHERE source_email=? AND content_hash=?
        """,
        (existing["source_email"], existing["content_hash"]),
    ).fetchone()[0]
    if location_count != 1:
        return False
    delete_target_copy(target, target_folder, int(existing["target_uid"]))
    db.execute(
        """
        DELETE FROM message_locations
        WHERE source_email=? AND content_hash=?
        """,
        (existing["source_email"], existing["content_hash"]),
    )
    db.execute(
        """
        DELETE FROM messages
        WHERE source_email=? AND content_hash=?
        """,
        (existing["source_email"], existing["content_hash"]),
    )
    db.commit()
    return True


def format_flag_list(flags: Iterable[str]) -> str:
    return "(" + " ".join(sorted(set(flags))) + ")"


def target_fetch(
    client: imaplib.IMAP4,
    uid: int,
    include_body: bool = False,
) -> Tuple[bytes, Optional[bytes]]:
    query = "(FLAGS BODY.PEEK[])" if include_body else "(FLAGS)"
    typ, rows = client.uid("fetch", str(uid), query)
    if typ != "OK":
        raise SyncError(f"Target FETCH failed for UID {uid}")
    if include_body:
        metadata, raw = parse_fetch_tuple(rows or [])
        return metadata, raw
    for row in rows or []:
        if isinstance(row, bytes) and UID_RE.search(row):
            return row, None
    raise SyncError(f"Target UID {uid} does not exist")


def append_target_copy(
    client: imaplib.IMAP4,
    target_folder: str,
    message: SourceMessage,
    label_ids: Sequence[str],
) -> Tuple[int, int]:
    _, uidvalidity = select_mailbox(client, target_folder, readonly=False)
    first_possible_uid = response_int(client, "UIDNEXT")
    system_flags = format_flag_list(message.system_flags)
    target_raw = target_message_raw(message)
    typ, response = client.append(
        quote_mailbox(target_folder),
        system_flags,
        message.internal_date,
        target_raw,
    )
    if typ != "OK":
        raise SyncError(f"APPEND failed: {response!r}")
    response_bytes = b" ".join(
        value for value in (response or []) if isinstance(value, bytes)
    )
    append_uid = APPENDUID_RE.search(response_bytes)
    if append_uid:
        appended_uidvalidity = int(append_uid.group(1))
        if appended_uidvalidity != uidvalidity:
            raise SyncError(
                f"APPENDUID UIDVALIDITY changed: expected {uidvalidity}, "
                f"received {appended_uidvalidity}"
            )
        created = [int(append_uid.group(2))]
    else:
        created = search_uids(client, f"UID {first_possible_uid}:*")
    if len(created) != 1:
        raise SyncError(f"Expected one appended UID, found {created}")
    uid = created[0]
    for label_id in label_ids:
        typ, response = client.uid(
            "store",
            str(uid),
            "+FLAGS.SILENT",
            f"({label_id})",
        )
        if typ != "OK":
            raise SyncError(f"Failed adding {label_id} to target UID {uid}: {response!r}")
    metadata, raw = target_fetch(client, uid, include_body=True)
    if raw is None or not content_hash_matches(
        raw,
        hashlib.sha256(target_raw).hexdigest(),
        target_raw,
    ):
        raise TargetContentVerificationError(uid)
    current_flags = set(
        value.decode("ascii", "replace")
        for value in (FLAGS_RE.search(metadata).group(1).split() if FLAGS_RE.search(metadata) else [])
    )
    missing = set(label_ids) - current_flags
    if missing:
        raise SyncError(f"Target UID {uid} is missing labels: {sorted(missing)}")
    return uidvalidity, uid


def ensure_no_preexisting_deleted(client: imaplib.IMAP4, old_uid: int) -> None:
    deleted = set(search_uids(client, "DELETED"))
    unexpected = deleted - {old_uid}
    if unexpected:
        raise SyncError(
            "Target mailbox contains unrelated messages already marked Deleted; "
            f"refusing broad EXPUNGE: {sorted(unexpected)}"
        )


def delete_target_copy(
    client: imaplib.IMAP4,
    target_folder: str,
    old_uid: int,
) -> None:
    select_mailbox(client, target_folder, readonly=False)
    ensure_no_preexisting_deleted(client, old_uid)
    if old_uid not in set(search_uids(client, f"UID {old_uid}")):
        return
    typ, response = client.uid(
        "store",
        str(old_uid),
        "+FLAGS.SILENT",
        r"(\Deleted)",
    )
    if typ != "OK":
        raise SyncError(f"Could not mark target UID {old_uid} deleted: {response!r}")
    typ, response = client.expunge()
    if typ != "OK":
        raise SyncError(f"EXPUNGE failed: {response!r}")


def find_untracked_target_by_message_id(
    db: sqlite3.Connection,
    client: imaplib.IMAP4,
    target_folder: str,
    message_id: Optional[str],
) -> Optional[int]:
    if not message_id:
        return None
    select_mailbox(client, target_folder, readonly=False)
    typ, data = client.uid(
        "search",
        None,
        "HEADER",
        "Message-ID",
        quote_imap_string(message_id),
    )
    if typ != "OK":
        raise SyncError(f"Target Message-ID search failed for {message_id}")
    candidates = [
        int(value)
        for value in (data[0].split() if data and data[0] else [])
    ]
    for uid in candidates:
        tracked = db.execute(
            "SELECT 1 FROM messages WHERE target_uid=?",
            (uid,),
        ).fetchone()
        if tracked is None:
            return uid
    return None


def normalize_folder_labels(values: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    return tuple(sorted(set(values)))


def row_folder_labels(row: sqlite3.Row) -> Tuple[str, ...]:
    raw = row["folder_labels_json"]
    if raw:
        return normalize_folder_labels(json.loads(raw))
    return (row["folder_label"],)


def begin_pending_operation(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    message: SourceMessage,
    account_label: str,
    folder_labels: Sequence[str],
    old_target_uid: Optional[int],
    location_folder_label: Optional[str] = None,
) -> None:
    normalized_labels = normalize_folder_labels(folder_labels)
    if not normalized_labels:
        raise SyncError("At least one folder label is required")
    primary_folder_label = location_folder_label or normalized_labels[0]
    if primary_folder_label not in normalized_labels:
        raise SyncError(
            f"Location label {primary_folder_label} is not in desired labels"
        )
    _, target_uidvalidity = select_mailbox(target, target_folder, readonly=False)
    target_uid_before = max(0, response_int(target, "UIDNEXT") - 1)
    timestamp = now_iso()
    db.execute(
        """
        INSERT INTO pending_operations (
            source_email, content_hash, message_id, source_folder,
            source_uidvalidity, source_uid, source_flags, internal_date,
            raw_message, account_label, folder_label, folder_labels_json,
            old_target_uid,
            target_uidvalidity, target_uid_before, new_target_uid,
            status, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
                  'pending', NULL, ?, ?)
        ON CONFLICT(source_email, content_hash) DO UPDATE SET
            message_id=excluded.message_id,
            source_folder=excluded.source_folder,
            source_uidvalidity=excluded.source_uidvalidity,
            source_uid=excluded.source_uid,
            source_flags=excluded.source_flags,
            internal_date=excluded.internal_date,
            raw_message=excluded.raw_message,
            account_label=excluded.account_label,
            folder_label=excluded.folder_label,
            folder_labels_json=excluded.folder_labels_json,
            old_target_uid=excluded.old_target_uid,
            target_uidvalidity=excluded.target_uidvalidity,
            target_uid_before=excluded.target_uid_before,
            new_target_uid=NULL,
            status='pending',
            last_error=NULL,
            updated_at=excluded.updated_at
        """,
        (
            message.source_email,
            message.content_hash,
            message.message_id,
            message.folder,
            message.uidvalidity,
            message.uid,
            json.dumps(message.system_flags),
            message.internal_date.isoformat() if message.internal_date else None,
            message.raw,
            account_label,
            primary_folder_label,
            json.dumps(normalized_labels, ensure_ascii=False),
            old_target_uid,
            target_uidvalidity,
            target_uid_before,
            timestamp,
            timestamp,
        ),
    )
    db.commit()


def update_pending_target_uid(
    db: sqlite3.Connection,
    message: SourceMessage,
    target_uid: int,
) -> None:
    db.execute(
        """
        UPDATE pending_operations
        SET new_target_uid=?, updated_at=?
        WHERE source_email=? AND content_hash=?
        """,
        (
            target_uid,
            now_iso(),
            message.source_email,
            message.content_hash,
        ),
    )
    db.commit()


def quarantine_pending_operation(
    db: sqlite3.Connection,
    message: SourceMessage,
    target_uid: int,
    error: str,
) -> None:
    db.execute(
        """
        UPDATE pending_operations
        SET new_target_uid=?, status='quarantined', last_error=?, updated_at=?
        WHERE source_email=? AND content_hash=?
        """,
        (
            target_uid,
            error,
            now_iso(),
            message.source_email,
            message.content_hash,
        ),
    )
    db.commit()


def metadata_flags(metadata: bytes) -> Set[str]:
    match = FLAGS_RE.search(metadata)
    if not match:
        return set()
    return {
        value.decode("ascii", "replace")
        for value in match.group(1).split()
    }


def target_copy_matches(
    client: imaplib.IMAP4,
    uid: int,
    message: SourceMessage,
    label_ids: Sequence[str],
) -> bool:
    try:
        metadata, raw = target_fetch(client, uid, include_body=True)
    except SyncError:
        return False
    expected_raw = target_message_raw(message)
    return (
        raw is not None
        and content_hash_matches(
            raw,
            hashlib.sha256(expected_raw).hexdigest(),
            expected_raw,
        )
        and set(label_ids).issubset(metadata_flags(metadata))
    )


def repair_target_copy(
    client: imaplib.IMAP4,
    uid: int,
    message: SourceMessage,
    label_ids: Sequence[str],
) -> bool:
    try:
        metadata, raw = target_fetch(client, uid, include_body=True)
    except SyncError:
        return False
    expected_raw = target_message_raw(message)
    if raw is None or not content_hash_matches(
        raw,
        hashlib.sha256(expected_raw).hexdigest(),
        expected_raw,
    ):
        return False
    missing = set(label_ids) - metadata_flags(metadata)
    for label_id in sorted(missing):
        typ, response = client.uid(
            "store",
            str(uid),
            "+FLAGS.SILENT",
            f"({label_id})",
        )
        if typ != "OK":
            raise SyncError(
                f"Failed repairing {label_id} on target UID {uid}: {response!r}"
            )
    return target_copy_matches(
        client,
        uid,
        message,
        label_ids,
    )


def pending_row_message(row: sqlite3.Row) -> SourceMessage:
    raw = bytes(row["raw_message"])
    return SourceMessage(
        source_email=row["source_email"],
        folder=row["source_folder"],
        uidvalidity=int(row["source_uidvalidity"]),
        uid=int(row["source_uid"]),
        raw=raw,
        content_hash=row["content_hash"],
        message_id=row["message_id"],
        internal_date=(
            datetime.fromisoformat(row["internal_date"])
            if row["internal_date"]
            else None
        ),
        system_flags=tuple(json.loads(row["source_flags"])),
    )


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


def replace_target_copy(
    client: imaplib.IMAP4,
    target_folder: str,
    message: SourceMessage,
    label_ids: Sequence[str],
    old_uid: int,
) -> Tuple[int, int]:
    select_mailbox(client, target_folder, readonly=False)
    ensure_no_preexisting_deleted(client, old_uid)
    target_uidvalidity, new_uid = append_target_copy(
        client,
        target_folder,
        message,
        label_ids,
    )
    try:
        delete_target_copy(client, target_folder, old_uid)
    except Exception:
        # Keep the verified new copy. The state is not updated, so a later run
        # will not silently forget the old UID and can report the duplicate.
        raise
    return target_uidvalidity, new_uid


def upsert_state(
    db: sqlite3.Connection,
    message: SourceMessage,
    target_uidvalidity: int,
    target_uid: int,
    account_label: str,
    folder_labels: Sequence[str],
    run_id: str,
) -> None:
    normalized_labels = normalize_folder_labels(folder_labels)
    if not normalized_labels:
        raise SyncError("At least one folder label is required")
    primary_folder_label = normalized_labels[0]
    target_raw = target_message_raw(message)
    existing = db.execute(
        """
        SELECT normalized_hash FROM messages
        WHERE source_email=? AND content_hash=?
        """,
        (message.source_email, message.content_hash),
    ).fetchone()
    if content_hash_matches(message.raw, message.content_hash):
        source_normalized_hash = normalized_content_hash(message.raw)
    elif existing is not None and existing["normalized_hash"]:
        # Reconciliation may reconstruct SourceMessage from an already
        # normalized target copy rather than the original source bytes.
        source_normalized_hash = existing["normalized_hash"]
    else:
        source_normalized_hash = None
    db.execute(
        """
        INSERT INTO messages (
            source_email, content_hash, normalized_hash, target_normalized_hash,
            message_id, source_folder,
            source_uidvalidity, source_uid, source_flags, internal_date,
            source_date_verified, target_date_header_verified,
            target_uidvalidity, target_uid, account_label, folder_label,
            folder_labels_json, last_seen_run, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_email, content_hash) DO UPDATE SET
            normalized_hash=excluded.normalized_hash,
            target_normalized_hash=excluded.target_normalized_hash,
            message_id=excluded.message_id,
            source_folder=excluded.source_folder,
            source_uidvalidity=excluded.source_uidvalidity,
            source_uid=excluded.source_uid,
            source_flags=excluded.source_flags,
            internal_date=excluded.internal_date,
            source_date_verified=1,
            target_date_header_verified=1,
            target_uidvalidity=excluded.target_uidvalidity,
            target_uid=excluded.target_uid,
            account_label=excluded.account_label,
            folder_label=excluded.folder_label,
            folder_labels_json=excluded.folder_labels_json,
            last_seen_run=excluded.last_seen_run,
            updated_at=excluded.updated_at
        """,
        (
            message.source_email,
            message.content_hash,
            source_normalized_hash,
            normalized_content_hash(target_raw),
            message.message_id,
            message.folder,
            message.uidvalidity,
            message.uid,
            json.dumps(message.system_flags),
            message.internal_date.isoformat() if message.internal_date else None,
            target_uidvalidity,
            target_uid,
            account_label,
            primary_folder_label,
            json.dumps(normalized_labels, ensure_ascii=False),
            run_id,
            now_iso(),
        ),
    )


def upsert_message_location(
    db: sqlite3.Connection,
    message: SourceMessage,
    folder_label: str,
    run_id: str,
) -> None:
    db.execute(
        """
        INSERT INTO message_locations (
            source_email, content_hash, source_folder, source_uidvalidity,
            source_uid, folder_label, last_seen_run, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (
            source_email, source_folder, source_uidvalidity, source_uid
        ) DO UPDATE SET
            content_hash=excluded.content_hash,
            folder_label=excluded.folder_label,
            last_seen_run=excluded.last_seen_run,
            updated_at=excluded.updated_at
        """,
        (
            message.source_email,
            message.content_hash,
            message.folder,
            message.uidvalidity,
            message.uid,
            folder_label,
            run_id,
            now_iso(),
        ),
    )
    db.execute(
        """
        UPDATE messages SET last_seen_run=?, updated_at=?
        WHERE source_email=? AND content_hash=?
        """,
        (
            run_id,
            now_iso(),
            message.source_email,
            message.content_hash,
        ),
    )


def find_by_location(
    db: sqlite3.Connection,
    source_email: str,
    folder: str,
    uidvalidity: int,
    uid: int,
) -> Optional[sqlite3.Row]:
    return db.execute(
        """
        SELECT m.*, l.folder_label AS location_folder_label
        FROM message_locations l
        JOIN messages m
          ON m.source_email=l.source_email
         AND m.content_hash=l.content_hash
        WHERE l.source_email=? AND l.source_folder=?
          AND l.source_uidvalidity=? AND l.source_uid=?
        """,
        (source_email, folder, uidvalidity, uid),
    ).fetchone()


def sync_message(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    message: SourceMessage,
    account_label_name: str,
    folder_label_name: str,
    labels_by_name: Dict[str, str],
    run_id: str,
    force_rebuild: bool = False,
) -> str:
    try:
        account_label_id = labels_by_name[account_label_name]
        folder_label_id = labels_by_name[folder_label_name]
    except KeyError as exc:
        raise SyncError(f"Undefined Synology label: {exc.args[0]}") from exc

    quarantined = db.execute(
        """
        SELECT 1 FROM pending_operations
        WHERE source_email=? AND content_hash=? AND status='quarantined'
        """,
        (message.source_email, message.content_hash),
    ).fetchone()
    if quarantined is not None:
        return "quarantined"

    existing = db.execute(
        "SELECT * FROM messages WHERE source_email=? AND content_hash=?",
        (message.source_email, message.content_hash),
    ).fetchone()
    old_target_uid: Optional[int] = None
    desired_folder_labels = (folder_label_name,)
    if existing is None:
        prior_target_uid = find_untracked_target_by_message_id(
            db,
            target,
            target_folder,
            message.message_id,
        )
        if prior_target_uid is None:
            action = "created"
        else:
            old_target_uid = prior_target_uid
            action = "existing_rebuilt"
    else:
        existing_folder_labels = row_folder_labels(existing)
        stored_internal_date = (
            datetime.fromisoformat(existing["internal_date"])
            if existing["internal_date"]
            else None
        )
        if (
            existing["source_folder"] == "__deleted__"
            or existing["account_label"] != account_label_name
        ):
            old_target_uid = int(existing["target_uid"])
            action = "rebuilt"
        elif force_rebuild or stored_internal_date != message.internal_date:
            # Some IMAP servers omit INTERNALDATE. Older versions then APPENDed
            # with no date, causing the target to assign the current year.
            old_target_uid = int(existing["target_uid"])
            desired_folder_labels = existing_folder_labels
            action = "date_rebuilt"
        elif folder_label_name not in existing_folder_labels:
            target_uid = int(existing["target_uid"])
            select_mailbox(target, target_folder, readonly=False)
            typ, response = target.uid(
                "store",
                str(target_uid),
                "+FLAGS.SILENT",
                f"({folder_label_id})",
            )
            if typ != "OK":
                raise SyncError(
                    f"Failed adding {folder_label_id} to target UID "
                    f"{target_uid}: {response!r}"
                )
            desired_folder_labels = normalize_folder_labels(
                (*existing_folder_labels, folder_label_name)
            )
            upsert_state(
                db,
                message,
                int(existing["target_uidvalidity"]),
                target_uid,
                account_label_name,
                desired_folder_labels,
                run_id,
            )
            upsert_message_location(
                db,
                message,
                folder_label_name,
                run_id,
            )
            db.commit()
            return "folder_label_added"
        else:
            upsert_state(
                db,
                message,
                int(existing["target_uidvalidity"]),
                int(existing["target_uid"]),
                account_label_name,
                existing_folder_labels,
                run_id,
            )
            upsert_message_location(
                db,
                message,
                folder_label_name,
                run_id,
            )
            db.commit()
            return "source_location_added"

    label_ids = [
        account_label_id,
        *(labels_by_name[name] for name in desired_folder_labels),
    ]
    if old_target_uid is not None:
        select_mailbox(target, target_folder, readonly=False)
        ensure_no_preexisting_deleted(target, old_target_uid)
    begin_pending_operation(
        db,
        target,
        target_folder,
        message,
        account_label_name,
        desired_folder_labels,
        old_target_uid,
        folder_label_name,
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
    if old_target_uid is not None:
        delete_target_copy(target, target_folder, old_target_uid)
    upsert_state(
        db,
        message,
        target_uidvalidity,
        target_uid,
        account_label_name,
        desired_folder_labels,
        run_id,
    )
    upsert_message_location(
        db,
        message,
        folder_label_name,
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
    return action


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
        ensure_no_preexisting_deleted(target, old_target_uid)
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


def source_rule_for(config: dict, email: str) -> dict:
    for rule in config.get("sources", []):
        if rule.get("email") == email:
            return rule
    raise SyncError(f"No source rule for {email}")


def selected_source_rules(config: dict, email: Optional[str]) -> List[dict]:
    rules = config.get("sources", [])
    if email:
        return [source_rule_for(config, email)]
    return list(rules)


def command_authorize_outlook(
    config: dict,
    profiles: Dict[str, dict],
    source_email: str,
) -> int:
    source_rule_for(config, source_email)
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
            print(
                json.dumps(
                    {
                        "event": "outlook_authorization_succeeded",
                        "source": source_email,
                        "token_file": auth["token_file"],
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


def command_list_folders(
    config: dict,
    profiles: Dict[str, dict],
    source_email: Optional[str],
) -> int:
    for rule in selected_source_rules(config, source_email):
        email = rule["email"]
        client = connect(profiles[email])
        try:
            folders = list_folders(client)
            selected = {item.raw_name for item in selectable_source_folders(folders, rule)}
            print(f"{email}:")
            for folder in folders:
                status = "sync" if folder.raw_name in selected else "excluded"
                attrs = " ".join(folder.attributes) or "-"
                print(
                    f"  [{status:8}] {folder.display_name} "
                    f"(raw={folder.raw_name!r}, attrs={attrs})"
                )
        finally:
            safe_logout(client)
    return 0


def command_sync(
    config: dict,
    profiles: Dict[str, dict],
    labels_by_name: Dict[str, str],
    state_path: Path,
    source_email: Optional[str],
    only_folder: Optional[str],
    limit: Optional[int],
    newest_first: bool,
) -> int:
    db = init_db(state_path)
    target_rule = config["target"]
    target_profile = profiles[TARGET_PROFILE_KEY]
    target = connect(target_profile)
    total_scanned = 0
    total_changed = 0
    run_id = uuid.uuid4().hex
    all_scans_complete = True
    reconciled_sources = 0
    summary: Dict[str, int] = {}
    try:
        recovered = recover_pending_operations(
            db,
            target,
            target_rule["folder"],
            labels_by_name,
        )
        if recovered:
            summary["pending_operation_recovered"] = recovered
        for rule in selected_source_rules(config, source_email):
            email = rule["email"]
            source_changed = 0
            source_scan_complete = True
            source = connect(profiles[email])
            try:
                folders = selectable_source_folders(list_folders(source), rule)
                if only_folder:
                    folders = [
                        folder
                        for folder in folders
                        if only_folder in (folder.raw_name, folder.display_name)
                    ]
                    if not folders:
                        raise SyncError(f"Source folder not found or excluded: {only_folder}")

                for folder in folders:
                    _, uidvalidity = select_mailbox(
                        source,
                        folder.raw_name,
                        readonly=True,
                    )
                    uids = search_uids(source)
                    skipped_uids = source_folder_skipped_uids(rule, folder)
                    inspect_deleted_drafts = (
                        rule.get("skip_deleted_drafts", False)
                        and source_folder_label(rule, folder)
                        == rule.get("deleted_label")
                    )
                    if newest_first:
                        uids.reverse()
                    for uid in uids:
                        known = find_by_location(
                            db,
                            email,
                            folder.raw_name,
                            uidvalidity,
                            uid,
                        )
                        if (
                            known is not None
                            and known["internal_date"] is not None
                            and known["source_date_verified"]
                            and not inspect_deleted_drafts
                        ):
                            db.execute(
                                """
                                UPDATE message_locations
                                SET last_seen_run=?, updated_at=?
                                WHERE source_email=? AND source_folder=?
                                  AND source_uidvalidity=? AND source_uid=?
                                """,
                                (
                                    run_id,
                                    now_iso(),
                                    email,
                                    folder.raw_name,
                                    uidvalidity,
                                    uid,
                                ),
                            )
                            db.execute(
                                """
                                UPDATE messages SET last_seen_run=?, updated_at=?
                                WHERE source_email=? AND content_hash=?
                                """,
                                (
                                    run_id,
                                    now_iso(),
                                    email,
                                    known["content_hash"],
                                ),
                            )
                            action = "unchanged"
                        else:
                            if known is None and uid in skipped_uids:
                                action = "source_message_configured_skip"
                                summary[action] = summary.get(action, 0) + 1
                                total_scanned += 1
                                print(
                                    json.dumps(
                                        {
                                            "event": action,
                                            "source": email,
                                            "folder": folder.display_name,
                                            "uid": uid,
                                        },
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                                continue
                            if limit is not None and source_changed >= limit:
                                source_scan_complete = False
                                break
                            try:
                                message = fetch_source_message(
                                    source,
                                    email,
                                    folder.raw_name,
                                    uidvalidity,
                                    uid,
                                    profiles[email],
                                )
                            except SourceMessageUnavailable as exc:
                                action = "source_message_skipped"
                                summary[action] = summary.get(action, 0) + 1
                                total_scanned += 1
                                print(
                                    json.dumps(
                                        {
                                            "event": action,
                                            "source": email,
                                            "folder": folder.display_name,
                                            "uid": uid,
                                            "reason": str(exc),
                                        },
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                                continue
                            if is_deleted_draft_message(rule, folder, message):
                                purged = (
                                    known is not None
                                    and purge_single_location_message(
                                        db,
                                        target,
                                        target_rule["folder"],
                                        known,
                                    )
                                )
                                action = (
                                    "source_deleted_draft_purged"
                                    if purged
                                    else "source_deleted_draft_skipped"
                                )
                                if purged:
                                    source_changed += 1
                                    total_changed += 1
                                summary[action] = summary.get(action, 0) + 1
                                total_scanned += 1
                                print(
                                    json.dumps(
                                        {
                                            "event": action,
                                            "source": email,
                                            "folder": folder.display_name,
                                            "uid": uid,
                                        },
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                                continue
                            action = sync_message(
                                db,
                                target,
                                target_rule["folder"],
                                message,
                                rule["account_label"],
                                source_folder_label(rule, folder),
                                labels_by_name,
                                run_id,
                            )
                            if action not in NO_CHANGE_ACTIONS:
                                source_changed += 1
                                total_changed += 1
                                source = ensure_source_connection(
                                    profiles[email],
                                    source,
                                    folder.raw_name,
                                    uidvalidity,
                                )
                        summary[action] = summary.get(action, 0) + 1
                        total_scanned += 1
                        if action not in NO_CHANGE_ACTIONS:
                            print(
                                json.dumps(
                                    {
                                        "action": action,
                                        "source": email,
                                        "folder": folder.display_name,
                                        "uid": uid,
                                    },
                                    ensure_ascii=False,
                                )
                            )
                    db.commit()
                    if not source_scan_complete:
                        break

                if only_folder is None and source_scan_complete:
                    rebuilt_deleted = reconcile_deleted_messages(
                        db,
                        target,
                        target_rule["folder"],
                        rule,
                        labels_by_name,
                        run_id,
                    )
                    if rebuilt_deleted:
                        summary["source_deleted_rebuilt"] = (
                            summary.get("source_deleted_rebuilt", 0)
                            + rebuilt_deleted
                        )
                    reconciled_sources += 1
                else:
                    all_scans_complete = False
            finally:
                safe_logout(source)

        print(
            json.dumps(
                {
                    "scanned": total_scanned,
                    "changed": total_changed,
                    "summary": summary,
                    "reconciliation": (
                        "completed"
                        if all_scans_complete and only_folder is None
                        else "deferred_until_complete_scan"
                    ),
                    "reconciled_sources": reconciled_sources,
                    "state_db": str(state_path),
                },
                ensure_ascii=False,
            )
        )
    finally:
        try:
            safe_logout(target)
        finally:
            db.close()
    return 0


def command_repair_dates(
    config: dict,
    profiles: Dict[str, dict],
    labels_by_name: Dict[str, str],
    state_path: Path,
    source_email: Optional[str],
) -> int:
    """Verify stored dates against source INTERNALDATE and rebuild mismatches."""
    db = init_db(state_path)
    target_rule = config["target"]
    target = connect(profiles[TARGET_PROFILE_KEY])
    run_id = uuid.uuid4().hex
    totals = {
        "verified": 0,
        "date_rebuilt": 0,
        "qq_header_rebuilt": 0,
        "unresolved": 0,
    }
    try:
        recover_pending_operations(
            db,
            target,
            target_rule["folder"],
            labels_by_name,
        )
        for rule in selected_source_rules(config, source_email):
            email = rule["email"]
            source = connect(profiles[email])
            account_verified = 0
            account_rebuilt = 0
            try:
                folders = {
                    folder.raw_name: folder
                    for folder in selectable_source_folders(
                        list_folders(source),
                        rule,
                    )
                }
                rows = db.execute(
                    """
                    SELECT * FROM messages
                    WHERE source_email=?
                      AND (
                          source_date_verified=0
                          OR target_date_header_verified=0
                      )
                      AND source_folder<>'__deleted__'
                    ORDER BY source_folder, source_uidvalidity, source_uid
                    """,
                    (email,),
                ).fetchall()
                select_mailbox(
                    target,
                    target_rule["folder"],
                    readonly=True,
                )
                target_metadata = fetch_message_metadata(
                    target,
                    [int(row["target_uid"]) for row in rows],
                )
                target_date_headers = fetch_date_header_presence(
                    target,
                    [int(row["target_uid"]) for row in rows],
                )
                rows_by_folder: Dict[Tuple[str, int], List[sqlite3.Row]] = {}
                for row in rows:
                    key = (
                        row["source_folder"],
                        int(row["source_uidvalidity"]),
                    )
                    rows_by_folder.setdefault(key, []).append(row)

                for (folder_name, expected_uidvalidity), folder_rows in (
                    rows_by_folder.items()
                ):
                    folder = folders.get(folder_name)
                    if folder is None:
                        totals["unresolved"] += len(folder_rows)
                        continue
                    _, uidvalidity = select_mailbox(
                        source,
                        folder_name,
                        readonly=True,
                    )
                    if uidvalidity != expected_uidvalidity:
                        raise SyncError(
                            f"Source UIDVALIDITY changed during date repair: "
                            f"{email} {folder_name}"
                        )
                    metadata = fetch_message_metadata(
                        source,
                        [int(row["source_uid"]) for row in folder_rows],
                    )
                    for row in folder_rows:
                        uid = int(row["source_uid"])
                        current = metadata.get(uid)
                        if current is None:
                            totals["unresolved"] += 1
                            continue
                        source_date, _ = current
                        target_date = target_metadata.get(
                            int(row["target_uid"]),
                            (None, ()),
                        )[0]
                        stored_date = (
                            datetime.fromisoformat(row["internal_date"])
                            if row["internal_date"]
                            else None
                        )
                        target_header_audit_required = (
                            not row["target_date_header_verified"]
                            and is_qq_source(email)
                        )
                        target_has_date_header = target_date_headers.get(
                            int(row["target_uid"]),
                            False,
                        )
                        if (
                            source_date is not None
                            and source_date == stored_date
                            and target_date == stored_date
                            and (
                                not target_header_audit_required
                                or target_has_date_header
                            )
                        ):
                            db.execute(
                                """
                                UPDATE messages
                                SET source_date_verified=1,
                                    target_date_header_verified=1,
                                    updated_at=?
                                WHERE source_email=? AND content_hash=?
                                """,
                                (
                                    now_iso(),
                                    email,
                                    row["content_hash"],
                                ),
                            )
                            account_verified += 1
                            totals["verified"] += 1
                            continue

                        message = fetch_source_message(
                            source,
                            email,
                            folder_name,
                            uidvalidity,
                            uid,
                            profiles[email],
                        )
                        if message.content_hash != row["content_hash"]:
                            raise SyncError(
                                f"Source content changed during date repair: "
                                f"{email} {folder_name} UID {uid}"
                            )
                        if is_deleted_draft_message(rule, folder, message):
                            raise SyncError(
                                f"Tracked Outlook deleted draft encountered "
                                f"during date repair: {email} UID {uid}"
                            )
                        qq_header_rebuild = (
                            is_qq_source(email)
                            and needs_qq_date_header(message)
                            and message.internal_date is not None
                        )
                        action = sync_message(
                            db,
                            target,
                            target_rule["folder"],
                            message,
                            rule["account_label"],
                            source_folder_label(rule, folder),
                            labels_by_name,
                            run_id,
                            force_rebuild=(
                                target_date != message.internal_date
                                or qq_header_rebuild
                            ),
                        )
                        if action == "date_rebuilt":
                            account_rebuilt += 1
                            totals["date_rebuilt"] += 1
                            if qq_header_rebuild:
                                totals["qq_header_rebuilt"] += 1
                        else:
                            account_verified += 1
                            totals["verified"] += 1
                        if (account_rebuilt + account_verified) % 100 == 0:
                            print(
                                json.dumps(
                                    {
                                        "event": "date_repair_progress",
                                        "source": email,
                                        "verified": account_verified,
                                        "date_rebuilt": account_rebuilt,
                                        "qq_header_rebuilt": totals[
                                            "qq_header_rebuilt"
                                        ],
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                    db.commit()
                unresolved_deleted = db.execute(
                    """
                    SELECT COUNT(*) FROM messages
                    WHERE source_email=?
                      AND (
                          source_date_verified=0
                          OR target_date_header_verified=0
                      )
                      AND source_folder='__deleted__'
                    """,
                    (email,),
                ).fetchone()[0]
                totals["unresolved"] += int(unresolved_deleted)
                print(
                    json.dumps(
                        {
                            "event": "date_repair_source_completed",
                            "source": email,
                            "verified": account_verified,
                            "date_rebuilt": account_rebuilt,
                            "unresolved_deleted": int(unresolved_deleted),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            finally:
                safe_logout(source)
        remaining = db.execute(
            """
            SELECT COUNT(*) FROM messages
            WHERE source_date_verified=0
               OR target_date_header_verified=0
            """
        ).fetchone()[0]
        print(
            json.dumps(
                {
                    "event": "date_repair_completed",
                    **totals,
                    "remaining_unverified": int(remaining),
                    "state_db": str(state_path),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0 if remaining == 0 else 1
    finally:
        try:
            safe_logout(target)
        finally:
            db.close()


def command_watch(
    config: dict,
    profiles: Dict[str, dict],
    labels_by_name: Dict[str, str],
    state_path: Path,
    source_email: Optional[str],
    only_folder: Optional[str],
    limit: Optional[int],
    newest_first: bool,
    interval: int,
    max_cycles: Optional[int],
) -> int:
    rules = selected_source_rules(config, source_email)
    if not rules:
        raise SyncError("No source mailboxes are configured")

    slot_seconds = interval / len(rules)
    started_at = time.monotonic()
    schedule = []
    for index, rule in enumerate(rules):
        source_interval = int(rule.get("sync_interval", interval))
        if source_interval < 1:
            raise SyncError(
                f"sync_interval for {rule['email']} must be at least 1 second"
            )
        schedule.append(
            {
                "email": rule["email"],
                "interval": source_interval,
                "batch_size": int(
                    rule.get(
                        "batch_size",
                        os.environ.get("SYNC_BATCH_SIZE", "5"),
                    )
                ),
                "next_run": started_at + index * slot_seconds,
            }
        )
        if schedule[-1]["batch_size"] < 1:
            raise SyncError(
                f"batch_size for {rule['email']} must be at least 1"
            )

    cycle = 0
    while True:
        job = min(schedule, key=lambda item: item["next_run"])
        delay = job["next_run"] - time.monotonic()
        if delay > 0:
            time.sleep(delay)

        cycle += 1
        print(
            json.dumps(
                {
                    "event": "source_sync_started",
                    "cycle": cycle,
                    "source": job["email"],
                    "interval": job["interval"],
                    "batch_size": job["batch_size"],
                    "time": now_iso(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            command_sync(
                config,
                profiles,
                labels_by_name,
                state_path,
                job["email"],
                only_folder,
                limit if limit is not None else job["batch_size"],
                newest_first,
            )
            success = True
        except (SyncError, imaplib.IMAP4.error, OSError, ValueError) as exc:
            success = False
            print(
                json.dumps(
                    {
                        "event": "source_sync_failed",
                        "cycle": cycle,
                        "source": job["email"],
                        "time": now_iso(),
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )

        if max_cycles is not None and cycle >= max_cycles:
            return 0 if success else 1

        job["next_run"] = time.monotonic() + job["interval"]
        next_job = min(schedule, key=lambda item: item["next_run"])
        print(
            json.dumps(
                {
                    "event": "next_source_sync_scheduled",
                    "source": next_job["email"],
                    "seconds": round(
                        max(0.0, next_job["next_run"] - time.monotonic()),
                        3,
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="config.json",
        help="Configuration file (default: config.json)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list-folders",
        help="List source folders and show which ones will sync",
    )
    list_parser.add_argument("--source", help="Only this source email")

    authorize_parser = subparsers.add_parser(
        "authorize-outlook",
        help="Authorize one Outlook source using the Microsoft device-code flow",
    )
    authorize_parser.add_argument(
        "--source",
        required=True,
        help="Outlook source email to authorize",
    )

    sync_parser = subparsers.add_parser("sync", help="Run incremental synchronization")
    sync_parser.add_argument("--source", help="Only this source email")
    sync_parser.add_argument("--folder", help="Only this source folder")
    sync_parser.add_argument("--limit", type=int, help="Maximum messages to process")
    sync_parser.add_argument(
        "--newest-first",
        action="store_true",
        help="Process higher source UIDs first",
    )
    repair_dates_parser = subparsers.add_parser(
        "repair-dates",
        help="Verify source INTERNALDATE values and rebuild target mismatches",
    )
    repair_dates_parser.add_argument(
        "--source",
        help="Only this source email",
    )
    watch_parser = subparsers.add_parser(
        "watch",
        help="Run synchronization continuously at a fixed interval",
    )
    watch_parser.add_argument("--source", help="Only this source email")
    watch_parser.add_argument("--folder", help="Only this source folder")
    watch_parser.add_argument("--limit", type=int, help="Maximum messages per cycle")
    watch_order = watch_parser.add_mutually_exclusive_group()
    watch_order.add_argument(
        "--newest-first",
        dest="newest_first",
        action="store_true",
        help="Process higher source UIDs first (default)",
    )
    watch_order.add_argument(
        "--oldest-first",
        dest="newest_first",
        action="store_false",
        help="Process lower source UIDs first",
    )
    watch_parser.set_defaults(newest_first=True)
    watch_parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("SYNC_INTERVAL", "60")),
        help="Seconds between cycle start times (default: SYNC_INTERVAL or 60)",
    )
    watch_parser.add_argument(
        "--max-cycles",
        type=int,
        help="Stop after this many cycles (primarily for verification)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    base = config_path.parent
    config = load_json(config_path)
    resolve_auth_token_paths(config, base)
    state_path = resolve_path(
        base,
        os.environ.get("MAIL_SYNC_STATE_DB", config["state_db"]),
    )
    profiles = credential_profiles(config)
    labels_by_name = label_id_map(config["synology_labels"])

    if TARGET_PROFILE_KEY not in profiles:
        raise SyncError("Missing target mailbox configuration")
    required_emails = {rule["email"] for rule in config.get("sources", [])}
    missing = required_emails - set(profiles)
    if missing:
        raise SyncError(f"Missing credentials for: {sorted(missing)}")

    if args.command == "list-folders":
        return command_list_folders(config, profiles, args.source)
    if args.command == "authorize-outlook":
        return command_authorize_outlook(config, profiles, args.source)
    if args.command == "sync":
        with process_lock(state_path):
            return command_sync(
                config,
                profiles,
                labels_by_name,
                state_path,
                args.source,
                args.folder,
                args.limit,
                args.newest_first,
            )
    if args.command == "repair-dates":
        with process_lock(state_path):
            return command_repair_dates(
                config,
                profiles,
                labels_by_name,
                state_path,
                args.source,
            )
    if args.command == "watch":
        if args.interval < 1:
            raise SyncError("--interval must be at least 1 second")
        if args.max_cycles is not None and args.max_cycles < 1:
            raise SyncError("--max-cycles must be positive")
        with process_lock(state_path):
            return command_watch(
                config,
                profiles,
                labels_by_name,
                state_path,
                args.source,
                args.folder,
                args.limit,
                args.newest_first,
                args.interval,
                args.max_cycles,
            )
    raise SyncError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(
            json.dumps(
                {"event": "synchronizer_stopped", "time": now_iso()},
                ensure_ascii=False,
            )
        )
        raise SystemExit(0)
    except (SyncError, imaplib.IMAP4.error, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
