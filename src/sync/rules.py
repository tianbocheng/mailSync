from __future__ import annotations

from email import message_from_bytes
from typing import Set

from common.errors import SyncError
from domain.message import SourceMessage
from imap.protocol import FolderInfo


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

