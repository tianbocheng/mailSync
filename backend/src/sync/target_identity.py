from __future__ import annotations

import sqlite3


def target_identity_from_profile(target: dict) -> tuple[str, str]:
    return (
        str(target.get("email") or "").strip(),
        str(target.get("service_address") or "").strip(),
    )


def normalized_target_identity(
    target_email: str = "",
    target_service_address: str = "",
) -> tuple[str, str]:
    return (
        str(target_email or "").strip().casefold(),
        str(target_service_address or "").strip().casefold(),
    )


def target_identity_params(
    target_email: str = "",
    target_service_address: str = "",
) -> tuple[str, str]:
    return normalized_target_identity(target_email, target_service_address)


def target_identity_filter(
    alias: str,
    target_email: str = "",
    target_service_address: str = "",
) -> tuple[str, tuple[str, ...]]:
    if not str(target_email or "").strip():
        return "", ()
    return (
        f"""
        AND lower(COALESCE({alias}.target_email, ''))=?
        AND lower(COALESCE({alias}.target_service_address, ''))=?
        """,
        target_identity_params(target_email, target_service_address),
    )


def row_matches_target_identity(
    row: sqlite3.Row | None,
    target_email: str = "",
    target_service_address: str = "",
) -> bool:
    if row is None:
        return False
    if not str(target_email or "").strip():
        return True
    keys = row.keys()
    if "target_email" not in keys:
        return False
    row_email = str(row["target_email"] or "")
    row_service = (
        str(row["target_service_address"] or "")
        if "target_service_address" in keys
        else ""
    )
    return normalized_target_identity(
        row_email,
        row_service,
    ) == normalized_target_identity(target_email, target_service_address)


def row_has_target_identity(row: sqlite3.Row | None) -> bool:
    if row is None or "target_email" not in row.keys():
        return False
    return bool(str(row["target_email"] or "").strip())


def target_identity_values(
    target_email: str = "",
    target_service_address: str = "",
) -> tuple[str | None, str | None]:
    email = str(target_email or "").strip()
    service = str(target_service_address or "").strip()
    return email or None, service or None
