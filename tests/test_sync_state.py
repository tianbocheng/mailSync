import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.header import Header
from pathlib import Path
from unittest import mock


import mail_sync
from sync import pending as pending_module
from sync import engine as engine_module
from domain import message as mail_sync_message

from tests import helpers  # noqa: F401

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
    def test_upsert_state_preserves_unverified_target_header_for_same_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: date audit\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@qq.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id="<date-audit@example.com>",
                internal_date=datetime.now(timezone.utc),
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "SYNC:source@qq.com",
                ("BOX:收件箱",),
                "initial",
            )
            db.execute(
                """
                UPDATE messages SET target_date_header_verified=0
                WHERE source_email=? AND content_hash=?
                """,
                (message.source_email, message.content_hash),
            )
            db.commit()

            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "SYNC:source@qq.com",
                ("BOX:收件箱", "BOX:已发邮件"),
                "same-copy",
            )
            same_copy = db.execute(
                """
                SELECT target_date_header_verified FROM messages
                WHERE source_email=? AND content_hash=?
                """,
                (message.source_email, message.content_hash),
            ).fetchone()[0]

            mail_sync.upsert_state(
                db,
                message,
                2,
                21,
                "SYNC:source@qq.com",
                ("BOX:收件箱",),
                "rebuilt-copy",
            )
            rebuilt_copy = db.execute(
                """
                SELECT target_date_header_verified FROM messages
                WHERE source_email=? AND content_hash=?
                """,
                (message.source_email, message.content_hash),
            ).fetchone()[0]

            self.assertEqual(same_copy, 0)
            self.assertEqual(rebuilt_copy, 1)
            db.close()

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
                engine_module,
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

    def test_unchanged_location_can_defer_commit_for_batching(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: unchanged\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id="<unchanged@example.com>",
                internal_date=None,
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "SYNC:source@example.com",
                ("BOX:收件箱",),
                "initial",
            )
            mail_sync.upsert_message_location(
                db,
                message,
                "BOX:收件箱",
                "initial",
            )
            db.commit()

            action = mail_sync.sync_message(
                db,
                mock.Mock(),
                "INBOX",
                message,
                "SYNC:source@example.com",
                "BOX:收件箱",
                {
                    "SYNC:source@example.com": "$account",
                    "BOX:收件箱": "$inbox",
                },
                "batched",
                commit_state=False,
            )

            self.assertEqual(action, "source_location_added")
            self.assertTrue(db.in_transaction)
            db.commit()
            self.assertFalse(db.in_transaction)
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
                    pending_module,
                    "select_mailbox",
                    return_value=(10, 20),
                ),
                mock.patch.object(
                    pending_module,
                    "response_int",
                    return_value=31,
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
