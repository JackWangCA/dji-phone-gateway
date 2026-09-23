import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

PATH = pathlib.Path(__file__).parents[1] / "src" / "sms_bridge.py"
spec = importlib.util.spec_from_file_location("sms_bridge", PATH)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class ParsingTests(unittest.TestCase):
    def test_bot_suffix_and_quoted_message(self):
        self.assertEqual(bridge.parse_command('/sms@mybot +1555 "hello world"'), ("/sms", ["+1555", "hello world"]))

    def test_phone_validation(self):
        self.assertTrue(bridge.safe_phone("+1-555-123-4567"))
        self.assertFalse(bridge.safe_phone("+1; reboot"))

    def test_sms_flattens_cli_control_characters(self):
        self.assertEqual(bridge.safe_sms("hello\ncore stop now\x00"), "hello core stop now")

    def test_telegram_token_validation(self):
        self.assertTrue(bridge.valid_bot_token("123456789:abcdefghijklmnopqrstuvwxyz_ABCDE"))
        self.assertFalse(bridge.valid_bot_token("echo sudo something"))
        with self.assertRaisesRegex(ValueError, "Paste only the token"):
            bridge.Telegram("not a token")

    def test_unauthorized_chat_is_silent(self):
        class Telegram:
            def send(self, *_):
                raise AssertionError("must not reply to unauthorized users")
        cfg = bridge.Config("token", 42, "user", "password")
        bridge.handle(cfg, Telegram(), 7, "/help")

    def test_only_quoted_replies_are_sent(self):
        class Telegram:
            def __init__(self):
                self.messages = []

            def send(self, _chat_id, text):
                self.messages.append(text)

        with tempfile.TemporaryDirectory() as directory:
            database = str(pathlib.Path(directory) / "messages.sqlite3")
            with sqlite3.connect(database) as db:
                db.execute(
                    "CREATE TABLE telegram_reply_targets (message_id INTEGER, chat_id INTEGER, "
                    "sender TEXT, created_at TEXT, PRIMARY KEY(message_id, chat_id))"
                )
                db.execute(
                    "INSERT INTO telegram_reply_targets VALUES (101, 42, '+15551234567', '2026-09-20 10:00:00')"
                )
                db.execute(
                    "INSERT INTO telegram_reply_targets VALUES (102, 42, '+15557654321', '2026-09-20 10:01:00')"
                )
            cfg = bridge.Config("token", 42, "user", "password", sms_database=database)
            tg = Telegram()
            with mock.patch.object(bridge, "queue_sms", return_value=(True, "SMS queued for send")) as queue:
                bridge.handle(cfg, tg, 42, "Quoted reply", reply_message_id=101)
                bridge.handle(cfg, tg, 42, "Unquoted reply")

            queue.assert_called_once_with(cfg, "+15551234567", "Quoted reply")
            self.assertEqual(tg.messages, ["SMS queued to +15551234567."])

    def test_queued_telegram_sms_is_added_to_conversation_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(pathlib.Path(directory) / "messages.sqlite3")
            cfg = bridge.Config("token", 42, "user", "password", sms_database=database)
            with mock.patch.object(bridge, "ami_command", return_value="SMS queued for send"):
                queued, _ = bridge.queue_sms(cfg, "+1 555-123-4567", "Reply")
            with sqlite3.connect(database) as db:
                row = db.execute("SELECT sender, body, direction FROM messages").fetchone()
            self.assertTrue(queued)
            self.assertEqual(row, ("+15551234567", "Reply", "outgoing"))


if __name__ == "__main__":
    unittest.main()
