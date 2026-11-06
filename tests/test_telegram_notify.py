import json
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import unittest
import importlib.machinery
import importlib.util
import io
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "bin" / "telegram-notify"
LOADER = importlib.machinery.SourceFileLoader("telegram_notify", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
notify = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(notify)
import conversation_store


class IncomingSmsTests(unittest.TestCase):
    def test_blocked_agi_sms_is_saved_without_forwarding(self):
        with tempfile.TemporaryDirectory() as directory:
            database = pathlib.Path(directory) / "messages.sqlite3"
            conversation_store.update("BANK", {"blocked": True}, database)
            settings = pathlib.Path(directory) / "settings.json"
            settings.write_text('{"telegram_bot_token":"test","telegram_chat_id":42}')
            with mock.patch.dict(os.environ, {"SMS_DATABASE": str(database), "GATEWAY_SETTINGS": str(settings)}), \
                    mock.patch.object(notify.sys, "stdin", io.StringIO("\n")), \
                    mock.patch.object(notify, "agi_variable", side_effect=["BANK", "", "Your code is 123456"]), \
                    mock.patch.object(notify, "agi_verbose"), \
                    mock.patch.object(notify.urllib.request, "urlopen") as request, \
                    self.assertRaises(SystemExit):
                notify.main()
            request.assert_not_called()
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("SELECT body FROM messages").fetchone()[0], "Your code is 123456")
    def test_telegram_can_be_disabled_without_stopping_sms_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            database = pathlib.Path(directory) / "messages.sqlite3"
            settings = pathlib.Path(directory) / "settings.json"
            settings.write_text(json.dumps({"telegram_bot_token": "saved-token", "telegram_chat_id": 42,
                                            "telegram_enabled": False}))
            with (
                mock.patch.dict(os.environ, {"SMS_DATABASE": str(database), "GATEWAY_SETTINGS": str(settings)}),
                mock.patch.object(notify.sys, "stdin", io.StringIO("\n")),
                mock.patch.object(notify, "agi_variable", side_effect=["+15551234567", "", "Hello"]),
                mock.patch.object(notify, "agi_verbose"),
                mock.patch.object(notify.urllib.request, "urlopen") as request,
                self.assertRaises(SystemExit) as exit_result,
            ):
                notify.main()
            self.assertEqual(exit_result.exception.code, 0)
            request.assert_not_called()
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("SELECT body FROM messages").fetchone()[0], "Hello")

    def test_telegram_message_id_is_linked_to_sender(self):
        with tempfile.TemporaryDirectory() as directory:
            database = pathlib.Path(directory) / "messages.sqlite3"
            notify.save_reply_target(database, 51, -42, "+15551234567")

            with sqlite3.connect(database) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT message_id, chat_id, sender FROM telegram_reply_targets"
                    ).fetchall(),
                    [(51, -42, "+15551234567")],
                )

    def test_notification_is_compact_and_siri_friendly(self):
        result = subprocess.run(
            [str(SCRIPT), "--format-preview", "+15551234567", "I’ll be there in ten minutes."],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "+1 (555) 123-4567: I’ll be there in ten minutes.")

    def test_unknown_sender_and_empty_message_are_spoken_naturally(self):
        result = subprocess.run(
            [str(SCRIPT), "--format-preview", "unknown", ""],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.stdout.strip(), "Unknown number: Empty message")

    def test_json_payload_is_stored_without_telegram(self):
        with tempfile.TemporaryDirectory() as directory:
            database = pathlib.Path(directory) / "messages.sqlite3"
            payload = json.dumps({"from": "+15551234567", "msg": "hello back"})
            agi_input = "\n" + "\n".join(
                [
                    "200 result=1 (+15551234567)",
                    f"200 result=1 ({payload})",
                    "200 result=0",
                    "200 result=1",
                    "200 result=1",
                ]
            ) + "\n"
            env = {
                **os.environ,
                "SMS_DATABASE": str(database),
                "GATEWAY_SETTINGS": str(pathlib.Path(directory) / "missing.json"),
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_CHAT_ID": "",
            }
            result = subprocess.run(
                [str(SCRIPT)], input=agi_input, text=True, capture_output=True, env=env, check=False
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with sqlite3.connect(database) as db:
                self.assertEqual(
                    db.execute("SELECT sender, body FROM messages").fetchall(),
                    [("+15551234567", "hello back")],
                )


if __name__ == "__main__":
    unittest.main()
