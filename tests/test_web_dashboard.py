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
    def test_dashboard_credentials_are_hashed_and_preserve_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = pathlib.Path(directory) / "settings.json"
            settings.write_text('{"telegram_bot_token":"token","telegram_chat_id":42}\n')
            with mock.patch.object(dashboard, "SETTINGS_PATH", settings):
                dashboard.save_dashboard_credentials("jackwang", "123456", "123456")
                saved = dashboard.load_settings()
            self.assertEqual(saved["dashboard_username"], "jackwang")
            self.assertEqual(saved["telegram_chat_id"], 42)
            self.assertNotIn("123456", saved["dashboard_password_hash"])
            self.assertTrue(dashboard.verify_password("123456", saved["dashboard_password_hash"]))
            self.assertFalse(dashboard.verify_password("wrong", saved["dashboard_password_hash"]))

    def test_dashboard_credentials_validate_username_and_confirmation(self):
        with self.assertRaisesRegex(ValueError, "Username"):
            dashboard.save_dashboard_credentials("x", "123456", "123456")
        with self.assertRaisesRegex(ValueError, "do not match"):
            dashboard.save_dashboard_credentials("jackwang", "123456", "654321")

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

    def test_history_error_does_not_report_a_queued_sms_as_failed(self):
        with (
            mock.patch.object(dashboard, "bridge_config", return_value=mock.Mock(modem="quectel0")),
            mock.patch.object(dashboard, "ami_command", return_value="SMS queued for send"),
            mock.patch.object(dashboard, "store_outgoing", side_effect=sqlite3.OperationalError("readonly database")),
        ):
            sent = dashboard.send_sms("+15551234567", "Hello")
        self.assertEqual(sent["direction"], "outgoing")
        self.assertIn("history could not be updated", sent["history_warning"])

    def test_page_contains_separate_conversation_view(self):
        with (
            mock.patch.object(dashboard, "run", return_value=(0, "off")),
            mock.patch.object(dashboard, "messages", return_value=[]),
        ):
            document = dashboard.page()
        self.assertIn('data-view="conversations"', document)
        self.assertIn('id="new-message"', document)
        self.assertIn('id="access-form"', document)
        self.assertIn("/api/sms", document)
        self.assertIn("/api/access", document)
        self.assertIn("preserveScroll", document)
        self.assertIn("touch-action:pan-y", document)
        self.assertIn('data-view="conversations" aria-selected="true"', document)
        self.assertIn("location.hash==='#dashboard'?'dashboard':'conversations'", document)
        self.assertIn('rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png"', document)
        self.assertNotIn("@@", document)

    def test_home_screen_icon_assets_exist(self):
        self.assertTrue((dashboard.ASSET_DIR / "dji-phone-gateway-icon.svg").is_file())
        self.assertTrue((dashboard.ASSET_DIR / "apple-touch-icon.png").is_file())


if __name__ == "__main__":
    unittest.main()
