from __future__ import annotations

import errno
import imaplib
import os
import selectors
import socket
import sys
import time

from auth.oauth import authenticate_oauth
from common.errors import SyncError
from config.proxy import ImapProxySettings, profile_imap_proxy
from config.validation import oauth_settings
from connection.proxy import http_connect_socket
from imap.protocol import select_mailbox


NETEASE_IMAP_SERVERS = {
    "imap.163.com",
    "imap.126.com",
    "imap.yeah.net",
}
DEFAULT_IMAP_TIMEOUT_SECONDS = float(
    os.environ.get("IMAP_TIMEOUT_SECONDS", "60")
)
DEFAULT_IMAP_CONNECT_TIMEOUT_SECONDS = float(
    os.environ.get("IMAP_CONNECT_TIMEOUT_SECONDS", "10")
)


class DirectIMAP4(imaplib.IMAP4):
    def _create_socket(self, timeout):
        return _direct_imap_socket(self, timeout)


class DirectIMAP4_SSL(imaplib.IMAP4_SSL):
    def _create_socket(self, timeout):
        sock = _direct_imap_socket(self, timeout)
        try:
            return self.ssl_context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


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
    timeout = _imap_timeout(profile)
    if profile.get("use_ssl", True):
        if proxy is not None:
            client: imaplib.IMAP4 = ProxiedIMAP4_SSL(
                host,
                port,
                proxy,
                timeout=timeout,
            )
        else:
            client = DirectIMAP4_SSL(host, port, timeout=timeout)
    else:
        if proxy is not None:
            client = ProxiedIMAP4(host, port, proxy, timeout=timeout)
        else:
            client = DirectIMAP4(host, port, timeout=timeout)
    if oauth_settings(profile) is not None:
        authenticate_oauth(client, profile)
    else:
        if not profile.get("password"):
            raise SyncError(f"Missing password for {profile['email']}")
        typ, _ = client.login(profile["email"], profile["password"])
        if typ != "OK":
            raise SyncError(f"IMAP login failed for {profile['email']}")
    identity = profile.get("imap_id") or default_imap_identity(profile)
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
    features = {}
    raw_features = profile.get("mailbox_features")
    if isinstance(raw_features, dict):
        features.update(raw_features)
    feature_groups = profile.get("mailbox_feature_groups")
    if isinstance(feature_groups, dict):
        for values in feature_groups.values():
            if isinstance(values, dict):
                features.update(values)
        try:
            setattr(client, "_mail_sync_feature_groups", dict(feature_groups))
        except Exception:
            pass
    if features:
        try:
            setattr(client, "_mail_sync_features", features)
        except Exception:
            pass
    return client


def _imap_timeout(profile: dict) -> float | None:
    raw = profile.get("imap_timeout")
    if raw in (None, ""):
        return DEFAULT_IMAP_TIMEOUT_SECONDS
    timeout = float(raw)
    if timeout <= 0:
        return None
    return timeout


def _direct_imap_socket(
    client: imaplib.IMAP4,
    timeout: float | None,
) -> socket.socket:
    if timeout is not None and timeout <= 0:
        raise ValueError("Non-blocking socket (timeout=0) is not supported")
    host = None if not client.host else client.host
    sys.audit("imaplib.open", client, client.host, client.port)
    connect_timeout = _imap_connect_timeout(timeout)
    sock = _race_direct_connection(host, client.port, connect_timeout)
    sock.settimeout(timeout)
    return sock


def _imap_connect_timeout(read_timeout: float | None) -> float | None:
    if read_timeout is None:
        return None
    return min(float(read_timeout), DEFAULT_IMAP_CONNECT_TIMEOUT_SECONDS)


def _race_direct_connection(
    host: str | None,
    port: int,
    timeout: float | None,
) -> socket.socket:
    if timeout is None:
        return socket.create_connection((host, port))
    if timeout <= 0:
        raise ValueError("Connection timeout must be positive")

    addresses = _direct_connection_addresses(host, port)
    selector = selectors.DefaultSelector()
    sockets: list[socket.socket] = []
    errors: list[OSError] = []
    winner: socket.socket | None = None
    pending_codes = {
        0,
        errno.EINPROGRESS,
        errno.EWOULDBLOCK,
        errno.EALREADY,
        errno.EINTR,
        errno.EISCONN,
    }
    deadline = time.monotonic() + timeout
    try:
        for family, socktype, proto, sockaddr in addresses:
            sock = socket.socket(family, socktype, proto)
            sockets.append(sock)
            sock.setblocking(False)
            error_code = sock.connect_ex(sockaddr)
            if error_code in {0, errno.EISCONN}:
                winner = sock
                break
            if error_code not in pending_codes:
                errors.append(OSError(error_code, os.strerror(error_code)))
                sock.close()
                continue
            selector.register(sock, selectors.EVENT_WRITE)

        while winner is None and selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            events = selector.select(remaining)
            if not events:
                break
            for key, _ in events:
                sock = key.fileobj
                error_code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if error_code == 0:
                    winner = sock
                    break
                errors.append(OSError(error_code, os.strerror(error_code)))
                selector.unregister(sock)
                sock.close()

        if winner is None:
            if selector.get_map():
                raise TimeoutError(
                    f"Timed out connecting to {host}:{port} after {timeout:g}s"
                )
            if errors:
                raise errors[-1]
            raise OSError(f"No usable address found for {host}:{port}")

        winner.setblocking(True)
        return winner
    finally:
        selector.close()
        for sock in sockets:
            if sock is not winner:
                sock.close()


def _direct_connection_addresses(
    host: str | None,
    port: int,
) -> list[tuple[int, int, int, tuple]]:
    addresses = []
    seen = set()
    for family, socktype, proto, _, sockaddr in socket.getaddrinfo(
        host,
        port,
        0,
        socket.SOCK_STREAM,
    ):
        key = (family, socktype, proto, sockaddr)
        if key in seen:
            continue
        seen.add(key)
        addresses.append((family, socktype, proto, sockaddr))
    return addresses


def default_imap_identity(profile: dict) -> dict:
    configured = _common_feature(profile, "imap_id_required")
    if configured is False:
        return {}
    host = str(profile.get("imap_server") or "").strip().casefold()
    email = str(profile.get("email") or "").strip()
    domain = email.rsplit("@", 1)[-1].casefold() if "@" in email else ""
    needs_identity = configured is True or host in NETEASE_IMAP_SERVERS or domain in {
        "163.com",
        "126.com",
        "yeah.net",
    }
    if not needs_identity:
        return {}
    return {
        "name": "mail-sync",
        "version": "1.0.0",
        "vendor": "lantian-mail",
        "support-email": email or "support@example.com",
    }


def _common_feature(profile: dict, key: str) -> bool | None:
    groups = profile.get("mailbox_feature_groups")
    if isinstance(groups, dict):
        common = groups.get("common")
        if isinstance(common, dict) and key in common:
            return bool(common.get(key))
    features = profile.get("mailbox_features")
    if isinstance(features, dict) and key in features:
        return bool(features.get(key))
    return None


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
