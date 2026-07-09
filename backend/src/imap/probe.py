from __future__ import annotations

import imaplib
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

from common.clock import now_iso
from common.errors import SyncError
from connection import connect, default_imap_identity, safe_logout
from domain.message import (
    content_hash,
    content_hash_matches,
    parse_internal_date,
    parse_message_date,
)
from imap.protocol import quote_mailbox, response_int, search_uids, select_mailbox
from imap.source import list_folders
from imap.target import APPENDUID_RE, supports_uid_expunge, target_fetch


APPEND_FETCH_ATTEMPTS = 5
APPEND_FETCH_DELAY_SECONDS = 0.5
HISTORICAL_PROBE_DATE = datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
COMMON_FEATURES = {
    "all_time_range_supported",
    "imap_id_required",
    "imap_login",
    "imap_noop",
    "list_folders",
    "modified_utf7_folders",
}
SOURCE_FEATURES = {
    "source_all_time_range_supported",
    "idle_supported",
    "pop_login",
    "pop_list",
    "synthesize_missing_date_header",
}
TARGET_FEATURES = {
    "appenduid_supported",
    "append_internal_date_supported",
    "append_read_after_write",
    "date_header_roundtrip",
    "display_time_uses_append_time_without_internal_date",
    "effective_display_time_uses_append_time",
    "leading_received_header_added",
    "nul_rewrite_normalized",
    "omit_append_internal_date",
    "special_content_roundtrip",
    "target_folder_selectable",
    "target_all_time_range_supported",
    "uid_expunge_supported",
    "uidnext_available",
    "web_display_time_uses_import_time",
}


def run_imap_mailbox_probe(profile: dict) -> dict:
    tested_at = now_iso()
    features: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    client: imaplib.IMAP4 | None = None
    email = str(profile.get("email") or "")
    folder = str(profile.get("folder") or profile.get("target_folder") or "INBOX")

    def add_item(
        key: str,
        label: str,
        status: str,
        message: str,
        scope: str = "common",
        details: dict[str, Any] | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "key": key,
            "label": label,
            "status": status,
            "message": message,
            "scope": scope,
        }
        if details:
            item["details"] = details
        items.append(item)

    try:
        try:
            client = connect(profile)
        except (imaplib.IMAP4.error, OSError, EOFError, ValueError, SyncError) as exc:
            features["imap_login"] = False
            add_item("imap_login", "IMAP 登录", "fail", f"登录失败：{exc}")
            return _probe_result(
                False,
                tested_at,
                "imap",
                items,
                features,
                "IMAP 登录失败。",
            )

        features["imap_login"] = True
        add_item("imap_login", "IMAP 登录", "ok", f"已登录 {email}。")
        imap_id_required = bool(default_imap_identity(profile))
        features["imap_id_required"] = imap_id_required
        add_item(
            "imap_id_required",
            "IMAP ID",
            "ok" if imap_id_required else "skip",
            "该邮箱连接会发送 IMAP ID。" if imap_id_required else "该邮箱不需要额外 IMAP ID。",
            "common",
        )

        try:
            typ, response = client.noop()
            if typ != "OK":
                raise SyncError(f"NOOP 返回异常：{response!r}")
        except (imaplib.IMAP4.error, OSError, EOFError, ValueError, SyncError) as exc:
            features["imap_noop"] = False
            add_item("imap_noop", "连接保活", "fail", str(exc))
            return _probe_result(
                False,
                tested_at,
                "imap",
                items,
                features,
                "IMAP 连接不可用。",
            )
        features["imap_noop"] = True
        add_item("imap_noop", "连接保活", "ok", "服务器响应正常。")

        idle = _idle_supported(client)
        features["idle_supported"] = idle
        features["synthesize_missing_date_header"] = _should_synthesize_date_header(email)
        add_item(
            "idle_supported",
            "IMAP IDLE",
            "ok" if idle else "warn",
            "支持准实时新邮件监听。" if idle else "不支持 IDLE，准实时同步会使用轮询。",
            "source",
        )
        add_item(
            "synthesize_missing_date_header",
            "缺 Date 补偿",
            "ok" if features["synthesize_missing_date_header"] else "skip",
            (
                "源邮件缺少 Date 时会用 INTERNALDATE 补齐。"
                if features["synthesize_missing_date_header"]
                else "源邮件缺少 Date 时不做额外补偿。"
            ),
            "source",
        )

        try:
            folders = list_folders(client)
        except (imaplib.IMAP4.error, OSError, EOFError, ValueError, SyncError) as exc:
            features["list_folders"] = False
            add_item("list_folders", "读取文件夹", "fail", f"读取失败：{exc}")
            return _probe_result(
                False,
                tested_at,
                "imap",
                items,
                features,
                "文件夹读取失败。",
            )
        special_folders = [
            folder_info
            for folder_info in folders
            if folder_info.raw_name != folder_info.display_name
        ]
        features["list_folders"] = True
        features["modified_utf7_folders"] = bool(special_folders)
        add_item(
            "list_folders",
            "读取文件夹",
            "ok",
            f"找到 {len(folders)} 个文件夹。",
            "common",
            {"count": len(folders)},
        )
        add_item(
            "folder_name_encoding",
            "特殊文件夹名",
            "ok",
            (
                f"已识别 {len(special_folders)} 个特殊编码文件夹。"
                if special_folders
                else "文件夹名编码正常。"
            ),
            "common",
            {"modified_utf7_count": len(special_folders)},
        )

        try:
            select_mailbox(client, folder, readonly=False)
        except (imaplib.IMAP4.error, OSError, EOFError, ValueError, SyncError) as exc:
            features["target_folder_selectable"] = False
            add_item(
                "select_target_folder",
                "选择写入文件夹",
                "fail",
                f"{folder} 不可写入：{exc}",
                "target",
            )
            return _probe_result(
                False,
                tested_at,
                "imap",
                items,
                features,
                "目标文件夹不可写入。",
            )
        features["target_folder_selectable"] = True
        features["uidnext_available"] = _uidnext_available(client)
        add_item(
            "select_target_folder",
            "选择写入文件夹",
            "ok",
            f"{folder} 可写入。",
            "target",
        )

        uidplus = supports_uid_expunge(client)
        features["uid_expunge_supported"] = uidplus
        add_item(
            "uid_expunge_supported",
            "单封删除",
            "ok" if uidplus else "warn",
            "支持 UID EXPUNGE，删除旧副本更精确。"
            if uidplus
            else "不支持 UID EXPUNGE，会在安全检查后使用普通 EXPUNGE。",
            "target",
        )
        add_item(
            "uidnext_available",
            "UIDNEXT",
            "ok" if features["uidnext_available"] else "warn",
            "服务器会返回 UIDNEXT。"
            if features["uidnext_available"]
            else "服务器未稳定返回 UIDNEXT，写入后会用 SEARCH 定位新邮件。",
            "target",
        )
        probe_folder = _select_probe_folder(client, folders, folder)
        preexisting_deleted = _safe_search_uids(client, "DELETED")
        if not uidplus and preexisting_deleted:
            features["append_read_after_write"] = None
            add_item(
                "append_read_same_connection",
                "写入/读取测试邮件",
                "skip",
                "服务器不支持单封清理且已有删除标记邮件，已跳过写入探测。",
                "target",
                {"preexisting_deleted": len(preexisting_deleted)},
            )
            add_item(
                "append_internal_date_visible",
                "原始时间写入",
                "skip",
                "写入探测已跳过，暂不更新原始时间特性。",
                "target",
            )
            add_item(
                "special_content_roundtrip",
                "目标特殊内容改写",
                "skip",
                "写入探测已跳过。",
                "target",
            )
            return _probe_result(
                True,
                tested_at,
                "imap",
                items,
                features,
                "IMAP 基础连接正常，写入探测已跳过。",
            )

        suite_raw = _probe_message_raw(
            email,
            "feature-suite",
            include_nul=True,
            message_date=HISTORICAL_PROBE_DATE,
        )
        suite = _append_fetch_delete(
            client,
            probe_folder,
            suite_raw,
            internal_date=HISTORICAL_PROBE_DATE,
            preexisting_deleted=preexisting_deleted,
        )
        if suite["ok"]:
            features["append_read_after_write"] = True
            features["appenduid_supported"] = bool(suite.get("appenduid_supported"))
            features["append_internal_date_supported"] = True
            features["omit_append_internal_date"] = False
            features["effective_display_time_uses_append_time"] = False
            features["all_time_range_supported"] = True
            features["date_header_roundtrip"] = _date_header_roundtrips(
                suite,
                HISTORICAL_PROBE_DATE,
            )
            display_time_uses_append_time = _display_time_uses_append_time(suite)
            features[
                "display_time_uses_append_time_without_internal_date"
            ] = display_time_uses_append_time
            if suite.get("raw") is not None:
                features["leading_received_header_added"] = (
                    _adds_leading_received_header(suite_raw, suite["raw"])
                )
            features["web_display_time_uses_import_time"] = bool(
                features.get("leading_received_header_added")
                or display_time_uses_append_time
            )
            _set_special_content_features(features, suite, suite_raw)
            add_item(
                "appenduid_supported",
                "APPENDUID",
                "ok" if features["appenduid_supported"] else "warn",
                "APPEND 会返回新 UID。"
                if features["appenduid_supported"]
                else "APPEND 不返回新 UID，会用 UIDNEXT/SEARCH 定位。",
                "target",
            )
            add_item(
                "leading_received_header_added",
                "目标补写到达记录",
                "warn" if features.get("leading_received_header_added") else "ok",
                "网页列表可能显示导入时间；邮件实际发生时间仍会保留。"
                if features.get("leading_received_header_added")
                else "未发现目标补写到达记录。",
                "target",
            )
            add_item(
                "date_header_roundtrip",
                "Date 头保留",
                "ok" if features["date_header_roundtrip"] else "warn",
                _cleanup_message(
                    "写入后 Date 头仍保留原始邮件时间。"
                    if features["date_header_roundtrip"]
                    else "写入后 Date 头不能稳定保留，邮件展示时间可能不准。",
                    suite,
                ),
                "target",
            )
            add_item(
                "mailbox_display_time",
                "目标补写到达记录",
                "warn" if features.get("web_display_time_uses_import_time") else "ok",
                (
                    "网页列表可能显示导入时间；邮件实际发生时间仍会保留。"
                    if features.get("web_display_time_uses_import_time")
                    else "未发现目标补写到达记录。"
                ),
                "target",
            )
            add_item(
                "append_internal_date_visible",
                "全时间段范围",
                "ok",
                _cleanup_message("历史时间邮件写入后可以读取。", suite),
                "target",
            )
            add_item(
                "special_content_roundtrip",
                "目标特殊内容改写",
                "warn" if features.get("nul_rewrite_normalized") else "ok",
                _cleanup_message(
                    (
                        "特殊内容可能被服务侧改写。"
                        if features.get("nul_rewrite_normalized")
                        else "特殊内容写入后保持可校验。"
                    ),
                    suite,
                ),
                "target",
            )
            ok = bool(features.get("imap_login")) and bool(features.get("list_folders"))
            message = "IMAP 检测完成。" if ok else "IMAP 检测失败。"
            return _probe_result(ok, tested_at, "imap", items, features, message)

        add_item(
            "feature_suite",
            "综合测试邮件",
            "warn",
            _cleanup_message(
                "综合测试未通过，已切换为分项验证。",
                suite,
            ),
            "target",
            {"reason": str(suite["message"])},
        )

        normal_raw = _probe_message_raw(email, "normal")
        normal = _append_fetch_delete(
            client,
            probe_folder,
            normal_raw,
            internal_date=None,
            preexisting_deleted=preexisting_deleted,
        )
        features["append_read_after_write"] = normal["ok"]
        features["appenduid_supported"] = bool(normal.get("appenduid_supported"))
        if normal.get("raw") is not None:
            features["leading_received_header_added"] = _adds_leading_received_header(
                normal_raw,
                normal["raw"],
            )
        add_item(
            "appenduid_supported",
            "APPENDUID",
            "ok" if features["appenduid_supported"] else "warn",
            "APPEND 会返回新 UID。"
            if features["appenduid_supported"]
            else "APPEND 不返回新 UID，会用 UIDNEXT/SEARCH 定位。",
            "target",
        )
        add_item(
            "leading_received_header_added",
            "目标补写到达记录",
            "warn" if features.get("leading_received_header_added") else "ok",
            "网页列表可能显示导入时间；邮件实际发生时间仍会保留。"
            if features.get("leading_received_header_added")
            else "未发现目标补写到达记录。",
            "target",
        )
        if normal["ok"]:
            add_item(
                "append_read_same_connection",
                "写入/读取测试邮件",
                "ok",
                _cleanup_message("写入后可以读取。", normal),
                "target",
            )
        else:
            add_item(
                "append_read_same_connection",
                "写入/读取测试邮件",
                "fail",
                _cleanup_message(str(normal["message"]), normal),
                "target",
            )
            add_item(
                "append_internal_date_visible",
                "原始时间写入",
                "skip",
                "普通写入/读取失败，已跳过原始时间探测。",
                "target",
            )
            add_item(
                "special_content_roundtrip",
                "目标特殊内容改写",
                "skip",
                "普通写入/读取失败，已跳过特殊内容探测。",
                "target",
            )
            return _probe_result(
                True,
                tested_at,
                "imap",
                items,
                features,
                "IMAP 基础连接正常，写入/读取探测失败。",
            )
        if not normal.get("cleanup_ok", True):
            add_item(
                "append_internal_date_visible",
                "原始时间写入",
                "skip",
                "测试邮件未能确认清理，已跳过后续写入探测。",
                "target",
            )
            add_item(
                "special_content_roundtrip",
                "目标特殊内容改写",
                "skip",
                "测试邮件未能确认清理，已跳过后续写入探测。",
                "target",
            )
            return _probe_result(
                True,
                tested_at,
                "imap",
                items,
                features,
                "IMAP 基础连接正常，后续写入探测已跳过。",
            )

        display_date = datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
        display_raw = _probe_message_raw(
            email,
            "display-time",
            message_date=display_date,
        )
        display = _append_fetch_delete(
            client,
            probe_folder,
            display_raw,
            internal_date=None,
            preexisting_deleted=preexisting_deleted,
        )
        date_header_roundtrip = _date_header_roundtrips(display, display_date)
        features["date_header_roundtrip"] = date_header_roundtrip
        display_time_uses_append_time = _display_time_uses_append_time(display)
        features[
            "display_time_uses_append_time_without_internal_date"
        ] = display_time_uses_append_time
        features["web_display_time_uses_import_time"] = bool(
            features.get("leading_received_header_added")
            or display_time_uses_append_time
        )
        add_item(
            "date_header_roundtrip",
            "Date 头保留",
            "ok" if date_header_roundtrip else "warn",
            _cleanup_message(
                "写入后 Date 头仍保留原始邮件时间。"
                if date_header_roundtrip
                else "写入后 Date 头不能稳定保留，邮件展示时间可能不准。",
                display,
            ),
            "target",
        )
        add_item(
            "mailbox_display_time",
            "目标补写到达记录",
            (
                "warn"
                if features.get("web_display_time_uses_import_time")
                else "ok"
            ),
            (
                "网页列表可能显示导入时间；邮件实际发生时间仍会保留。"
                if features.get("web_display_time_uses_import_time")
                else "未发现目标补写到达记录。"
            ),
            "target",
        )
        if not display.get("cleanup_ok", True):
            add_item(
                "append_internal_date_visible",
                "原始时间写入",
                "skip",
                "测试邮件未能确认清理，已跳过原始时间探测。",
                "target",
            )
            add_item(
                "special_content_roundtrip",
                "目标特殊内容改写",
                "skip",
                "测试邮件未能确认清理，已跳过特殊内容探测。",
                "target",
            )
            return _probe_result(
                True,
                tested_at,
                "imap",
                items,
                features,
                "IMAP 检测完成，后续写入探测已跳过。",
            )

        dated_raw = _probe_message_raw(
            email,
            "internal-date",
            message_date=HISTORICAL_PROBE_DATE,
        )
        dated = _append_fetch_delete(
            client,
            probe_folder,
            dated_raw,
            internal_date=HISTORICAL_PROBE_DATE,
            preexisting_deleted=preexisting_deleted,
        )
        if dated["ok"]:
            features["append_internal_date_supported"] = True
            features["omit_append_internal_date"] = False
            features["effective_display_time_uses_append_time"] = False
            features["all_time_range_supported"] = True
            add_item(
                "append_internal_date_visible",
                "全时间段范围",
                "ok",
                _cleanup_message("历史时间邮件写入后可以读取。", dated),
                "target",
            )
        else:
            features["append_internal_date_supported"] = False
            features["omit_append_internal_date"] = True
            features[
                "effective_display_time_uses_append_time"
            ] = display_time_uses_append_time
            features["all_time_range_supported"] = False
            add_item(
                "append_internal_date_visible",
                "全时间段范围",
                "warn",
                _cleanup_message(
                    "历史时间邮件写入后不可稳定读取，同步时会按兼容方式处理。",
                    dated,
                ),
                "target",
                {"reason": str(dated["message"])},
            )
        if not dated.get("cleanup_ok", True):
            add_item(
                "special_content_roundtrip",
                "目标特殊内容改写",
                "skip",
                "测试邮件未能确认清理，已跳过特殊内容探测。",
                "target",
            )
            return _probe_result(
                True,
                tested_at,
                "imap",
                items,
                features,
                "IMAP 检测完成，特殊内容探测已跳过。",
            )

        special_raw = _probe_message_raw(email, "special-content", include_nul=True)
        special = _append_fetch_delete(
            client,
            probe_folder,
            special_raw,
            internal_date=None,
            preexisting_deleted=preexisting_deleted,
        )
        _set_special_content_features(features, special, special_raw)
        if features["special_content_roundtrip"]:
            add_item(
                "special_content_roundtrip",
                "目标特殊内容改写",
                "warn" if features.get("nul_rewrite_normalized") else "ok",
                _cleanup_message(
                    "特殊内容可能被服务侧改写。"
                    if features.get("nul_rewrite_normalized")
                    else "特殊内容写入后保持可校验。",
                    special,
                ),
                "target",
            )
        else:
            add_item(
                "special_content_roundtrip",
                "目标特殊内容改写",
                "warn",
                _cleanup_message(str(special["message"]), special),
                "target",
            )

        ok = bool(features.get("imap_login")) and bool(features.get("list_folders"))
        message = "IMAP 检测完成。" if ok else "IMAP 检测失败。"
        return _probe_result(ok, tested_at, "imap", items, features, message)
    finally:
        if client is not None:
            safe_logout(client)


def _probe_result(
    ok: bool,
    tested_at: str,
    protocol: str,
    items: list[dict[str, Any]],
    features: dict[str, Any],
    message: str,
) -> dict:
    return {
        "ok": ok,
        "message": message,
        "probe_results": {
            "tested_at": tested_at,
            "protocol": protocol,
            "items": items,
            "features": features,
            "feature_groups": _feature_groups(features),
        },
        "items": items,
        "features": features,
        "feature_groups": _feature_groups(features),
    }


def _feature_groups(features: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "common": {
            key: value
            for key, value in features.items()
            if key in COMMON_FEATURES
        },
        "source": {
            key: value
            for key, value in features.items()
            if key in SOURCE_FEATURES
        },
        "target": {
            key: value
            for key, value in features.items()
            if key in TARGET_FEATURES
        },
    }


def _select_probe_folder(
    client: imaplib.IMAP4,
    folders: list,
    fallback_folder: str,
) -> str:
    candidates = _probe_folder_candidates(folders, fallback_folder)
    for candidate in candidates:
        try:
            select_mailbox(client, candidate, readonly=False)
            return candidate
        except (imaplib.IMAP4.error, OSError, EOFError, ValueError, SyncError):
            continue
    select_mailbox(client, fallback_folder, readonly=False)
    return fallback_folder


def _probe_folder_candidates(folders: list, fallback_folder: str) -> list[str]:
    candidates: list[str] = []
    for folder in folders:
        if _is_deleted_folder(folder):
            candidates.append(str(folder.raw_name or ""))
    candidates.append(fallback_folder)
    result: list[str] = []
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _is_deleted_folder(folder: Any) -> bool:
    attrs = {
        str(attr or "").strip().casefold()
        for attr in getattr(folder, "attributes", ())
    }
    if r"\noselect" in attrs:
        return False
    if r"\trash" in attrs:
        return True
    names = {
        str(getattr(folder, "raw_name", "") or "").strip().casefold(),
        str(getattr(folder, "display_name", "") or "").strip().casefold(),
    }
    return any(
        name in {
            "deleted",
            "deleted items",
            "deleted messages",
            "trash",
            "已删除",
            "已删除邮件",
            "废纸篓",
        }
        for name in names
    )


def _set_special_content_features(
    features: dict[str, Any],
    append_result: dict[str, Any],
    expected_raw: bytes,
) -> None:
    fetched_raw = append_result.get("raw")
    if append_result.get("ok") and fetched_raw == expected_raw:
        features["special_content_roundtrip"] = True
        features["nul_rewrite_normalized"] = False
        return
    if (
        append_result.get("ok")
        and isinstance(fetched_raw, bytes)
        and content_hash_matches(
            fetched_raw,
            content_hash(expected_raw),
            expected_raw,
        )
    ):
        features["special_content_roundtrip"] = True
        features["nul_rewrite_normalized"] = _nul_was_rewritten(fetched_raw)
        return
    features["special_content_roundtrip"] = False
    features["nul_rewrite_normalized"] = False


def _nul_was_rewritten(raw: bytes) -> bool:
    return b"\x00" not in raw and b"\x80" in raw


def _append_fetch_delete(
    client: imaplib.IMAP4,
    folder: str,
    raw: bytes,
    *,
    internal_date: datetime | None,
    preexisting_deleted: set[int],
) -> dict[str, Any]:
    uid: int | None = None
    result: dict[str, Any] = {
        "ok": False,
        "message": "写入探测未完成。",
        "cleanup_ok": True,
    }
    try:
        select_mailbox(client, folder, readonly=False)
        first_possible_uid = _selected_uidnext(client)
        typ, response = client.append(
            quote_mailbox(folder),
            r"(\Seen)",
            internal_date,
            raw,
        )
        if typ != "OK":
            result = {
                "ok": False,
                "message": f"APPEND 失败：{response!r}",
                "cleanup_ok": True,
            }
            return result
        appenduid_supported = _response_has_appenduid(response)
        uid = _created_uid_from_response(client, response, first_possible_uid)
        if uid is None:
            result = {
                "ok": False,
                "message": "APPEND 成功但没有找到新 UID。",
                "cleanup_ok": True,
            }
            return result
        mailbox_internal_date = _fetch_uid_internal_date(client, uid)
        metadata, fetched_raw = _fetch_with_retry(client, uid)
        if fetched_raw is None:
            result = {
                "ok": False,
                "message": f"UID {uid} 读取不到邮件正文。",
                "uid": uid,
                "mailbox_internal_date": mailbox_internal_date,
                "cleanup_ok": True,
            }
            return result
        result = {
            "ok": True,
            "message": "写入后可以读取。",
            "uid": uid,
            "metadata": metadata,
            "raw": fetched_raw,
            "mailbox_internal_date": mailbox_internal_date,
            "message_date": parse_message_date(fetched_raw),
            "appenduid_supported": appenduid_supported,
            "cleanup_ok": True,
        }
        return result
    except (imaplib.IMAP4.error, OSError, EOFError, ValueError, SyncError) as exc:
        result = {
            "ok": False,
            "message": str(exc),
            "uid": uid,
            "cleanup_ok": True,
        }
        return result
    finally:
        if uid is not None:
            cleanup_ok, cleanup_message = _delete_probe_uid(
                client,
                uid,
                preexisting_deleted,
            )
            result["cleanup_ok"] = cleanup_ok
            if cleanup_message:
                result["cleanup_message"] = cleanup_message


def _fetch_uid_internal_date(
    client: imaplib.IMAP4,
    uid: int,
) -> datetime | None:
    typ, rows = client.uid("fetch", str(uid), "(UID INTERNALDATE FLAGS)")
    if typ != "OK":
        return None
    metadata = b" ".join(row for row in rows or [] if isinstance(row, bytes))
    return parse_internal_date(metadata)


def _fetch_with_retry(
    client: imaplib.IMAP4,
    uid: int,
) -> tuple[bytes, bytes | None]:
    last_error: Exception | None = None
    for attempt in range(APPEND_FETCH_ATTEMPTS):
        try:
            return target_fetch(client, uid, include_body=True)
        except SyncError as exc:
            last_error = exc
            if attempt < APPEND_FETCH_ATTEMPTS - 1:
                time.sleep(APPEND_FETCH_DELAY_SECONDS)
    raise SyncError(str(last_error) if last_error is not None else "FETCH 失败")


def _selected_uidnext(client: imaplib.IMAP4) -> int:
    try:
        return response_int(client, "UIDNEXT")
    except SyncError:
        return max(search_uids(client), default=0) + 1


def _created_uid_from_response(
    client: imaplib.IMAP4,
    response: list[object] | tuple[object, ...] | None,
    first_possible_uid: int,
) -> int | None:
    response_bytes = b" ".join(
        value for value in (response or []) if isinstance(value, bytes)
    )
    append_uid = APPENDUID_RE.search(response_bytes)
    if append_uid:
        return int(append_uid.group(2))
    for attempt in range(APPEND_FETCH_ATTEMPTS):
        created = [
            uid
            for uid in search_uids(client, f"UID {first_possible_uid}:*")
            if uid >= first_possible_uid
        ]
        if len(created) == 1:
            return created[0]
        if attempt < APPEND_FETCH_ATTEMPTS - 1:
            time.sleep(APPEND_FETCH_DELAY_SECONDS)
    return None


def _response_has_appenduid(
    response: list[object] | tuple[object, ...] | None,
) -> bool:
    response_bytes = b" ".join(
        value for value in (response or []) if isinstance(value, bytes)
    )
    return APPENDUID_RE.search(response_bytes) is not None


def _uidnext_available(client: imaplib.IMAP4) -> bool:
    try:
        response_int(client, "UIDNEXT")
        return True
    except SyncError:
        return False


def _delete_probe_uid(
    client: imaplib.IMAP4,
    uid: int,
    preexisting_deleted: set[int],
) -> tuple[bool, str]:
    if not _uid_exists(client, uid):
        return True, ""
    if supports_uid_expunge(client):
        typ, response = client.uid(
            "store",
            str(uid),
            "+FLAGS.SILENT",
            r"(\Deleted)",
        )
        if typ != "OK":
            return False, f"清理标记失败：{response!r}"
        typ, response = client.uid("EXPUNGE", str(uid))
        if typ != "OK":
            return False, f"单封清理失败：{response!r}"
        return True, ""
    if preexisting_deleted:
        return False, "为避免误删其他已删除邮件，未执行批量清理。"
    typ, response = client.uid(
        "store",
        str(uid),
        "+FLAGS.SILENT",
        r"(\Deleted)",
    )
    if typ != "OK":
        return False, f"清理标记失败：{response!r}"
    typ, response = client.expunge()
    if typ != "OK":
        return False, f"清理失败：{response!r}"
    return True, ""


def _uid_exists(client: imaplib.IMAP4, uid: int) -> bool:
    try:
        return uid in set(search_uids(client, f"UID {uid}"))
    except SyncError:
        return True


def _dates_close(
    left: datetime,
    right: datetime,
    *,
    tolerance: timedelta = timedelta(seconds=60),
) -> bool:
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return abs(left - right) <= tolerance


def _date_header_roundtrips(
    append_result: dict[str, Any],
    expected_date: datetime,
) -> bool:
    message_date = append_result.get("message_date")
    return bool(
        append_result.get("ok")
        and isinstance(message_date, datetime)
        and _dates_close(message_date, expected_date)
    )


def _display_time_uses_append_time(
    append_result: dict[str, Any],
) -> bool:
    message_date = append_result.get("message_date")
    mailbox_internal_date = append_result.get("mailbox_internal_date")
    return bool(
        append_result.get("ok")
        and isinstance(message_date, datetime)
        and isinstance(mailbox_internal_date, datetime)
        and not _dates_close(mailbox_internal_date, message_date)
    )


def _safe_search_uids(client: imaplib.IMAP4, criteria: str) -> set[int]:
    try:
        return set(search_uids(client, criteria))
    except SyncError:
        return set()


def _idle_supported(client: imaplib.IMAP4) -> bool:
    result: set[str] = set()
    capabilities = getattr(client, "capabilities", ())
    if isinstance(capabilities, (list, tuple, set)):
        for item in capabilities:
            if isinstance(item, bytes):
                result.update(
                    part.upper()
                    for part in item.decode("ascii", "replace").split()
                    if part
                )
            else:
                result.update(part.upper() for part in str(item).split() if part)
    if result:
        return "IDLE" in result

    capability = getattr(client, "capability", None)
    if not callable(capability):
        return False
    try:
        typ, rows = capability()
    except Exception:
        return False
    if typ != "OK":
        return False
    for row in rows or []:
        if isinstance(row, bytes):
            result.update(
                part.upper()
                for part in row.decode("ascii", "replace").split()
                if part
            )
        else:
            result.update(part.upper() for part in str(row).split() if part)
    return "IDLE" in result


def _should_synthesize_date_header(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].casefold() if "@" in email else ""
    return domain in {"qq.com", "foxmail.com"}


def _adds_leading_received_header(expected: bytes, actual: bytes) -> bool:
    if expected.lower().startswith(b"received:"):
        return False
    return actual.lower().startswith(b"received:")


def _cleanup_message(message: str, result: dict[str, Any]) -> str:
    uid = result.get("uid")
    cleanup_ok = True
    cleanup_detail = ""
    if uid is not None:
        cleanup_ok = bool(result.get("cleanup_ok", True))
        cleanup_detail = str(result.get("cleanup_message") or "")
    if cleanup_ok:
        return message
    suffix = cleanup_detail or "测试邮件未能自动清理。"
    return f"{message}（{suffix}）"


def _probe_message_raw(
    email: str,
    kind: str,
    *,
    include_nul: bool = False,
    message_date: datetime | None = None,
) -> bytes:
    token = uuid.uuid4().hex
    now = format_datetime(message_date or datetime.now(timezone.utc))
    body = (
        f"mail-sync probe {kind}\r\n"
        f"token={token}\r\n"
    ).encode("ascii")
    if include_nul:
        body += b"nul=\x00\r\n"
    return (
        f"From: mail-sync-probe@example.invalid\r\n"
        f"To: {email or 'mailbox@example.invalid'}\r\n"
        f"Subject: mail-sync probe {kind}\r\n"
        f"Message-ID: <mail-sync-probe-{token}@example.invalid>\r\n"
        f"Date: {now}\r\n"
        f"X-Mail-Sync-Probe: {token}\r\n"
        f"\r\n"
    ).encode("ascii") + body
