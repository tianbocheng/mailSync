from __future__ import annotations

import imaplib

from auth.oauth import authenticate_oauth
from common.errors import SyncError
from config.proxy import ImapProxySettings, profile_imap_proxy
from config.validation import oauth_settings
from connection.proxy import http_connect_socket
from imap.protocol import select_mailbox


class ProxiedIMAP4(imaplib.IMAP4):
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


class ProxiedIMAP4_SSL(imaplib.IMAP4_SSL):
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
        sock = http_connect_socket(
            self.host,
            self.port,
            self._mail_sync_proxy,
            timeout,
        )
        try:
            return self.ssl_context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def connect(profile: dict) -> imaplib.IMAP4:
    host = profile["imap_server"]
    port = int(profile.get("imap_port", 993 if profile.get("use_ssl", True) else 143))
    proxy = profile_imap_proxy(profile)
    timeout = profile.get("imap_timeout")
    if timeout is not None:
        timeout = float(timeout)
    if profile.get("use_ssl", True):
        if proxy is not None:
            client: imaplib.IMAP4 = ProxiedIMAP4_SSL(
                host,
                port,
                proxy,
                timeout=timeout,
            )
        else:
            client = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    else:
        if proxy is not None:
            client = ProxiedIMAP4(host, port, proxy, timeout=timeout)
        else:
            client = imaplib.IMAP4(host, port, timeout=timeout)
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
