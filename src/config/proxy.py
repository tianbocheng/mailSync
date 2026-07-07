from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse

from common.errors import SyncError


@dataclass(frozen=True)
class ImapProxySettings:
    proxy_type: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None


def _validate_proxy_type(value: object, context: str) -> str:
    proxy_type = str(value or "http_connect")
    if proxy_type == "http":
        proxy_type = "http_connect"
    if proxy_type != "http_connect":
        raise SyncError(
            f"Unsupported IMAP proxy type for {context}: {proxy_type}"
        )
    return proxy_type


def _validate_port(value: object, context: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise SyncError(f"IMAP proxy port for {context} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise SyncError(f"IMAP proxy port for {context} must be 1-65535")
    return port


def parse_imap_proxy(raw: object, context: str) -> Optional[ImapProxySettings]:
    """Parse an optional per-mailbox IMAP proxy configuration."""
    if raw in (None, False, ""):
        return None

    if isinstance(raw, str):
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "http_connect"):
            raise SyncError(
                f"IMAP proxy URL for {context} must use http://"
            )
        if not parsed.hostname:
            raise SyncError(f"IMAP proxy URL for {context} is missing a host")
        return ImapProxySettings(
            proxy_type="http_connect",
            host=parsed.hostname,
            port=_validate_port(parsed.port or 8080, context),
            username=unquote(parsed.username) if parsed.username else None,
            password=unquote(parsed.password) if parsed.password else None,
        )

    if not isinstance(raw, dict):
        raise SyncError(f"imap_proxy for {context} must be a URL string or object")

    if raw.get("url"):
        settings = parse_imap_proxy(raw["url"], context)
        if settings is None:
            return None
        return ImapProxySettings(
            proxy_type=_validate_proxy_type(raw.get("type", settings.proxy_type), context),
            host=settings.host,
            port=settings.port,
            username=raw.get("username", settings.username),
            password=raw.get("password", settings.password),
        )

    proxy_type = _validate_proxy_type(raw.get("type", "http_connect"), context)
    host = raw.get("host")
    if not isinstance(host, str) or not host:
        raise SyncError(f"imap_proxy.host for {context} is required")
    return ImapProxySettings(
        proxy_type=proxy_type,
        host=host,
        port=_validate_port(raw.get("port", 8080), context),
        username=raw.get("username"),
        password=raw.get("password"),
    )


def profile_imap_proxy(profile: dict) -> Optional[ImapProxySettings]:
    return parse_imap_proxy(
        profile.get("imap_proxy"),
        str(profile.get("email", "<unknown>")),
    )
