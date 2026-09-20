import json
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "bin" / "telegram-notify"


class IncomingSmsTests(unittest.TestCase):
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
