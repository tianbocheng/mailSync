from __future__ import annotations

import imaplib
import re
import time
from typing import Iterable, Optional, Sequence, Set, Tuple

from common.errors import SyncError
from domain.message import (
    FLAGS_RE,
    SourceMessage,
    content_hash_matches,
    parse_fetch_tuple,
    target_content_hash,
    target_message_raw,
)
from imap.protocol import (
    UID_RE,
    quote_imap_string,
    quote_mailbox,
    response_int,
    search_uids,
    select_mailbox,
)
from sync.target_identity import target_identity_filter


APPENDUID_RE = re.compile(rb"\[APPENDUID (\d+) (\d+)\]")
APPEND_VISIBILITY_ATTEMPTS = 5
APPEND_VISIBILITY_DELAY_SECONDS = 0.5
TARGET_VERIFICATION_ATTEMPTS = 20
TARGET_VERIFICATION_DELAY_SECONDS = 1.0
NETEASE_IMAP_HOSTS = {
    "imap.163.com",
    "imap.126.com",
    "imap.yeah.net",
}


class TargetContentVerificationError(SyncError):
    def __init__(self, uid: int, detail: str = ""):
        self.uid = uid
        message = f"Target UID {uid} failed content verification"
        if detail:
            message += f": {detail}"
        super().__init__(message)


def format_flag_list(flags: Iterable[str]) -> str:
    return "(" + " ".join(sorted(set(flags))) + ")"


def _selected_mailbox_uidnext(client: imaplib.IMAP4) -> int:
    features = _target_features(client)
    if features.get("uidnext_available") is False:
        return max(search_uids(client), default=0) + 1
    try:
        return response_int(client, "UIDNEXT")
    except SyncError:
        return max(search_uids(client), default=0) + 1


def _created_uids_after_append(
    client: imaplib.IMAP4,
    first_possible_uid: int,
) -> list[int]:
    criteria = f"UID {first_possible_uid}:*"
    for attempt in range(APPEND_VISIBILITY_ATTEMPTS):
        created = [
            uid
            for uid in search_uids(client, criteria)
            if uid >= first_possible_uid
        ]
        if created:
            return created
        if attempt < APPEND_VISIBILITY_ATTEMPTS - 1:
            time.sleep(APPEND_VISIBILITY_DELAY_SECONDS)
    return []


def _verify_target_copy(
    client: imaplib.IMAP4,
    target_folder: str,
    uid: int,
    message: SourceMessage,
    label_ids: Sequence[str],
    repair_missing_labels: bool,
) -> Tuple[bool, str]:
    expected_raw = target_message_raw(message)
    expected_hash = target_content_hash(message)
    last_detail = "target body unavailable"
    for attempt in range(TARGET_VERIFICATION_ATTEMPTS):
        if attempt and target_folder:
            try:
                select_mailbox(client, target_folder, readonly=False)
            except SyncError as exc:
                last_detail = str(exc)
                if attempt < TARGET_VERIFICATION_ATTEMPTS - 1:
                    time.sleep(TARGET_VERIFICATION_DELAY_SECONDS)
                continue
        try:
            metadata, raw = target_fetch(client, uid, include_body=True)
        except SyncError as exc:
            last_detail = str(exc)
        else:
            if raw is None:
                last_detail = "target body unavailable"
            elif not content_hash_matches(raw, expected_hash, expected_raw):
                last_detail = "target content does not match source"
            else:
                missing = set(label_ids) - metadata_flags(metadata)
                if not missing:
                    return True, ""
                last_detail = f"target UID {uid} is missing labels: {sorted(missing)}"
                if repair_missing_labels:
                    for label_id in sorted(missing):
                        typ, response = client.uid(
                            "store",
                            str(uid),
                            "+FLAGS.SILENT",
                            f"({label_id})",
                        )
                        if typ != "OK":
                            raise SyncError(
                                f"Failed repairing {label_id} on target UID "
                                f"{uid}: {response!r}"
                            )
        if attempt < TARGET_VERIFICATION_ATTEMPTS - 1:
            time.sleep(TARGET_VERIFICATION_DELAY_SECONDS)
    return False, last_detail


def target_fetch(
    client: imaplib.IMAP4,
    uid: int,
    include_body: bool = False,
) -> Tuple[bytes, Optional[bytes]]:
    queries = (
        ("(FLAGS BODY.PEEK[])", "(FLAGS RFC822)")
        if include_body
        else ("(FLAGS)",)
    )
    last_error: Optional[SyncError] = None
    for query in queries:
        typ, rows = client.uid("fetch", str(uid), query)
        if typ != "OK":
            last_error = SyncError(f"Target FETCH failed for UID {uid}")
            continue
        if include_body:
            try:
                return parse_fetch_tuple(rows or [])
            except SyncError as exc:
                last_error = exc
                continue
        for row in rows or []:
            if isinstance(row, bytes) and UID_RE.search(row):
                return row, None
    if last_error is not None:
        raise last_error
    raise SyncError(f"Target UID {uid} does not exist")


def append_target_copy(
    client: imaplib.IMAP4,
    target_folder: str,
    message: SourceMessage,
    label_ids: Sequence[str],
) -> Tuple[int, int]:
    _, uidvalidity = select_mailbox(client, target_folder, readonly=False)
    first_possible_uid = _selected_mailbox_uidnext(client)
    system_flags = format_flag_list(message.system_flags)
    target_raw = target_message_raw(message)
    typ, response = client.append(
        quote_mailbox(target_folder),
        system_flags,
        _append_internal_date(client, message),
        target_raw,
    )
    if typ != "OK":
        raise SyncError(f"APPEND failed: {response!r}")
    response_bytes = b" ".join(
        value for value in (response or []) if isinstance(value, bytes)
    )
    features = _target_features(client)
    append_uid = (
        APPENDUID_RE.search(response_bytes)
        if features.get("appenduid_supported") is not False
        else None
    )
    if append_uid:
        appended_uidvalidity = int(append_uid.group(1))
        if appended_uidvalidity != uidvalidity:
            raise SyncError(
                f"APPENDUID UIDVALIDITY changed: expected {uidvalidity}, "
                f"received {appended_uidvalidity}"
            )
        created = [int(append_uid.group(2))]
    else:
        created = _created_uids_after_append(client, first_possible_uid)
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
    verified, detail = _verify_target_copy(
        client,
        target_folder,
        uid,
        message,
        label_ids,
        repair_missing_labels=True,
    )
    if not verified:
        raise TargetContentVerificationError(uid, detail)
    return uidvalidity, uid


def _append_internal_date(
    client: imaplib.IMAP4,
    message: SourceMessage,
) -> Optional[object]:
    features = _target_features(client)
    if features:
        if features.get("omit_append_internal_date") is True:
            return None
        if features.get("append_internal_date_supported") is False:
            return None
        if features.get("append_internal_date_supported") is True:
            return message.internal_date
    host = str(getattr(client, "host", "") or "").strip().casefold()
    if host in NETEASE_IMAP_HOSTS:
        return None
    return message.internal_date


def _target_features(client: imaplib.IMAP4) -> dict:
    groups = getattr(client, "_mail_sync_feature_groups", {})
    if isinstance(groups, dict) and isinstance(groups.get("target"), dict):
        return dict(groups["target"])
    features = getattr(client, "_mail_sync_features", {})
    return dict(features) if isinstance(features, dict) else {}


def client_capabilities(client: imaplib.IMAP4) -> Set[str]:
    capabilities = getattr(client, "capabilities", ())
    if not isinstance(capabilities, (list, tuple, set)):
        return set()
    result = set()
    for item in capabilities:
        if isinstance(item, bytes):
            text = item.decode("ascii", "replace")
        else:
            text = str(item)
        result.update(part.upper() for part in text.split() if part)
    return result


def supports_uid_expunge(client: imaplib.IMAP4) -> bool:
    cached = getattr(client, "__dict__", {}).get(
        "_mail_sync_supports_uid_expunge"
    )
    if cached is not None:
        return bool(cached)
    features = _target_features(client)
    if "uid_expunge_supported" in features:
        supported = bool(features["uid_expunge_supported"])
        try:
            setattr(client, "_mail_sync_supports_uid_expunge", supported)
        except Exception:
            pass
        return supported
    supported = "UIDPLUS" in client_capabilities(client)
    try:
        setattr(client, "_mail_sync_supports_uid_expunge", supported)
    except Exception:
        pass
    return supported


def ensure_no_preexisting_deleted(client: imaplib.IMAP4, old_uid: int) -> None:
    deleted = set(search_uids(client, "DELETED"))
    unexpected = deleted - {old_uid}
    if unexpected:
        raise SyncError(
            "Target mailbox contains unrelated messages already marked Deleted; "
            f"refusing broad EXPUNGE: {sorted(unexpected)}"
        )


def ensure_target_delete_will_be_safe(
    client: imaplib.IMAP4,
    old_uid: int,
) -> None:
    if not supports_uid_expunge(client):
        ensure_no_preexisting_deleted(client, old_uid)


def delete_target_copy(
    client: imaplib.IMAP4,
    target_folder: str,
    old_uid: int,
) -> None:
    select_mailbox(client, target_folder, readonly=False)
    if old_uid not in set(search_uids(client, f"UID {old_uid}")):
        return
    ensure_target_delete_will_be_safe(client, old_uid)
    typ, response = client.uid(
        "store",
        str(old_uid),
        "+FLAGS.SILENT",
        r"(\Deleted)",
    )
    if typ != "OK":
        raise SyncError(f"Could not mark target UID {old_uid} deleted: {response!r}")
    if supports_uid_expunge(client):
        typ, response = client.uid("EXPUNGE", str(old_uid))
        if typ != "OK":
            raise SyncError(
                f"UID EXPUNGE failed for target UID {old_uid}: {response!r}"
            )
        return
    typ, response = client.expunge()
    if typ != "OK":
        raise SyncError(f"EXPUNGE failed: {response!r}")


def find_untracked_target_by_message_id(
    db: sqlite3.Connection,
    client: imaplib.IMAP4,
    target_folder: str,
    message_id: Optional[str],
    target_email: str = "",
    target_service_address: str = "",
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
    target_filter, target_params = target_identity_filter(
        "messages",
        target_email,
        target_service_address,
    )
    for uid in candidates:
        tracked = db.execute(
            f"""
            SELECT 1 FROM messages
            WHERE target_uid=?
              AND COALESCE(target_folder, '')=?
              {target_filter}
            """,
            (uid, target_folder, *target_params),
        ).fetchone()
        if tracked is None:
            return uid
    return None


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
    matched, _detail = _verify_target_copy(
        client,
        "",
        uid,
        message,
        label_ids,
        repair_missing_labels=False,
    )
    return matched


def repair_target_copy(
    client: imaplib.IMAP4,
    target_folder: str,
    uid: int,
    message: SourceMessage,
    label_ids: Sequence[str],
) -> bool:
    select_mailbox(client, target_folder, readonly=False)
    if uid not in set(search_uids(client, f"UID {uid}")):
        return False
    matched, _detail = _verify_target_copy(
        client,
        target_folder,
        uid,
        message,
        label_ids,
        repair_missing_labels=True,
    )
    return matched


def replace_target_copy(
    client: imaplib.IMAP4,
    target_folder: str,
    message: SourceMessage,
    label_ids: Sequence[str],
    old_uid: int,
) -> Tuple[int, int]:
    select_mailbox(client, target_folder, readonly=False)
    ensure_target_delete_will_be_safe(client, old_uid)
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
