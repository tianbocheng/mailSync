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


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
