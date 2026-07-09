from __future__ import annotations

import sqlite3


def mark_sync_rule_success(db: sqlite3.Connection, source_email: str) -> None:
    db.execute(
        """
        UPDATE source_rules
        SET last_sync_status='ok',
            last_sync_error_summary=NULL,
            last_sync_error_detail=NULL
        WHERE email=?
        """,
        (source_email,),
    )
    db.commit()


def mark_sync_rule_failure(
    db: sqlite3.Connection,
    source_email: str,
    error: object,
) -> None:
    detail = str(error or "").strip() or "未知同步错误"
    summary = _error_summary(detail)
    db.execute(
        """
        UPDATE source_rules
        SET last_sync_status='error',
            last_sync_error_summary=?,
            last_sync_error_detail=?
        WHERE email=?
        """,
        (summary, detail, source_email),
    )
    db.commit()


def _error_summary(detail: str) -> str:
    if detail.startswith("Cannot select mailbox "):
        folder = detail.removeprefix("Cannot select mailbox ").strip()
        return f"目标文件夹不可用：{folder}"
    if "Undefined Synology label" in detail:
        return "标签配置不可用"
    if "Missing credentials" in detail:
        return "邮箱凭据不可用"
    text = detail.splitlines()[0].strip()
    return text[:120] if len(text) > 120 else text
