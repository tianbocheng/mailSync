from __future__ import annotations

import imaplib

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
