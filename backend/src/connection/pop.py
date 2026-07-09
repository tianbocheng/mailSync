from __future__ import annotations

import poplib
import ssl

from common.errors import SyncError
from config.proxy import ImapProxySettings, profile_imap_proxy
from connection.proxy import http_connect_socket


class ProxiedPOP3(poplib.POP3):
    def __init__(
        self,
        host: str,
        port: int,
        proxy: ImapProxySettings,
        timeout: float | None = None,
    ):
        self._mail_sync_proxy = proxy
        super().__init__(host, port, timeout=timeout)

    def _create_socket(self, timeout):
        return http_connect_socket(
            self.host,
            self.port,
            self._mail_sync_proxy,
            timeout,
        )


class ProxiedPOP3_SSL(poplib.POP3_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        proxy: ImapProxySettings,
        timeout: float | None = None,
    ):
        self._mail_sync_proxy = proxy
        super().__init__(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )

    def _create_socket(self, timeout):
        sock = http_connect_socket(
            self.host,
            self.port,
            self._mail_sync_proxy,
            timeout,
        )
        try:
            return self.context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def connect_pop(profile: dict) -> poplib.POP3:
    settings = profile.get("pop_fallback")
    if not isinstance(settings, dict):
        raise SyncError(f"POP is not configured for {profile['email']}")
    if not profile.get("password"):
        raise SyncError(f"POP requires a password for {profile['email']}")

    host = settings.get("server") or settings.get("host")
    if not host:
        raise SyncError(f"POP server is missing for {profile['email']}")
    use_ssl = bool(settings.get("use_ssl", True))
    port = int(settings.get("port", 995 if use_ssl else 110))
    timeout = settings.get("timeout", 300)
    if timeout is not None:
        timeout = float(timeout)
    proxy = profile_imap_proxy(profile)

    client: poplib.POP3 | None = None
    try:
        if use_ssl:
            if proxy is not None:
                client = ProxiedPOP3_SSL(host, port, proxy, timeout=timeout)
            else:
                client = poplib.POP3_SSL(
                    host,
                    port,
                    timeout=timeout,
                    context=ssl.create_default_context(),
                )
        elif proxy is not None:
            client = ProxiedPOP3(host, port, proxy, timeout=timeout)
        else:
            client = poplib.POP3(host, port, timeout=timeout)

        client.user(profile["email"])
        client.pass_(profile["password"])
        return client
    except Exception:
        if client is not None:
            safe_pop_quit(client)
        raise


def safe_pop_quit(client: poplib.POP3) -> None:
    try:
        client.quit()
    except (poplib.error_proto, OSError, EOFError):
        pass
