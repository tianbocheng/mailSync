import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.header import Header
from pathlib import Path
from unittest import mock


import mail_sync
from domain import message as mail_sync_message
from imap import probe as probe_module

from tests import helpers  # noqa: F401

class MessageDateTests(unittest.TestCase):
    def test_lightweight_metadata_fetch_is_batched_and_parsed(self):
        client = mock.Mock()
        client.uid.return_value = (
            "OK",
            [
                (
                    b'1 (UID 9 INTERNALDATE "10-Nov-2020 15:44:03 +0800" '
                    b"FLAGS (\\Seen))",
                    None,
                )
            ],
        )

        result = mail_sync.fetch_message_metadata(client, [9])

        self.assertEqual(
            result[9],
            (
                datetime.fromisoformat("2020-11-10T15:44:03+08:00"),
                ("\\Seen",),
            ),
        )

    def test_new_state_database_marks_existing_dates_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            try:
                columns = {
                    row[1]
                    for row in db.execute(
                        "PRAGMA table_info(messages)"
                    ).fetchall()
                }
                schema_version = db.execute("PRAGMA user_version").fetchone()[0]
            finally:
                db.close()

        self.assertIn("source_date_verified", columns)
        self.assertIn("target_date_header_verified", columns)
        self.assertIn("target_normalized_hash", columns)
        self.assertEqual(schema_version, mail_sync.DB_SCHEMA_VERSION)

    def test_newer_state_database_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            db = mail_sync.sqlite3.connect(path)
            db.execute(f"PRAGMA user_version={mail_sync.DB_SCHEMA_VERSION + 1}")
            db.close()

            with self.assertRaisesRegex(mail_sync.SyncError, "newer"):
                mail_sync.init_db(path)

    def test_date_header_presence_is_batched_and_parsed(self):
        client = mock.Mock()
        client.uid.return_value = (
            "OK",
            [
                (
                    b"1 (UID 9 BODY[HEADER.FIELDS (DATE)] {39}",
                    b"Date: Tue, 10 Nov 2020 15:44:03 +0800\r\n\r\n",
                ),
                (
                    b"2 (UID 10 BODY[HEADER.FIELDS (DATE)] {2}",
                    b"\r\n",
                ),
            ],
        )

        result = mail_sync.fetch_date_header_presence(client, [9, 10])

        self.assertEqual(result, {9: True, 10: False})

    def test_split_fetch_response_preserves_trailing_internal_date_and_flags(self):
        rows = [
            (b"25 (UID 5408 BODY[] {4}", b"body"),
            b' INTERNALDATE "10-Nov-2020 15:44:03 +0800" FLAGS (\\Seen))',
        ]

        metadata, raw = mail_sync.parse_fetch_tuple(rows)

        self.assertEqual(raw, b"body")
        self.assertEqual(
            mail_sync.parse_internal_date(metadata),
            datetime.fromisoformat("2020-11-10T15:44:03+08:00"),
        )
        self.assertEqual(mail_sync.parse_flags(metadata), ("\\Seen",))

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

    def test_display_time_feature_detects_append_time_internaldate(self):
        result = {
            "ok": True,
            "message_date": datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
            "mailbox_internal_date": datetime(2026, 7, 9, 2, 10, 14, tzinfo=timezone.utc),
        }

        self.assertTrue(probe_module._display_time_uses_append_time(result))

    def test_display_time_feature_allows_matching_internaldate(self):
        result = {
            "ok": True,
            "message_date": datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
            "mailbox_internal_date": datetime(2001, 2, 3, 4, 5, 30, tzinfo=timezone.utc),
        }

        self.assertFalse(probe_module._display_time_uses_append_time(result))

    def test_naive_date_header_is_treated_as_utc(self):
        result = mail_sync.parse_message_date(
            b"Date: Mon, 8 May 2023 15:05:39\r\n\r\n"
        )

        self.assertEqual(result.tzinfo, timezone.utc)

    def test_invalid_date_header_returns_none(self):
        self.assertIsNone(
            mail_sync.parse_message_date(b"Date: definitely-not-a-date\r\n\r\n")
        )

    def test_header_object_date_is_converted_to_string(self):
        parsed_message = mock.Mock()
        parsed_message.get.return_value = Header(
            "Mon, 8 May 2023 15:05:39 +0800"
        )
        with mock.patch.object(
            mail_sync_message,
            "message_from_bytes",
            return_value=parsed_message,
        ):
            result = mail_sync.parse_message_date(b"ignored")

        self.assertEqual(
            result,
            datetime.fromisoformat("2023-05-08T15:05:39+08:00"),
        )

class QQDateCompatibilityTests(unittest.TestCase):
    def message(self, raw, source_email="source@qq.com"):
        return mail_sync.SourceMessage(
            source_email=source_email,
            folder="INBOX",
            uidvalidity=1,
            uid=2,
            raw=raw,
            content_hash=mail_sync.hashlib.sha256(raw).hexdigest(),
            message_id=None,
            internal_date=datetime.fromisoformat(
                "2020-11-10T15:44:03+08:00"
            ),
            system_flags=(),
        )

    def test_missing_date_on_qq_system_message_is_synthesized(self):
        raw = (
            b"X-QQ-mid: newapiserver8t1604994243t17624\r\n"
            b"Subject: test\r\n\r\nbody\r\n"
        )

        message = self.message(raw)
        self.assertTrue(mail_sync.needs_qq_date_header(message))
        target_raw = mail_sync.target_message_raw(message)

        self.assertTrue(
            target_raw.startswith(
                b"Date: Tue, 10 Nov 2020 15:44:03 +0800\r\n"
            )
        )
        self.assertEqual(target_raw.split(b"\r\n", 1)[1], raw)

    def test_existing_date_is_preserved_byte_for_byte(self):
        raw = (
            b"X-QQ-mid: value\r\n"
            b"Date: Tue, 10 Nov 2020 15:44:03 +0800\r\n"
            b"Subject: test\r\n\r\nbody\r\n"
        )

        message = self.message(raw)
        self.assertFalse(mail_sync.needs_qq_date_header(message))
        self.assertEqual(mail_sync.target_message_raw(message), raw)

    def test_non_qq_headerless_message_is_not_modified(self):
        raw = b"Subject: test\r\n\r\nbody\r\n"

        self.assertEqual(
            mail_sync.target_message_raw(
                self.message(raw, "source@example.com")
            ),
            raw,
        )

    def test_source_feature_can_enable_missing_date_synthesis(self):
        raw = b"Subject: test\r\n\r\nbody\r\n"
        message = self.message(raw, "source@example.com")
        message = mail_sync.SourceMessage(
            **{
                **message.__dict__,
                "source_features": {"synthesize_missing_date_header": True},
            }
        )

        self.assertTrue(
            mail_sync.target_message_raw(message).startswith(b"Date: ")
        )

    def test_source_feature_can_disable_qq_missing_date_synthesis(self):
        raw = b"Subject: test\r\n\r\nbody\r\n"
        message = self.message(raw, "source@qq.com")
        message = mail_sync.SourceMessage(
            **{
                **message.__dict__,
                "source_features": {"synthesize_missing_date_header": False},
            }
        )

        self.assertEqual(mail_sync.target_message_raw(message), raw)

if __name__ == "__main__":
    unittest.main()
