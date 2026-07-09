from __future__ import annotations

from email import message_from_bytes
from typing import Set, Tuple

from common.errors import SyncError
from domain.message import SourceMessage
from imap.protocol import FolderInfo


def source_folder_labels(source_rule: dict, folder: FolderInfo) -> Tuple[str, ...]:
    mapping = source_rule.get("folder_labels", {})
    value = (
        mapping.get(folder.raw_name)
        or mapping.get(folder.display_name)
        or source_rule.get("default_folder_label", "")
    )
    if isinstance(value, str):
        labels = [value]
    elif isinstance(value, (list, tuple)):
        labels = [str(item) for item in value]
    else:
        labels = [str(value)]
    return tuple(label.strip() for label in labels if label.strip())


def source_folder_label(source_rule: dict, folder: FolderInfo) -> str:
    labels = source_folder_labels(source_rule, folder)
    return labels[0] if labels else source_rule.get("default_folder_label", "")


def source_folder_target_folder(
    source_rule: dict,
    folder: FolderInfo,
    default_target_folder: str,
) -> str:
    mapping = source_rule.get("target_folders", {})
    if not isinstance(mapping, dict):
        raise SyncError("target_folders must be an object keyed by source folder")
    value = mapping.get(folder.raw_name) or mapping.get(folder.display_name)
    target_folder = str(value or default_target_folder or "INBOX").strip()
    return target_folder or "INBOX"


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
    if source_rule.get("deleted_label") not in source_folder_labels(source_rule, folder):
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
