import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import mail_sync


class MessageDateTests(unittest.TestCase):
    def test_internal_date_takes_precedence_over_date_header(self):
        metadata = b'1 (INTERNALDATE "01-Jun-2026 09:38:23 +0800")'
        raw = b"Date: Mon, 8 May 2023 15:05:39 +0800\r\n\r\nbody\r\n"

        result = mail_sync.source_message_date(metadata, raw)

        self.assertEqual(
            result,
            datetime.fromisoformat("2026-06-01T09:38:23+08:00"),
        )

    def test_date_header_is_used_when_internal_date_is_omitted(self):
        metadata = b"1 (UID 1 FLAGS (\\Seen))"
        raw = b"Date: Mon, 8 May 2023 15:05:39 +0800\r\n\r\nbody\r\n"

        result = mail_sync.source_message_date(metadata, raw)

        self.assertEqual(
            result,
            datetime.fromisoformat("2023-05-08T15:05:39+08:00"),
        )

    def test_naive_date_header_is_treated_as_utc(self):
        result = mail_sync.parse_message_date(
            b"Date: Mon, 8 May 2023 15:05:39\r\n\r\n"
        )

        self.assertEqual(result.tzinfo, timezone.utc)

    def test_invalid_date_header_returns_none(self):
        self.assertIsNone(
            mail_sync.parse_message_date(b"Date: definitely-not-a-date\r\n\r\n")
        )


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
        with mock.patch.object(mail_sync.imaplib, "IMAP4_SSL", return_value=client):
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
            mock.patch.object(mail_sync.imaplib, "IMAP4_SSL", return_value=client),
            mock.patch.object(
                mail_sync,
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
                mail_sync,
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
                    mail_sync,
                    "post_form_json",
                    side_effect=responses,
                ) as request,
                mock.patch.object(mail_sync.time, "sleep"),
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


class ContentVerificationTests(unittest.TestCase):
    def test_exact_content_matches(self):
        raw = b"Subject: test\r\n\r\nbody\r\n"
        content_hash = mail_sync.hashlib.sha256(raw).hexdigest()
        self.assertTrue(mail_sync.content_hash_matches(raw, content_hash))

    def test_crlf_target_matches_lf_source_hash(self):
        source = b"Subject: test\n\nbody\n"
        target = b"Subject: test\r\n\r\nbody\r\n"
        content_hash = mail_sync.hashlib.sha256(source).hexdigest()
        self.assertTrue(mail_sync.content_hash_matches(target, content_hash))

    def test_crlf_target_matches_mixed_line_ending_source(self):
        source = b"Subject: test\r\nX-Test: value\n\nbody\n"
        target = b"Subject: test\r\nX-Test: value\r\n\r\nbody\r\n"
        content_hash = mail_sync.hashlib.sha256(source).hexdigest()
        self.assertTrue(
            mail_sync.content_hash_matches(target, content_hash, source)
        )

    def test_mailplus_nul_rewrite_matches_source(self):
        source = b"Subject: test\r\n\r\nbinary:\x00XY+\r\n"
        target = b"Subject: test\r\n\r\nbinary:\x80XY+\r\n"
        content_hash = mail_sync.hashlib.sha256(source).hexdigest()
        self.assertTrue(
            mail_sync.content_hash_matches(target, content_hash, source)
        )

    def test_changed_content_does_not_match(self):
        source = b"Subject: test\n\nbody\n"
        target = b"Subject: test\r\n\r\nchanged\r\n"
        content_hash = mail_sync.hashlib.sha256(source).hexdigest()
        self.assertFalse(mail_sync.content_hash_matches(target, content_hash))


class SourceFetchTests(unittest.TestCase):
    def test_missing_source_body_has_specific_error(self):
        client = mock.Mock()
        client.uid.return_value = ("OK", [b"1563346595 (UID 1563346595)"])

        with self.assertRaisesRegex(
            mail_sync.SourceMessageUnavailable,
            "UID 1563346595",
        ):
            mail_sync.fetch_source_message(
                client,
                "source@example.com",
                "INBOX",
                123,
                1563346595,
            )

    def test_missing_imap_body_uses_pop_fallback(self):
        raw = b"Message-ID: <large@example.com>\r\nSubject: large\r\n\r\nbody\r\n"
        header = b"Message-ID: <large@example.com>\r\nSubject: large\r\n\r\n"
        metadata = (
            b'1 (UID 9 RFC822.SIZE '
            + str(len(raw)).encode("ascii")
            + b' INTERNALDATE "01-Jun-2026 09:38:23 +0800" FLAGS (\\Seen))'
        )
        client = mock.Mock()
        client.uid.side_effect = [
            ("OK", [None]),
            ("OK", [(metadata, header), b")"]),
        ]
        profile = {
            "email": "source@example.com",
            "password": "secret",
            "pop_fallback": {"server": "pop.example.com"},
        }
        with mock.patch.object(
            mail_sync,
            "fetch_pop_message_by_identity",
            return_value=raw,
        ) as fallback:
            message = mail_sync.fetch_source_message(
                client,
                "source@example.com",
                "INBOX",
                123,
                9,
                profile,
            )
        self.assertEqual(message.raw, raw)
        fallback.assert_called_once_with(
            profile,
            "<large@example.com>",
            len(raw),
        )

    def test_pop_stream_unstuffs_dot_lines(self):
        client = mock.Mock()
        client._getline.side_effect = [
            (b"Header: value", 15),
            (b"..dot-start", 12),
            (b".", 3),
        ]
        raw = mail_sync.pop_retrieve_raw(client, 4)
        self.assertEqual(raw, b"Header: value\r\n.dot-start\r\n")
        client._putcmd.assert_called_once_with("RETR 4")


class SourceRuleTests(unittest.TestCase):
    def test_configured_skipped_uids(self):
        folder = mail_sync.FolderInfo("INBOX", "INBOX", ())
        rule = {"skip_uids": {"INBOX": [1761199015, "1761199014"]}}
        self.assertEqual(
            mail_sync.source_folder_skipped_uids(rule, folder),
            {1761199015, 1761199014},
        )


class ImapQuotingTests(unittest.TestCase):
    def test_quotes_and_escapes_search_value(self):
        value = '<id.JavaMail."user@example.com"@host>'
        self.assertEqual(
            mail_sync.quote_imap_string(value),
            '"<id.JavaMail.\\"user@example.com\\"@host>"',
        )


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


class SourceReconnectTests(unittest.TestCase):
    def test_reconnects_after_source_autologout(self):
        original = mock.Mock()
        original.noop.side_effect = mail_sync.imaplib.IMAP4.error(
            "Autologout"
        )
        replacement = mock.Mock()
        profile = {
            "email": "source@example.com",
            "imap_server": "imap.example.com",
        }
        with (
            mock.patch.object(mail_sync, "connect", return_value=replacement),
            mock.patch.object(
                mail_sync,
                "select_mailbox",
                return_value=(10, 123),
            ) as select,
        ):
            result = mail_sync.ensure_source_connection(
                profile,
                original,
                "INBOX",
                123,
            )
        self.assertIs(result, replacement)
        select.assert_called_once_with(
            replacement,
            "INBOX",
            readonly=True,
        )


class QuarantineTests(unittest.TestCase):
    def test_quarantined_message_is_not_appended_again(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: test\r\n\r\nbody\r\n"
            content_hash = mail_sync.hashlib.sha256(raw).hexdigest()
            db.execute(
                """
                INSERT INTO pending_operations (
                    source_email, content_hash, message_id, source_folder,
                    source_uidvalidity, source_uid, source_flags, internal_date,
                    raw_message, account_label, folder_label, old_target_uid,
                    target_uidvalidity, target_uid_before, new_target_uid,
                    status, last_error, created_at, updated_at
                ) VALUES (?, ?, NULL, ?, ?, ?, '[]', NULL, ?, ?, ?,
                          NULL, ?, ?, ?, 'quarantined', ?, ?, ?)
                """,
                (
                    "source@example.com",
                    content_hash,
                    "INBOX",
                    1,
                    2,
                    raw,
                    "SYNC:source@example.com",
                    "BOX:收件箱",
                    3,
                    4,
                    5,
                    "verification failed",
                    mail_sync.now_iso(),
                    mail_sync.now_iso(),
                ),
            )
            db.commit()
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=2,
                raw=raw,
                content_hash=content_hash,
                message_id=None,
                internal_date=None,
                system_flags=(),
            )
            action = mail_sync.sync_message(
                db,
                mock.Mock(),
                "INBOX",
                message,
                "SYNC:source@example.com",
                "BOX:收件箱",
                {
                    "SYNC:source@example.com": "$account",
                    "BOX:收件箱": "$folder",
                },
                "run-id",
            )
            self.assertEqual(action, "quarantined")
            db.close()


class DuplicateLocationTests(unittest.TestCase):
    def test_second_location_adds_another_folder_label(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: duplicate\r\n\r\nbody\r\n"
            content_hash = mail_sync.hashlib.sha256(raw).hexdigest()
            first = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=content_hash,
                message_id="<duplicate@example.com>",
                internal_date=None,
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                first,
                2,
                20,
                "SYNC:source@example.com",
                ("BOX:收件箱",),
                "same-run",
            )
            mail_sync.upsert_message_location(
                db,
                first,
                "BOX:收件箱",
                "same-run",
            )
            db.commit()
            second = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="Sent",
                uidvalidity=3,
                uid=30,
                raw=raw,
                content_hash=content_hash,
                message_id="<duplicate@example.com>",
                internal_date=None,
                system_flags=(),
            )
            target = mock.Mock()
            target.uid.return_value = ("OK", [b"stored"])
            with mock.patch.object(
                mail_sync,
                "select_mailbox",
                return_value=(1, 2),
            ):
                action = mail_sync.sync_message(
                    db,
                    target,
                    "INBOX",
                    second,
                    "SYNC:source@example.com",
                    "BOX:已发邮件",
                    {
                        "SYNC:source@example.com": "$account",
                        "BOX:收件箱": "$inbox",
                        "BOX:已发邮件": "$sent",
                    },
                    "same-run",
                )
            self.assertEqual(action, "folder_label_added")
            row = db.execute(
                "SELECT * FROM messages WHERE source_email=? AND content_hash=?",
                ("source@example.com", content_hash),
            ).fetchone()
            self.assertEqual(
                mail_sync.row_folder_labels(row),
                ("BOX:已发邮件", "BOX:收件箱"),
            )
            location_count = db.execute(
                """
                SELECT COUNT(*) FROM message_locations
                WHERE source_email=? AND content_hash=?
                """,
                ("source@example.com", content_hash),
            ).fetchone()[0]
            self.assertEqual(location_count, 2)
            db.close()


class PendingMultiLabelTests(unittest.TestCase):
    def test_pending_operation_stores_all_folder_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: pending\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=2,
                raw=raw,
                content_hash=mail_sync.hashlib.sha256(raw).hexdigest(),
                message_id="<pending@example.com>",
                internal_date=None,
                system_flags=(),
            )
            with (
                mock.patch.object(
                    mail_sync,
                    "select_mailbox",
                    return_value=(10, 20),
                ),
                mock.patch.object(
                    mail_sync,
                    "search_uids",
                    return_value=[30],
                ),
            ):
                mail_sync.begin_pending_operation(
                    db,
                    mock.Mock(),
                    "INBOX",
                    message,
                    "SYNC:source@example.com",
                    ("BOX:收件箱", "BOX:已发邮件"),
                    None,
                    "BOX:收件箱",
                )
            row = db.execute(
                "SELECT * FROM pending_operations"
            ).fetchone()
            self.assertEqual(
                mail_sync.row_folder_labels(row),
                ("BOX:已发邮件", "BOX:收件箱"),
            )
            self.assertEqual(row["folder_label"], "BOX:收件箱")
            db.close()


if __name__ == "__main__":
    unittest.main()
