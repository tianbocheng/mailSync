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
from sync import reconcile as reconcile_module
from sync import recovery as recovery_module
from storage.config_db import (
    DEFAULT_ADMIN_USERNAME,
    PASSWORD_ALGORITHM,
    verify_password,
)
from domain import message as mail_sync_message

from tests import helpers  # noqa: F401

class LoginUserSchemaTests(unittest.TestCase):
    def test_init_db_creates_default_admin_user_with_hashed_password(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            try:
                row = db.execute(
                    """
                    SELECT username, password_hash, password_salt,
                           password_algorithm, password_iterations, role,
                           enabled
                    FROM login_users
                    WHERE username=?
                    """,
                    (DEFAULT_ADMIN_USERNAME,),
                ).fetchone()
            finally:
                db.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["password_algorithm"], PASSWORD_ALGORITHM)
        self.assertEqual(row["role"], "admin")
        self.assertEqual(row["enabled"], 1)
        self.assertNotEqual(row["password_hash"], "TEST_ADMIN_PASSWORD")
        self.assertTrue(
            verify_password(
                "TEST_ADMIN_PASSWORD",
                row["password_salt"],
                row["password_hash"],
                row["password_iterations"],
            )
        )


class QuarantineTests(unittest.TestCase):
    def _insert_pending_operation(
        self,
        db,
        raw: bytes,
        *,
        source_uid: int = 2,
        old_target_uid=None,
        target_uid_before: int = 4,
        new_target_uid=None,
        status: str = "pending",
    ) -> str:
        content_hash = mail_sync.hashlib.sha256(raw).hexdigest()
        db.execute(
            """
            INSERT INTO pending_operations (
                source_email, content_hash, message_id, source_folder,
                source_uidvalidity, source_uid, source_flags, internal_date,
                raw_message, account_label, folder_label, old_target_uid,
                target_uidvalidity, target_uid_before, new_target_uid,
                status, last_error, created_at, updated_at, target_folder
            ) VALUES (?, ?, NULL, ?, ?, ?, '[]', NULL, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "source@example.com",
                content_hash,
                "INBOX",
                1,
                source_uid,
                raw,
                "SYNC:source@example.com",
                "BOX:收件箱",
                old_target_uid,
                3,
                target_uid_before,
                new_target_uid,
                status,
                "verification failed" if status == "quarantined" else None,
                mail_sync.now_iso(),
                mail_sync.now_iso(),
                "INBOX",
            ),
        )
        db.commit()
        return content_hash

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

    def test_quarantined_pending_operation_can_be_recovered_after_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: recovered\r\n\r\nbody\r\n"
            content_hash = mail_sync.hashlib.sha256(raw).hexdigest()
            db.execute(
                """
                INSERT INTO pending_operations (
                    source_email, content_hash, message_id, source_folder,
                    source_uidvalidity, source_uid, source_flags, internal_date,
                    raw_message, account_label, folder_label, old_target_uid,
                    target_uidvalidity, target_uid_before, new_target_uid,
                    status, last_error, created_at, updated_at, target_folder
                ) VALUES (?, ?, NULL, ?, ?, ?, '[]', NULL, ?, ?, ?,
                          NULL, ?, ?, ?, 'quarantined', ?, ?, ?, ?)
                """,
                (
                    "source@example.com",
                    content_hash,
                    "Sent",
                    1,
                    2,
                    raw,
                    "SYNC:source@example.com",
                    "BOX:已发邮件",
                    3,
                    4,
                    5,
                    "verification failed",
                    mail_sync.now_iso(),
                    mail_sync.now_iso(),
                    "INBOX",
                ),
            )
            db.commit()
            with (
                mock.patch.object(
                    recovery_module,
                    "select_mailbox",
                    return_value=(10, 3),
                ),
                mock.patch.object(
                    recovery_module,
                    "repair_target_copy",
                    return_value=True,
                ) as repair,
            ):
                recovered = mail_sync.recover_pending_operations(
                    db,
                    mock.Mock(),
                    "INBOX",
                    {
                        "SYNC:source@example.com": "$account",
                        "BOX:已发邮件": "$folder",
                    },
                    source_emails=("source@example.com",),
                    include_quarantined=True,
                )

            self.assertEqual(recovered, 1)
            repair.assert_called_once()
            self.assertEqual(repair.call_args.args[1], "INBOX")
            self.assertEqual(repair.call_args.args[2], 5)
            row = db.execute(
                "SELECT * FROM messages WHERE source_email=? AND content_hash=?",
                ("source@example.com", content_hash),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["last_sync_action"], "pending_operation_recovered")
            pending = db.execute("SELECT * FROM pending_operations").fetchone()
            self.assertIsNone(pending)
            db.close()

    def test_pending_recovery_tries_old_target_uid_before_candidate_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: reuse old\r\n\r\nbody\r\n"
            content_hash = self._insert_pending_operation(
                db,
                raw,
                old_target_uid=9,
                target_uid_before=100,
            )
            with (
                mock.patch.object(
                    recovery_module,
                    "select_mailbox",
                    return_value=(10, 3),
                ),
                mock.patch.object(
                    recovery_module,
                    "repair_target_copy",
                    return_value=True,
                ) as repair,
                mock.patch.object(recovery_module, "search_uids") as search,
            ):
                recovered = mail_sync.recover_pending_operations(
                    db,
                    mock.Mock(),
                    "INBOX",
                    {
                        "SYNC:source@example.com": "$account",
                        "BOX:收件箱": "$folder",
                    },
                    source_emails=("source@example.com",),
                )

            self.assertEqual(recovered, 1)
            repair.assert_called_once()
            self.assertEqual(repair.call_args.args[2], 9)
            search.assert_not_called()
            row = db.execute(
                "SELECT target_uid FROM messages WHERE content_hash=?",
                (content_hash,),
            ).fetchone()
            self.assertEqual(row["target_uid"], 9)
            self.assertIsNone(db.execute("SELECT 1 FROM pending_operations").fetchone())
            db.close()

    def test_quarantined_pending_without_new_uid_does_not_scan_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            self._insert_pending_operation(
                db,
                b"Subject: quarantined\r\n\r\nbody\r\n",
                target_uid_before=100,
                status="quarantined",
            )
            with (
                mock.patch.object(
                    recovery_module,
                    "select_mailbox",
                    return_value=(10, 3),
                ),
                mock.patch.object(recovery_module, "repair_target_copy") as repair,
                mock.patch.object(recovery_module, "search_uids") as search,
            ):
                recovered = mail_sync.recover_pending_operations(
                    db,
                    mock.Mock(),
                    "INBOX",
                    {
                        "SYNC:source@example.com": "$account",
                        "BOX:收件箱": "$folder",
                    },
                    source_emails=("source@example.com",),
                    include_quarantined=True,
                )

            self.assertEqual(recovered, 0)
            repair.assert_not_called()
            search.assert_not_called()
            row = db.execute("SELECT status FROM pending_operations").fetchone()
            self.assertEqual(row["status"], "quarantined")
            db.close()

    def test_pending_recovery_quarantines_excessive_candidate_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            self._insert_pending_operation(
                db,
                b"Subject: many candidates\r\n\r\nbody\r\n",
                target_uid_before=10,
            )
            with (
                mock.patch.object(
                    recovery_module,
                    "select_mailbox",
                    return_value=(10, 3),
                ),
                mock.patch.object(
                    recovery_module,
                    "search_uids",
                    return_value=list(range(11, 63)),
                ),
                mock.patch.object(recovery_module, "repair_target_copy") as repair,
            ):
                recovered = mail_sync.recover_pending_operations(
                    db,
                    mock.Mock(),
                    "INBOX",
                    {
                        "SYNC:source@example.com": "$account",
                        "BOX:收件箱": "$folder",
                    },
                    source_emails=("source@example.com",),
                )

            self.assertEqual(recovered, 0)
            repair.assert_not_called()
            row = db.execute(
                """
                SELECT status, new_target_uid, last_error
                FROM pending_operations
                """
            ).fetchone()
            self.assertEqual(row["status"], "quarantined")
            self.assertEqual(row["new_target_uid"], 62)
            self.assertIn("52 target candidates", row["last_error"])
            db.close()

class DuplicateLocationTests(unittest.TestCase):
    def test_new_message_can_sync_without_folder_label(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: no labels\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="Junk",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id=None,
                internal_date=None,
                system_flags=(),
            )
            target = mock.Mock()
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
                mock.patch.object(
                    engine_module,
                    "append_target_copy",
                    return_value=(20, 31),
                ) as append,
            ):
                action = mail_sync.sync_message(
                    db,
                    target,
                    "Archived",
                    message,
                    "SYNC:source@example.com",
                    "",
                    {"SYNC:source@example.com": "$account"},
                    "run-id",
                )

            self.assertEqual(action, "created")
            self.assertEqual(append.call_args.args[3], ["$account"])
            row = db.execute(
                "SELECT * FROM messages WHERE source_email=? AND content_hash=?",
                ("source@example.com", message.content_hash),
            ).fetchone()
            self.assertEqual(mail_sync.row_folder_labels(row), ())
            self.assertEqual(row["folder_label"], "")
            self.assertEqual(row["target_folder"], "Archived")
            location = db.execute(
                "SELECT * FROM message_locations WHERE source_email=?",
                ("source@example.com",),
            ).fetchone()
            self.assertEqual(location["folder_label"], "")
            pending = db.execute("SELECT * FROM pending_operations").fetchone()
            self.assertIsNone(pending)
            db.close()

    def test_new_message_can_sync_without_any_label(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: no labels at all\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=11,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id=None,
                internal_date=None,
                system_flags=(),
            )
            target = mock.Mock()
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
                mock.patch.object(
                    engine_module,
                    "append_target_copy",
                    return_value=(20, 31),
                ) as append,
            ):
                action = mail_sync.sync_message(
                    db,
                    target,
                    "INBOX",
                    message,
                    "",
                    "",
                    {},
                    "run-id",
                )

            self.assertEqual(action, "created")
            self.assertEqual(append.call_args.args[3], [])
            row = db.execute(
                "SELECT * FROM messages WHERE source_email=? AND content_hash=?",
                ("source@example.com", message.content_hash),
            ).fetchone()
            self.assertEqual(row["account_label"], "")
            self.assertEqual(mail_sync.row_folder_labels(row), ())
            db.close()

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

    def test_reconcile_uses_current_multi_label_folder_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: relabel\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id="<relabel@example.com>",
                internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "",
                ("BOX:收件箱",),
                "old-run",
                target_folder="INBOX",
            )
            mail_sync.upsert_message_location(
                db,
                message,
                "BOX:收件箱",
                "current-run",
            )
            db.commit()

            with (
                mock.patch.object(reconcile_module, "select_mailbox"),
                mock.patch.object(
                    reconcile_module,
                    "target_fetch",
                    return_value=(b"", raw),
                ),
                mock.patch.object(reconcile_module, "ensure_target_delete_will_be_safe"),
                mock.patch.object(
                    pending_module,
                    "select_mailbox",
                    return_value=(2, 30),
                ),
                mock.patch.object(
                    pending_module,
                    "response_int",
                    return_value=31,
                ),
                mock.patch.object(
                    reconcile_module,
                    "append_target_copy",
                    return_value=(2, 31),
                ) as append,
                mock.patch.object(reconcile_module, "delete_target_copy") as delete,
            ):
                rebuilt = mail_sync.reconcile_deleted_messages(
                    db,
                    mock.Mock(),
                    "INBOX",
                    {
                        "email": "source@example.com",
                        "folder_labels": {
                            "INBOX": ["BOX:收件箱", "SYNC:source@example.com"],
                        },
                    },
                    {
                        "BOX:收件箱": "$inbox",
                        "SYNC:source@example.com": "$account",
                    },
                    "current-run",
                )

            self.assertEqual(rebuilt, 1)
            self.assertCountEqual(append.call_args.args[3], ["$inbox", "$account"])
            delete.assert_called_once()
            row = db.execute(
                """
                SELECT * FROM messages
                WHERE source_email=? AND content_hash=?
                """,
                ("source@example.com", message.content_hash),
            ).fetchone()
            self.assertEqual(row["last_sync_action"], "source_labels_rebuilt")
            self.assertCountEqual(
                mail_sync.row_folder_labels(row),
                ("BOX:收件箱", "SYNC:source@example.com"),
            )
            self.assertEqual(row["target_uid"], 31)
            db.close()

    def test_reconcile_skips_unreadable_target_body(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: relabel\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id="<relabel@example.com>",
                internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "",
                ("BOX:收件箱",),
                "old-run",
                target_folder="INBOX",
            )
            mail_sync.upsert_message_location(
                db,
                message,
                "BOX:收件箱",
                "current-run",
            )
            db.commit()

            with (
                mock.patch.object(reconcile_module, "select_mailbox"),
                mock.patch.object(
                    reconcile_module,
                    "target_fetch",
                    side_effect=mail_sync.SyncError(
                        "IMAP FETCH did not return a message body"
                    ),
                ) as target_fetch,
                mock.patch.object(
                    reconcile_module,
                    "append_target_copy",
                ) as append,
                mock.patch.object(reconcile_module, "delete_target_copy") as delete,
            ):
                rebuilt = mail_sync.reconcile_deleted_messages(
                    db,
                    mock.Mock(),
                    "INBOX",
                    {
                        "email": "source@example.com",
                        "folder_labels": {
                            "INBOX": ["BOX:收件箱", "SYNC:source@example.com"],
                        },
                    },
                    {
                        "BOX:收件箱": "$inbox",
                        "SYNC:source@example.com": "$account",
                    },
                    "current-run",
                )

            self.assertEqual(rebuilt, 0)
            target_fetch.assert_called_once()
            append.assert_not_called()
            delete.assert_not_called()
            row = db.execute(
                """
                SELECT * FROM messages
                WHERE source_email=? AND content_hash=?
                """,
                ("source@example.com", message.content_hash),
            ).fetchone()
            self.assertEqual(row["last_sync_action"], "source_reconciliation_skipped")
            self.assertEqual(row["target_uid"], 20)
            self.assertCountEqual(
                mail_sync.row_folder_labels(row),
                ("BOX:收件箱",),
            )
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
            row = db.execute(
                "SELECT * FROM messages WHERE source_email=? AND content_hash=?",
                ("source@example.com", message.content_hash),
            ).fetchone()
            self.assertEqual(row["target_folder"], "INBOX")
            db.commit()
            self.assertFalse(db.in_transaction)
            db.close()

    def test_existing_message_keeps_stored_target_folder_when_rule_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: historical\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id="<historical@example.com>",
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
                target_folder="INBOX",
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
                "Archived",
                message,
                "SYNC:source@example.com",
                "BOX:收件箱",
                {
                    "SYNC:source@example.com": "$account",
                    "BOX:收件箱": "$inbox",
                },
                "after-rule-change",
            )

            self.assertEqual(action, "source_location_added")
            row = db.execute(
                "SELECT * FROM messages WHERE source_email=? AND content_hash=?",
                ("source@example.com", message.content_hash),
            ).fetchone()
            self.assertEqual(row["target_folder"], "INBOX")
            db.close()

    def test_reconcile_out_of_scope_message_keeps_target_when_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: moved out\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id="<moved-out@example.com>",
                internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "SYNC:source@example.com",
                ("BOX:收件",),
                "old-run",
                target_folder="INBOX",
            )
            mail_sync.upsert_message_location(
                db,
                message,
                "BOX:收件",
                "old-run",
            )
            db.commit()

            target = mock.Mock()
            rebuilt = mail_sync.reconcile_deleted_messages(
                db,
                target,
                "INBOX",
                {
                    "email": "source@example.com",
                    "account_label": "SYNC:source@example.com",
                    "out_of_scope_policy": "keep",
                },
                {
                    "SYNC:source@example.com": "$account",
                    "BOX:收件": "$inbox",
                },
                "current-run",
            )

            self.assertEqual(rebuilt, 0)
            target.assert_not_called()
            row = db.execute(
                """
                SELECT * FROM messages
                WHERE source_email=? AND content_hash=?
                """,
                ("source@example.com", message.content_hash),
            ).fetchone()
            self.assertEqual(row["source_folder"], "__out_of_scope__")
            self.assertEqual(row["last_sync_action"], "source_out_of_scope")
            self.assertEqual(mail_sync.row_folder_labels(row), ("BOX:收件",))
            self.assertEqual(row["target_uid"], 20)
            db.close()

    def test_reconcile_out_of_scope_message_marks_configured_label(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: moved out\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id="<moved-out@example.com>",
                internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "SYNC:source@example.com",
                ("BOX:收件",),
                "old-run",
                target_folder="INBOX",
            )
            mail_sync.upsert_message_location(
                db,
                message,
                "BOX:收件",
                "old-run",
            )
            db.commit()

            with (
                mock.patch.object(reconcile_module, "select_mailbox"),
                mock.patch.object(
                    reconcile_module,
                    "target_fetch",
                    return_value=(b"", raw),
                ),
                mock.patch.object(reconcile_module, "ensure_target_delete_will_be_safe"),
                mock.patch.object(
                    pending_module,
                    "select_mailbox",
                    return_value=(2, 30),
                ),
                mock.patch.object(
                    pending_module,
                    "response_int",
                    return_value=31,
                ),
                mock.patch.object(
                    reconcile_module,
                    "append_target_copy",
                    return_value=(2, 31),
                ) as append,
                mock.patch.object(reconcile_module, "delete_target_copy") as delete,
            ):
                rebuilt = mail_sync.reconcile_deleted_messages(
                    db,
                    mock.Mock(),
                    "INBOX",
                    {
                        "email": "source@example.com",
                        "account_label": "SYNC:source@example.com",
                        "out_of_scope_policy": "label",
                        "out_of_scope_label": "BOX:移出范围",
                    },
                    {
                        "SYNC:source@example.com": "$account",
                        "BOX:收件": "$inbox",
                        "BOX:移出范围": "$out",
                    },
                    "current-run",
                )

            self.assertEqual(rebuilt, 1)
            self.assertCountEqual(append.call_args.args[3], ["$account", "$out"])
            delete.assert_called_once()
            row = db.execute(
                """
                SELECT * FROM messages
                WHERE source_email=? AND content_hash=?
                """,
                ("source@example.com", message.content_hash),
            ).fetchone()
            self.assertEqual(row["source_folder"], "__out_of_scope__")
            self.assertEqual(row["last_sync_action"], "source_out_of_scope_labeled")
            self.assertEqual(mail_sync.row_folder_labels(row), ("BOX:移出范围",))
            self.assertEqual(row["target_uid"], 31)
            self.assertIsNone(db.execute("SELECT * FROM pending_operations").fetchone())
            db.close()

    def test_reconcile_out_of_scope_message_marks_multiple_configured_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: moved out multi\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id="<moved-out-multi@example.com>",
                internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "SYNC:source@example.com",
                ("BOX:收件",),
                "old-run",
                target_folder="INBOX",
            )
            mail_sync.upsert_message_location(
                db,
                message,
                "BOX:收件",
                "old-run",
            )
            db.commit()

            with (
                mock.patch.object(reconcile_module, "select_mailbox"),
                mock.patch.object(
                    reconcile_module,
                    "target_fetch",
                    return_value=(b"", raw),
                ),
                mock.patch.object(reconcile_module, "ensure_target_delete_will_be_safe"),
                mock.patch.object(
                    pending_module,
                    "select_mailbox",
                    return_value=(2, 30),
                ),
                mock.patch.object(
                    pending_module,
                    "response_int",
                    return_value=31,
                ),
                mock.patch.object(
                    reconcile_module,
                    "append_target_copy",
                    return_value=(2, 31),
                ) as append,
                mock.patch.object(reconcile_module, "delete_target_copy"),
            ):
                rebuilt = mail_sync.reconcile_deleted_messages(
                    db,
                    mock.Mock(),
                    "INBOX",
                    {
                        "email": "source@example.com",
                        "account_label": "SYNC:source@example.com",
                        "out_of_scope_policy": "label",
                        "out_of_scope_labels": ["BOX:移出范围", "BOX:归档"],
                    },
                    {
                        "SYNC:source@example.com": "$account",
                        "BOX:收件": "$inbox",
                        "BOX:移出范围": "$out",
                        "BOX:归档": "$archive",
                    },
                    "current-run",
                )

            self.assertEqual(rebuilt, 1)
            self.assertCountEqual(
                append.call_args.args[3],
                ["$account", "$out", "$archive"],
            )
            row = db.execute(
                """
                SELECT * FROM messages
                WHERE source_email=? AND content_hash=?
                """,
                ("source@example.com", message.content_hash),
            ).fetchone()
            self.assertEqual(row["last_sync_action"], "source_out_of_scope_labeled")
            self.assertCountEqual(
                mail_sync.row_folder_labels(row),
                ("BOX:移出范围", "BOX:归档"),
            )
            self.assertEqual(row["target_uid"], 31)
            self.assertIsNone(db.execute("SELECT * FROM pending_operations").fetchone())
            db.close()

    def test_reconcile_out_of_scope_message_deletes_target_once(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: moved out delete\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id="<moved-out-delete@example.com>",
                internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "SYNC:source@example.com",
                ("BOX:收件",),
                "old-run",
                target_folder="INBOX",
            )
            mail_sync.upsert_message_location(
                db,
                message,
                "BOX:收件",
                "old-run",
            )
            db.commit()

            source_rule = {
                "email": "source@example.com",
                "out_of_scope_policy": "delete",
            }
            with mock.patch.object(reconcile_module, "delete_target_copy") as delete:
                rebuilt = mail_sync.reconcile_deleted_messages(
                    db,
                    mock.Mock(),
                    "INBOX",
                    source_rule,
                    {},
                    "current-run",
                )

            self.assertEqual(rebuilt, 1)
            delete.assert_called_once()
            self.assertEqual(delete.call_args.args[1:], ("INBOX", 20))
            row = db.execute(
                """
                SELECT * FROM messages
                WHERE source_email=? AND content_hash=?
                """,
                ("source@example.com", message.content_hash),
            ).fetchone()
            self.assertEqual(row["source_folder"], "__out_of_scope__")
            self.assertEqual(row["last_sync_action"], "source_out_of_scope_deleted")
            self.assertEqual(mail_sync.row_folder_labels(row), ())
            self.assertEqual(row["target_uidvalidity"], 0)
            self.assertEqual(row["target_uid"], 0)

            with mock.patch.object(reconcile_module, "delete_target_copy") as delete:
                rebuilt = mail_sync.reconcile_deleted_messages(
                    db,
                    mock.Mock(),
                    "INBOX",
                    source_rule,
                    {},
                    "next-run",
                )
            self.assertEqual(rebuilt, 0)
            delete.assert_not_called()

            restored = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=3,
                uid=30,
                raw=raw,
                content_hash=message.content_hash,
                message_id="<moved-out-delete@example.com>",
                internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
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
                mock.patch.object(
                    engine_module,
                    "find_untracked_target_by_message_id",
                    return_value=None,
                ),
                mock.patch.object(
                    engine_module,
                    "append_target_copy",
                    return_value=(20, 31),
                ),
            ):
                action = mail_sync.sync_message(
                    db,
                    mock.Mock(),
                    "INBOX",
                    restored,
                    "",
                    "BOX:收件",
                    {"BOX:收件": "$inbox"},
                    "restored-run",
                )

            self.assertEqual(action, "created")
            row = db.execute(
                """
                SELECT * FROM messages
                WHERE source_email=? AND content_hash=?
                """,
                ("source@example.com", message.content_hash),
            ).fetchone()
            self.assertEqual(row["source_folder"], "INBOX")
            self.assertEqual(row["target_uid"], 31)
            self.assertEqual(mail_sync.row_folder_labels(row), ("BOX:收件",))
            db.close()

class TargetIdentityTests(unittest.TestCase):
    def test_reconciliation_skips_legacy_rows_without_target_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: legacy-target\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id=None,
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
                "old-run",
                target_folder="INBOX",
            )
            mail_sync.upsert_message_location(
                db,
                message,
                "BOX:收件箱",
                "old-run",
            )
            db.commit()

            with mock.patch.object(reconcile_module, "select_mailbox") as select:
                rebuilt = mail_sync.reconcile_deleted_messages(
                    db,
                    mock.Mock(),
                    "Archive",
                    {
                        "email": "source@example.com",
                        "deleted_label": "BOX:已删除邮件",
                        "account_label": "SYNC:source@example.com",
                    },
                    {
                        "SYNC:source@example.com": "$account",
                        "BOX:已删除邮件": "$deleted",
                    },
                    "current-run",
                    "new-target@example.com",
                    "imap.new.example.com",
                )

            self.assertEqual(rebuilt, 0)
            select.assert_not_called()
            remaining_locations = db.execute(
                """
                SELECT COUNT(*) FROM message_locations
                WHERE source_email=? AND content_hash=?
                """,
                (message.source_email, message.content_hash),
            ).fetchone()[0]
            self.assertEqual(remaining_locations, 1)
            db.close()

    def test_existing_row_for_different_target_is_resynced_to_current_target(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: moved-target\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="Sent",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id=None,
                internal_date=None,
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "SYNC:source@example.com",
                ("BOX:已发邮件",),
                "old-run",
                target_folder="INBOX",
                target_email="old-target@example.com",
                target_service_address="imap.old.example.com",
            )
            mail_sync.upsert_message_location(
                db,
                message,
                "BOX:已发邮件",
                "old-run",
            )
            db.commit()

            with (
                mock.patch.object(
                    pending_module,
                    "select_mailbox",
                    return_value=(10, 30),
                ),
                mock.patch.object(
                    pending_module,
                    "response_int",
                    return_value=41,
                ),
                mock.patch.object(
                    engine_module,
                    "append_target_copy",
                    return_value=(30, 41),
                ) as append,
                mock.patch.object(engine_module, "select_mailbox") as select,
            ):
                action = mail_sync.sync_message(
                    db,
                    mock.Mock(),
                    "Archive",
                    message,
                    "SYNC:source@example.com",
                    "BOX:已发邮件",
                    {
                        "SYNC:source@example.com": "$account",
                        "BOX:已发邮件": "$sent",
                    },
                    "current-run",
                    target_email="new-target@example.com",
                    target_service_address="imap.new.example.com",
                )

            self.assertEqual(action, "created")
            select.assert_not_called()
            self.assertEqual(append.call_args.args[1], "Archive")
            row = db.execute(
                """
                SELECT target_uidvalidity, target_uid, target_folder,
                       target_email, target_service_address
                FROM messages
                WHERE source_email=? AND content_hash=?
                """,
                (message.source_email, message.content_hash),
            ).fetchone()
            self.assertEqual(row["target_uidvalidity"], 30)
            self.assertEqual(row["target_uid"], 41)
            self.assertEqual(row["target_folder"], "Archive")
            self.assertEqual(row["target_email"], "new-target@example.com")
            self.assertEqual(
                row["target_service_address"],
                "imap.new.example.com",
            )
            db.close()


class SourceUnavailableSeenTests(unittest.TestCase):
    def test_known_unavailable_source_uid_keeps_location_seen(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            raw = b"Subject: unavailable\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id="<unavailable@example.com>",
                internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "SYNC:source@example.com",
                ("BOX:收件箱",),
                "old-run",
                target_folder="INBOX",
                target_email="target@example.com",
                target_service_address="imap.target",
            )
            mail_sync.upsert_message_location(
                db,
                message,
                "BOX:收件箱",
                "old-run",
            )
            db.execute(
                """
                UPDATE messages
                SET source_date_verified=0
                WHERE source_email=? AND content_hash=?
                """,
                (message.source_email, message.content_hash),
            )
            db.commit()
            db.close()

            target = {
                "email": "target@example.com",
                "service_address": "imap.target",
                "folder": "INBOX",
            }
            config = {
                "target": target,
                "synology_labels": {
                    "$account": "SYNC:source@example.com",
                    "$inbox": "BOX:收件箱",
                },
                "sources": [
                    {
                        "email": "source@example.com",
                        "account_label": "SYNC:source@example.com",
                        "default_folder_label": "BOX:收件箱",
                    }
                ],
            }
            profiles = {
                "source@example.com": {"email": "source@example.com"},
                "target@example.com": target,
            }
            labels_by_name = {
                "SYNC:source@example.com": "$account",
                "BOX:收件箱": "$inbox",
            }

            with (
                mock.patch.object(
                    mail_sync,
                    "connect",
                    side_effect=[mock.Mock(), mock.Mock()],
                ),
                mock.patch.object(
                    mail_sync,
                    "recover_pending_operations",
                    return_value=0,
                ),
                mock.patch.object(
                    mail_sync,
                    "list_folders",
                    return_value=[mail_sync.FolderInfo("INBOX", "INBOX", ())],
                ),
                mock.patch.object(
                    mail_sync,
                    "select_mailbox",
                    return_value=(1, 1),
                ),
                mock.patch.object(mail_sync, "search_uids", return_value=[10]),
                mock.patch.object(
                    mail_sync,
                    "fetch_source_message",
                    side_effect=mail_sync.SourceMessageUnavailable(
                        "Source message has no fetchable body"
                    ),
                ),
                mock.patch.object(
                    mail_sync,
                    "reconcile_deleted_messages",
                    return_value=0,
                ),
            ):
                mail_sync.command_sync(
                    config,
                    profiles,
                    labels_by_name,
                    state_path,
                    "source@example.com",
                    None,
                    None,
                    True,
                )

            db = mail_sync.init_db(state_path)
            try:
                location = db.execute(
                    """
                    SELECT last_seen_run
                    FROM message_locations
                    WHERE source_email=? AND content_hash=?
                    """,
                    (message.source_email, message.content_hash),
                ).fetchone()
                row = db.execute(
                    """
                    SELECT last_sync_action, last_seen_run
                    FROM messages
                    WHERE source_email=? AND content_hash=?
                    """,
                    (message.source_email, message.content_hash),
                ).fetchone()
            finally:
                db.close()

            self.assertIsNotNone(location)
            self.assertNotEqual(location["last_seen_run"], "old-run")
            self.assertEqual(
                row["last_sync_action"],
                "source_message_unavailable_seen",
            )
            self.assertNotEqual(row["last_seen_run"], "old-run")


class FolderWatermarkScanTests(unittest.TestCase):
    def test_full_scan_skips_unchanged_folder_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            raw = b"Subject: cached\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id="<cached@example.com>",
                internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "SYNC:source@example.com",
                ("BOX:收件箱",),
                "old-run",
                target_folder="INBOX",
                target_email="target@example.com",
                target_service_address="imap.target",
            )
            mail_sync.upsert_message_location(
                db,
                message,
                "BOX:收件箱",
                "old-run",
            )
            rule = {
                "email": "source@example.com",
                "account_label": "SYNC:source@example.com",
                "default_folder_label": "BOX:收件箱",
            }
            labels_by_name = {
                "SYNC:source@example.com": "$account",
                "BOX:收件箱": "$inbox",
            }
            signature = mail_sync._folder_scan_signature(
                rule,
                ("BOX:收件箱",),
                set(),
                "INBOX",
                labels_by_name,
            )
            mail_sync.upsert_folder_scan_state(
                db,
                "source@example.com",
                "INBOX",
                (1, 11, 1),
                "old-run",
                signature,
                "target@example.com",
                "imap.target",
            )
            db.commit()
            db.close()

            target = {
                "email": "target@example.com",
                "service_address": "imap.target",
                "folder": "INBOX",
            }
            config = {
                "target": target,
                "synology_labels": {
                    "$account": "SYNC:source@example.com",
                    "$inbox": "BOX:收件箱",
                },
                "sources": [rule],
            }
            profiles = {
                "source@example.com": {"email": "source@example.com"},
                "target@example.com": target,
            }

            with (
                mock.patch.object(
                    mail_sync,
                    "connect",
                    side_effect=[mock.Mock(), mock.Mock()],
                ),
                mock.patch.object(
                    mail_sync,
                    "recover_pending_operations",
                    return_value=0,
                ),
                mock.patch.object(
                    mail_sync,
                    "list_folders",
                    return_value=[mail_sync.FolderInfo("INBOX", "INBOX", ())],
                ),
                mock.patch.object(
                    mail_sync,
                    "mailbox_status",
                    return_value=(1, 11, 1),
                ),
                mock.patch.object(mail_sync, "select_mailbox") as select,
                mock.patch.object(mail_sync, "search_uids") as search,
                mock.patch.object(
                    mail_sync,
                    "reconcile_deleted_messages",
                    return_value=0,
                ),
            ):
                mail_sync.command_sync(
                    config,
                    profiles,
                    labels_by_name,
                    state_path,
                    "source@example.com",
                    None,
                    None,
                    True,
                )

            select.assert_not_called()
            search.assert_not_called()
            db = mail_sync.init_db(state_path)
            try:
                location = db.execute(
                    """
                    SELECT last_seen_run
                    FROM message_locations
                    WHERE source_email=? AND content_hash=?
                    """,
                    (message.source_email, message.content_hash),
                ).fetchone()
            finally:
                db.close()
            self.assertIsNotNone(location)
            self.assertEqual(location["last_seen_run"], "old-run")

    def test_full_scan_skips_unchanged_uid_list_when_uidnext_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            raw = b"Subject: cached\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=10,
                raw=raw,
                content_hash=mail_sync.content_hash(raw),
                message_id="<cached@example.com>",
                internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                system_flags=(),
            )
            mail_sync.upsert_state(
                db,
                message,
                2,
                20,
                "SYNC:source@example.com",
                ("BOX:收件箱",),
                "old-run",
                target_folder="INBOX",
                target_email="target@example.com",
                target_service_address="imap.target",
            )
            mail_sync.upsert_message_location(
                db,
                message,
                "BOX:收件箱",
                "old-run",
            )
            rule = {
                "email": "source@example.com",
                "account_label": "SYNC:source@example.com",
                "default_folder_label": "BOX:收件箱",
            }
            labels_by_name = {
                "SYNC:source@example.com": "$account",
                "BOX:收件箱": "$inbox",
            }
            signature = mail_sync._folder_scan_signature(
                rule,
                ("BOX:收件箱",),
                set(),
                "INBOX",
                labels_by_name,
            )
            mail_sync.upsert_folder_scan_state(
                db,
                "source@example.com",
                "INBOX",
                (1, 0, 1),
                "old-run",
                signature,
                "target@example.com",
                "imap.target",
                source_uids=[10],
            )
            db.commit()
            db.close()

            target = {
                "email": "target@example.com",
                "service_address": "imap.target",
                "folder": "INBOX",
            }
            config = {
                "target": target,
                "synology_labels": {
                    "$account": "SYNC:source@example.com",
                    "$inbox": "BOX:收件箱",
                },
                "sources": [rule],
            }
            profiles = {
                "source@example.com": {"email": "source@example.com"},
                "target@example.com": target,
            }

            with (
                mock.patch.object(
                    mail_sync,
                    "connect",
                    side_effect=[mock.Mock(), mock.Mock()],
                ),
                mock.patch.object(
                    mail_sync,
                    "recover_pending_operations",
                    return_value=0,
                ),
                mock.patch.object(
                    mail_sync,
                    "list_folders",
                    return_value=[mail_sync.FolderInfo("INBOX", "INBOX", ())],
                ),
                mock.patch.object(
                    mail_sync,
                    "mailbox_status",
                    return_value=(1, 0, 1),
                ),
                mock.patch.object(
                    mail_sync,
                    "select_mailbox",
                    return_value=(1, 1),
                ) as select,
                mock.patch.object(
                    mail_sync,
                    "search_uids",
                    return_value=[10],
                ) as search,
                mock.patch.object(mail_sync, "fetch_source_message") as fetch,
                mock.patch.object(
                    mail_sync,
                    "reconcile_deleted_messages",
                    return_value=0,
                ) as reconcile,
            ):
                mail_sync.command_sync(
                    config,
                    profiles,
                    labels_by_name,
                    state_path,
                    "source@example.com",
                    None,
                    None,
                    True,
                )

            select.assert_called_once()
            search.assert_called_once()
            fetch.assert_not_called()
            reconcile.assert_not_called()
            db = mail_sync.init_db(state_path)
            try:
                location = db.execute(
                    """
                    SELECT last_seen_run
                    FROM message_locations
                    WHERE source_email=? AND content_hash=?
                    """,
                    (message.source_email, message.content_hash),
                ).fetchone()
            finally:
                db.close()
            self.assertIsNotNone(location)
            self.assertEqual(location["last_seen_run"], "old-run")


class PendingMultiLabelTests(unittest.TestCase):
    def test_pending_operation_falls_back_to_search_when_uidnext_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            db = mail_sync.init_db(Path(directory) / "state.sqlite3")
            raw = b"Subject: pending fallback\r\n\r\nbody\r\n"
            message = mail_sync.SourceMessage(
                source_email="source@example.com",
                folder="INBOX",
                uidvalidity=1,
                uid=2,
                raw=raw,
                content_hash=mail_sync.hashlib.sha256(raw).hexdigest(),
                message_id="<pending-fallback@example.com>",
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
                    side_effect=mail_sync.SyncError("Invalid UIDNEXT: None"),
                ),
                mock.patch.object(
                    pending_module,
                    "search_uids",
                    return_value=[7, 12],
                ) as search,
            ):
                mail_sync.begin_pending_operation(
                    db,
                    mock.Mock(),
                    "INBOX",
                    message,
                    "SYNC:source@example.com",
                    ("BOX:收件箱",),
                    None,
                    "BOX:收件箱",
                )

            search.assert_called_once()
            row = db.execute(
                "SELECT target_uid_before FROM pending_operations"
            ).fetchone()
            self.assertEqual(row["target_uid_before"], 12)
            db.close()

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
            self.assertEqual(row["subject"], "pending")
            db.close()


if __name__ == "__main__":
    unittest.main()
