from __future__ import annotations

import imaplib
import re
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


APPENDUID_RE = re.compile(rb"\[APPENDUID (\d+) (\d+)\]")


class TargetContentVerificationError(SyncError):
    def __init__(self, uid: int):
        self.uid = uid
        super().__init__(f"Target UID {uid} failed content verification")


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
        target_content_hash(message),
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
            target_content_hash(message),
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
        target_content_hash(message),
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
