import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.header import Header
from pathlib import Path
from unittest import mock


import mail_sync
from connection import client as connection_module
from auth import oauth as oauth_module
from domain import message as mail_sync_message

from tests import helpers  # noqa: F401


class FakeImap:
    def __init__(self):
        self.login_calls = []
        self.oauth_payloads = []

    def login(self, email, password):
        self.login_calls.append((email, password))
        return "OK", [b"logged in"]

    def authenticate(self, mechanism, callback):
        self.oauth_payloads.append((mechanism, callback(b"")))
        return "OK", [b"authenticated"]


class OAuthTests(unittest.TestCase):
    def test_password_login_remains_supported(self):
        client = FakeImap()
        profile = {
            "email": "legacy@example.com",
            "password": "secret",
            "imap_server": "imap.example.com",
        }
        with mock.patch.object(connection_module.imaplib, "IMAP4_SSL", return_value=client):
            result = mail_sync.connect(profile)
        self.assertIs(result, client)
        self.assertEqual(client.login_calls, [("legacy@example.com", "secret")])
        self.assertEqual(client.oauth_payloads, [])

    def test_oauth_login_uses_xoauth2_without_password(self):
        client = FakeImap()
        profile = {
            "email": "user@outlook.com",
            "imap_server": "outlook.office365.com",
            "auth": {
                "type": "oauth2",
                "provider": "microsoft",
                "client_id": "client-id",
                "token_file": "/tmp/not-read-in-this-test.json",
            },
        }
        with (
            mock.patch.object(connection_module.imaplib, "IMAP4_SSL", return_value=client),
            mock.patch.object(
                oauth_module,
                "microsoft_access_token",
                return_value="access-token",
            ),
        ):
            mail_sync.connect(profile)
        self.assertEqual(client.login_calls, [])
        self.assertEqual(
            client.oauth_payloads,
            [
                (
                    "XOAUTH2",
                    b"user=user@outlook.com\x01"
                    b"auth=Bearer access-token\x01\x01",
                )
            ],
        )

    def test_imap_proxy_uses_proxied_ssl_client(self):
        client = FakeImap()
        profile = {
            "email": "user@gmail.com",
            "password": "secret",
            "imap_server": "imap.gmail.com",
            "imap_port": 993,
            "imap_proxy": "http://192.168.1.178:6152",
        }
        with (
            mock.patch.object(
                connection_module,
                "ProxiedIMAP4_SSL",
                return_value=client,
            ) as proxied,
            mock.patch.object(connection_module.imaplib, "IMAP4_SSL") as direct,
        ):
            result = mail_sync.connect(profile)
        self.assertIs(result, client)
        direct.assert_not_called()
        host, port, proxy = proxied.call_args.args
        self.assertEqual(host, "imap.gmail.com")
        self.assertEqual(port, 993)
        self.assertEqual(proxy.host, "192.168.1.178")
        self.assertEqual(proxy.port, 6152)
        self.assertEqual(client.login_calls, [("user@gmail.com", "secret")])

    def test_expired_token_is_refreshed_and_rotated_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "outlook-token.json"
            token_file.write_text(
                json.dumps(
                    {
                        "access_token": "expired",
                        "refresh_token": "old-refresh",
                        "expires_at": int(time.time()) - 1,
                    }
                ),
                encoding="utf-8",
            )
            profile = {
                "email": "user@outlook.com",
                "auth": {
                    "type": "oauth2",
                    "provider": "microsoft",
                    "client_id": "client-id",
                    "tenant": "consumers",
                    "token_file": str(token_file),
                },
            }
            response = {
                "access_token": "fresh-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }
            with mock.patch.object(
                oauth_module,
                "post_form_json",
                return_value=response,
            ) as request:
                access_token = mail_sync.microsoft_access_token(profile)

            self.assertEqual(access_token, "fresh-access")
            url, fields = request.call_args.args
            self.assertEqual(
                url,
                "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
            )
            self.assertEqual(fields["grant_type"], "refresh_token")
            self.assertEqual(fields["refresh_token"], "old-refresh")
            self.assertIn(mail_sync.MICROSOFT_IMAP_SCOPE, fields["scope"])
            saved = json.loads(token_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["refresh_token"], "new-refresh")
            self.assertGreater(saved["expires_at"], int(time.time()))
            self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    def test_oauth_token_files_must_not_be_shared(self):
        config = {
            "sources": [
                {
                    "email": "one@outlook.com",
                    "auth": {
                        "type": "oauth2",
                        "client_id": "id",
                        "token_file": "shared.json",
                    },
                },
                {
                    "email": "two@outlook.com",
                    "auth": {
                        "type": "oauth2",
                        "client_id": "id",
                        "token_file": "shared.json",
                    },
                },
            ]
        }
        with self.assertRaisesRegex(mail_sync.SyncError, "shared"):
            mail_sync.resolve_auth_token_paths(config, Path("/tmp"))

    def test_device_authorization_persists_refresh_token(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "device-token.json"
            profile = {
                "email": "user@outlook.com",
                "auth": {
                    "type": "oauth2",
                    "provider": "microsoft",
                    "client_id": "client-id",
                    "token_file": str(token_file),
                },
            }
            config = {"sources": [profile]}
            responses = [
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "expires_in": 600,
                    "interval": 1,
                },
                {"error": "authorization_pending"},
                {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 3600,
                },
            ]
            with (
                mock.patch.object(
                    oauth_module,
                    "post_form_json",
                    side_effect=responses,
                ) as request,
                mock.patch.object(oauth_module.time, "sleep"),
            ):
                result = mail_sync.command_authorize_outlook(
                    config,
                    {"user@outlook.com": profile},
                    "user@outlook.com",
                )

            self.assertEqual(result, 0)
            self.assertEqual(request.call_count, 3)
            saved = json.loads(token_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["access_token"], "access")
            self.assertEqual(saved["refresh_token"], "refresh")

class CredentialProfileTests(unittest.TestCase):
    def test_source_may_share_email_address_with_target(self):
        config = {
            "target": {
                "email": "same@example.com",
                "imap_server": "target.example.com",
            },
            "sources": [
                {
                    "email": "same@example.com",
                    "imap_server": "source.example.com",
                }
            ],
        }
        profiles = mail_sync.credential_profiles(config)
        self.assertEqual(
            profiles[mail_sync.TARGET_PROFILE_KEY]["imap_server"],
            "target.example.com",
        )
        self.assertEqual(
            profiles["same@example.com"]["imap_server"],
            "source.example.com",
        )

class ConfigValidationTests(unittest.TestCase):
    def valid_config(self):
        labels = {
            "$account": "SYNC:source@example.com",
            "$inbox": "BOX:收件箱",
            "$other": "BOX:其他文件夹",
            "$deleted": "BOX:已删除邮件",
        }
        return {
            "state_db": "data/state.sqlite3",
            "target": {
                "email": "target@example.com",
                "password": "secret",
                "imap_server": "imap.example.com",
                "folder": "INBOX",
            },
            "sources": [
                {
                    "email": "source@example.com",
                    "password": "secret",
                    "imap_server": "imap.example.com",
                    "account_label": "SYNC:source@example.com",
                    "folder_labels": {"INBOX": "BOX:收件箱"},
                    "default_folder_label": "BOX:其他文件夹",
                    "deleted_label": "BOX:已删除邮件",
                }
            ],
            "synology_labels": labels,
        }

    def test_valid_config_passes_preflight(self):
        config = self.valid_config()
        labels = mail_sync.label_id_map(config["synology_labels"])

        mail_sync.validate_config(config, labels)

    def test_imap_proxy_config_passes_preflight(self):
        config = self.valid_config()
        config["sources"][0]["imap_proxy"] = {
            "type": "http_connect",
            "host": "192.168.1.178",
            "port": 6152,
        }
        labels = mail_sync.label_id_map(config["synology_labels"])

        mail_sync.validate_config(config, labels)

    def test_invalid_imap_proxy_fails_preflight(self):
        config = self.valid_config()
        config["sources"][0]["imap_proxy"] = "socks5://127.0.0.1:1080"
        labels = mail_sync.label_id_map(config["synology_labels"])

        with self.assertRaisesRegex(mail_sync.SyncError, "IMAP proxy URL"):
            mail_sync.validate_config(config, labels)

    def test_missing_label_fails_before_syncing(self):
        config = self.valid_config()
        config["sources"][0]["folder_labels"]["Sent"] = "BOX:已发邮件"
        labels = mail_sync.label_id_map(config["synology_labels"])

        with self.assertRaisesRegex(mail_sync.SyncError, "Undefined Synology label"):
            mail_sync.validate_config(config, labels)

    def test_bad_skip_uid_configuration_fails_preflight(self):
        config = self.valid_config()
        config["sources"][0]["skip_uids"] = {"INBOX": ["not-an-int"]}
        labels = mail_sync.label_id_map(config["synology_labels"])

        with self.assertRaisesRegex(mail_sync.SyncError, "skip_uids"):
            mail_sync.validate_config(config, labels)


if __name__ == "__main__":
    unittest.main()
