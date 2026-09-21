import importlib
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

SRC = pathlib.Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("ASTERISK_AMI_USER", "test")
os.environ.setdefault("ASTERISK_AMI_PASSWORD", "test")
dashboard = importlib.import_module("web_dashboard")


class ConversationTests(unittest.TestCase):
    def test_legacy_inbox_migrates_and_outgoing_message_is_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            database = pathlib.Path(directory) / "messages.sqlite3"
            with sqlite3.connect(database) as db:
                db.execute(
                    "CREATE TABLE messages (id INTEGER PRIMARY KEY, received_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                    "sender TEXT NOT NULL, body TEXT NOT NULL)"
                )
                db.execute("INSERT INTO messages(sender, body) VALUES ('+15551234567', 'Received')")
            with mock.patch.object(dashboard, "SMS_DATABASE", database):
                sent = dashboard.store_outgoing("+15551234567", "Sent")
                history = dashboard.messages()
            self.assertEqual([item["direction"] for item in history], ["incoming", "outgoing"])
            self.assertEqual(sent["body"], "Sent")

    def test_successful_web_send_is_normalized_and_stored(self):
        with tempfile.TemporaryDirectory() as directory:
            database = pathlib.Path(directory) / "messages.sqlite3"
            with (
                mock.patch.object(dashboard, "SMS_DATABASE", database),
                mock.patch.object(dashboard, "bridge_config", return_value=mock.Mock(modem="quectel0")),
                mock.patch.object(dashboard, "ami_command", return_value="SMS queued for send") as ami,
            ):
                sent = dashboard.send_sms("+1 555-123-4567", "Hello")
                history = dashboard.messages()
            self.assertEqual(sent["peer"], "+15551234567")
            self.assertEqual(history[0]["direction"], "outgoing")
            self.assertIn("+15551234567 Hello", ami.call_args.args[1])

    def test_page_contains_separate_conversation_view(self):
        with (
            mock.patch.object(dashboard, "run", return_value=(0, "off")),
            mock.patch.object(dashboard, "messages", return_value=[]),
        ):
            document = dashboard.page()
        self.assertIn('data-view="conversations"', document)
        self.assertIn('id="new-message"', document)
        self.assertIn("/api/sms", document)
        self.assertNotIn("@@", document)


if __name__ == "__main__":
    unittest.main()
