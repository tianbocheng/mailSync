import json
import queue
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.header import Header
from pathlib import Path
from unittest import mock


import mail_sync
from domain import message as mail_sync_message
from sync import schedule as schedule_module

from tests import helpers  # noqa: F401

class ImapQuotingTests(unittest.TestCase):
    def test_quotes_and_escapes_search_value(self):
        value = '<id.JavaMail."user@example.com"@host>'
        self.assertEqual(
            mail_sync.quote_imap_string(value),
            '"<id.JavaMail.\\"user@example.com\\"@host>"',
        )

class ParserDefaultsTests(unittest.TestCase):
    def test_watch_default_interval_matches_documented_service_default(self):
        with mock.patch.dict(mail_sync.os.environ, {}, clear=True):
            args = mail_sync.build_parser().parse_args(["watch"])

        self.assertEqual(args.interval, mail_sync.DEFAULT_SYNC_INTERVAL_SECONDS)

    def test_serve_default_port_uses_environment(self):
        with mock.patch.dict(mail_sync.os.environ, {"MAIL_SYNC_WEB_PORT": "9090"}):
            args = mail_sync.build_parser().parse_args(["serve"])

        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 9090)

    def test_run_default_port_and_interval(self):
        with mock.patch.dict(mail_sync.os.environ, {}, clear=True):
            args = mail_sync.build_parser().parse_args(["run"])

        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 24000)
        self.assertEqual(args.interval, mail_sync.DEFAULT_SYNC_INTERVAL_SECONDS)
        self.assertEqual(args.poll_interval, 3)

    def test_source_realtime_poll_interval_prefers_mailbox_setting(self):
        self.assertEqual(
            mail_sync._source_realtime_poll_interval(
                {"realtime_poll_interval": "12"},
                3,
            ),
            12,
        )
        self.assertEqual(
            mail_sync._source_realtime_poll_interval(
                {"realtime_poll_interval": ""},
                3,
            ),
            3,
        )


class IncrementalRetryTests(unittest.TestCase):
    def test_incremental_retry_uses_bounded_backoff(self):
        pending = {}
        change = {
            "email": "source@example.com",
            "folder": "INBOX",
            "_queued_at": 90.0,
            "_retry_attempt": 0,
        }

        with mock.patch.object(mail_sync.time, "monotonic", return_value=100.0):
            for attempt, delay in enumerate(
                mail_sync.INCREMENTAL_SYNC_RETRY_DELAYS,
                start=1,
            ):
                retry = mail_sync._schedule_incremental_retry(pending, change)
                self.assertEqual(retry, (attempt, delay))
                change = pending.pop(("source@example.com", "INBOX"))
                self.assertEqual(change["_retry_attempt"], attempt)
                self.assertEqual(change["_retry_at"], 100.0 + delay)

            self.assertIsNone(
                mail_sync._schedule_incremental_retry(pending, change)
            )
            self.assertEqual(pending, {})

    def test_pending_retry_waits_without_hiding_ready_change(self):
        pending = {
            ("retry@example.com", "INBOX"): {
                "email": "retry@example.com",
                "folder": "INBOX",
                "_queued_at": 80.0,
                "_retry_at": 105.0,
            },
            ("ready@example.com", "INBOX"): {
                "email": "ready@example.com",
                "folder": "INBOX",
                "_queued_at": 90.0,
                "_retry_at": 0.0,
            },
        }

        self.assertEqual(
            mail_sync._next_pending_change_time(pending, 100.0),
            100.0,
        )
        ready = mail_sync._pop_ready_pending_change(pending, 100.0)
        self.assertEqual(ready["email"], "ready@example.com")
        self.assertIsNone(mail_sync._pop_ready_pending_change(pending, 104.0))
        self.assertEqual(
            mail_sync._next_pending_change_time(pending, 104.0),
            105.0,
        )


class ScheduleRefreshTests(unittest.TestCase):
    def test_build_initial_schedule_uses_source_interval_for_staggering(self):
        rules = [
            {"email": "a@example.com", "sync_interval": 100},
            {"email": "b@example.com", "sync_interval": 100},
            {"email": "c@example.com", "sync_interval": 100},
            {"email": "d@example.com", "sync_interval": 100},
        ]

        with mock.patch.object(schedule_module.time, "monotonic", return_value=1000.0):
            schedule = mail_sync.build_source_schedule(rules, 900)

        self.assertEqual([item["email"] for item in schedule], [rule["email"] for rule in rules])
        self.assertEqual([item["interval"] for item in schedule], [100, 100, 100, 100])
        self.assertEqual(
            [item["next_run"] for item in schedule],
            [1000.0, 1025.0, 1050.0, 1075.0],
        )

    def test_refresh_applies_changed_interval_and_batch_size(self):
        existing = [
            {
                "email": "source@example.com",
                "interval": 900,
                "batch_size": 5,
                "last_run": 100.0,
                "next_run": 1000.0,
            }
        ]

        with mock.patch.object(schedule_module.time, "monotonic", return_value=200.0):
            refreshed = mail_sync.refresh_source_schedule(
                [
                    {
                        "email": "source@example.com",
                        "sync_interval": 15,
                        "batch_size": 200,
                    }
                ],
                900,
                existing,
            )

        self.assertEqual(refreshed[0]["interval"], 15)
        self.assertEqual(refreshed[0]["batch_size"], 200)
        self.assertEqual(refreshed[0]["last_run"], 100.0)
        self.assertEqual(refreshed[0]["next_run"], 115.0)

    def test_refresh_preserves_next_run_when_only_batch_size_changes(self):
        existing = [
            {
                "email": "source@example.com",
                "interval": 900,
                "batch_size": 5,
                "last_run": 100.0,
                "next_run": 1000.0,
            }
        ]

        with mock.patch.object(schedule_module.time, "monotonic", return_value=200.0):
            refreshed = mail_sync.refresh_source_schedule(
                [
                    {
                        "email": "source@example.com",
                        "sync_interval": 900,
                        "batch_size": 200,
                    }
                ],
                900,
                existing,
            )

        self.assertEqual(refreshed[0]["interval"], 900)
        self.assertEqual(refreshed[0]["batch_size"], 200)
        self.assertEqual(refreshed[0]["next_run"], 1000.0)

    def test_refresh_preserves_monitor_skip_state(self):
        existing = [
            {
                "email": "source@example.com",
                "interval": 120,
                "batch_size": 20,
                "last_run": 100.0,
                "last_forced_run": 90.0,
                "last_monitor_version": 7,
                "next_run": 220.0,
            }
        ]

        refreshed = mail_sync.refresh_source_schedule(
            [{"email": "source@example.com", "sync_interval": 120}],
            900,
            existing,
        )

        self.assertEqual(refreshed[0]["last_forced_run"], 90.0)
        self.assertEqual(refreshed[0]["last_monitor_version"], 7)

    def test_monitor_unchanged_full_scan_skip_requires_all_folder_monitor(self):
        rule = {
            "email": "source@example.com",
            "service_address": "imap.example.com",
        }
        profiles = {
            "source@example.com": {"service_address": "imap.example.com"}
        }
        job = {
            "email": "source@example.com",
            "last_forced_run": 100.0,
            "last_monitor_version": 2,
        }
        records = {
            ("source@example.com", "imap.example.com"): {
                "state": {
                    "ready": True,
                    "coverage": "all_folders",
                    "change_version": 2,
                }
            }
        }

        with mock.patch.object(
            mail_sync,
            "MONITOR_UNCHANGED_FULL_SCAN_SKIP_SECONDS",
            600,
        ):
            self.assertTrue(
                mail_sync._scheduled_full_scan_can_skip(
                    job,
                    rule,
                    profiles,
                    records,
                    200.0,
                )
            )
            records[("source@example.com", "imap.example.com")]["state"][
                "coverage"
            ] = "inbox"
            self.assertFalse(
                mail_sync._scheduled_full_scan_can_skip(
                    job,
                    rule,
                    profiles,
                    records,
                    200.0,
                )
            )

    def test_monitor_unchanged_full_scan_skip_expires_for_safety(self):
        rule = {
            "email": "source@example.com",
            "service_address": "imap.example.com",
        }
        profiles = {
            "source@example.com": {"service_address": "imap.example.com"}
        }
        job = {
            "email": "source@example.com",
            "last_forced_run": 100.0,
            "last_monitor_version": 2,
        }
        records = {
            ("source@example.com", "imap.example.com"): {
                "state": {
                    "ready": True,
                    "coverage": "all_folders",
                    "change_version": 2,
                }
            }
        }

        with mock.patch.object(
            mail_sync,
            "MONITOR_UNCHANGED_FULL_SCAN_SKIP_SECONDS",
            600,
        ):
            self.assertFalse(
                mail_sync._scheduled_full_scan_can_skip(
                    job,
                    rule,
                    profiles,
                    records,
                    800.0,
                )
            )


class RealtimeMonitorRefreshTests(unittest.TestCase):
    def test_sync_source_monitors_starts_rule_added_after_watch_start(self):
        records = {}
        created_threads = []

        class FakeThread:
            def __init__(self, target, args, name, daemon):
                self.target = target
                self.args = args
                self.name = name
                self.daemon = daemon
                self.started = False
                self.joined = False
                created_threads.append(self)

            def start(self):
                self.started = True

            def join(self, timeout=None):
                self.joined = True
                self.timeout = timeout

        rules = [
            {
                "email": "source@example.com",
                "service_address": "imap.example.com",
            }
        ]
        profiles = {
            "source@example.com": {
                "service_address": "imap.example.com",
                "imap_server": "imap.example.com",
                "use_ssl": True,
            }
        }

        with mock.patch.object(mail_sync.threading, "Thread", FakeThread):
            mail_sync._sync_source_monitors(
                records,
                rules,
                profiles,
                None,
                3.0,
                queue.Queue(),
            )
            self.assertIn(("source@example.com", "imap.example.com"), records)
            self.assertTrue(created_threads[0].started)
            self.assertIn("change_version", created_threads[0].args[-1])
            self.assertFalse(
                records[("source@example.com", "imap.example.com")][
                    "stop_event"
                ].is_set()
            )

            mail_sync._sync_source_monitors(
                records,
                [],
                profiles,
                None,
                3.0,
                queue.Queue(),
            )

        self.assertEqual(records, {})
        self.assertTrue(created_threads[0].joined)


class CombinedRunnerTests(unittest.TestCase):
    def test_run_starts_web_and_sync_with_separate_logs(self):
        class FakeProcess:
            def __init__(self, pid, return_code):
                self.pid = pid
                self.return_code = return_code
                self.terminated = False
                self.killed = False

            def poll(self):
                return self.return_code

            def terminate(self):
                self.terminated = True
                self.return_code = 0

            def kill(self):
                self.killed = True
                self.return_code = -9

            def wait(self, timeout=None):
                return self.return_code

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            log_dir = Path(directory) / "logs"
            processes = [
                FakeProcess(101, None),
                FakeProcess(102, 3),
            ]
            commands = []

            def fake_popen(command, **kwargs):
                commands.append((command, kwargs))
                return processes[len(commands) - 1]

            with mock.patch.object(mail_sync.subprocess, "Popen", side_effect=fake_popen):
                result = mail_sync.command_run(
                    state_path,
                    "127.0.0.1",
                    8080,
                    "source@example.com",
                    "INBOX",
                    10,
                    False,
                    15,
                    0.0,
                    str(log_dir),
                )

            self.assertEqual(result, 3)
            self.assertTrue(processes[0].terminated)
            self.assertIn("serve", commands[0][0])
            self.assertIn("watch", commands[1][0])
            self.assertIn("--oldest-first", commands[1][0])
            self.assertIn("--source", commands[1][0])
            self.assertIn("source@example.com", commands[1][0])
            self.assertTrue((log_dir / "web.log").exists())
            self.assertTrue((log_dir / "sync.log").exists())
            self.assertIn("web", (log_dir / "web.log").read_text(encoding="utf-8"))
            self.assertIn("sync", (log_dir / "sync.log").read_text(encoding="utf-8"))


class WatchStartupTests(unittest.TestCase):
    def test_watch_allows_empty_runtime_config(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            db.close()

            with mock.patch.object(mail_sync, "command_watch", return_value=0) as watch:
                result = mail_sync.main(
                    [
                        "--db",
                        str(state_path),
                        "watch",
                        "--source",
                        "future@example.com",
                    ]
                )

            self.assertEqual(result, 0)
            config = watch.call_args.args[0]
            profiles = watch.call_args.args[1]
            rules = config["sources"]
            self.assertEqual(rules, [])
            self.assertEqual(profiles, {})

    def test_strict_validation_still_rejects_empty_sources(self):
        config = {
            "state_db": "state.sqlite3",
            "target": {
                "email": "target@example.com",
                "password": "secret",
                "imap_server": "target.example.com",
                "folder": "INBOX",
            },
            "sources": [],
        }
        labels = {}

        with self.assertRaisesRegex(mail_sync.SyncError, "sources"):
            mail_sync.validate_config(config, labels)

        mail_sync.validate_config(config, labels, allow_empty_sources=True)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
