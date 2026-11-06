import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))
import modem_sms
import conversation_store


class ModemSMSTests(unittest.TestCase):
    def test_named_muted_telegram_is_silent_and_blocked_sms_stays_local(self):
        modem_sms.store(self.sms())
        settings = pathlib.Path(self.temp.name) / "settings.json"
        settings.write_text('{"telegram_bot_token":"test","telegram_chat_id":123}')
        conversation_store.update("+15551234567", {"name": "Alice", "muted": True}, modem_sms.DATABASE)
        with mock.patch.object(modem_sms, "SETTINGS", settings), mock.patch.object(modem_sms, "Telegram") as telegram:
            telegram.return_value.call.return_value = {"result": {"message_id": 123}}
            modem_sms.forward_pending()
            payload = telegram.return_value.call.call_args.args[1]
            self.assertEqual(payload["text"], "Alice: Missed SMS")
            self.assertEqual(payload["disable_notification"], "true")
            conversation_store.update("+15551234567", {"blocked": True}, modem_sms.DATABASE)
            modem_sms.store(self.sms("2026-11-06T10:15:21+08"))
            telegram.reset_mock()
            modem_sms.forward_pending()
            telegram.assert_not_called()
        with sqlite3.connect(modem_sms.DATABASE) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM modem_sms_telegram").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.object(modem_sms, "DATABASE", pathlib.Path(self.temp.name) / "sms.sqlite3")
        patcher.start()
        self.addCleanup(patcher.stop)
        modem_sms.initialize()
        with sqlite3.connect(modem_sms.DATABASE) as db:
            db.execute("INSERT INTO messages(received_at,sender,body) VALUES ('2026-11-02 01:00:00','old','old')")

    def sms(self, stamp="2026-11-05T17:15:21+08", state="received"):
        return {"properties": {"state": state, "timestamp": stamp},
                "content": {"number": "+15551234567", "text": "Missed SMS"}}

    def test_missed_qmi_sms_imported_once_and_not_resurrected_after_delete(self):
        sms = self.sms()
        self.assertTrue(modem_sms.store(sms))
        self.assertFalse(modem_sms.store(sms))
        with sqlite3.connect(modem_sms.DATABASE) as db:
            self.assertEqual(db.execute("SELECT received_at FROM messages WHERE id=2").fetchone()[0],
                             "2026-11-05 09:15:21")
            db.execute("UPDATE messages SET sender='',body='',deleted_at=CURRENT_TIMESTAMP WHERE id=2")
        self.assertFalse(modem_sms.store(sms))

    def test_old_archive_and_partial_messages_not_imported(self):
        self.assertFalse(modem_sms.store(self.sms("2024-11-21T17:56:45+08")))
        self.assertFalse(modem_sms.store(self.sms(state="receiving")))
        self.assertTrue(modem_sms.store(self.sms()))

    def test_existing_asterisk_message_not_duplicated(self):
        # Establish the baseline before a new Asterisk message arrives.
        modem_sms.store(self.sms("2024-11-21T17:56:45+08"))
        with sqlite3.connect(modem_sms.DATABASE) as db:
            db.execute("INSERT INTO messages(received_at,sender,body) VALUES "
                       "('2026-11-05 09:15:23','+15551234567','Missed SMS')")
        self.assertFalse(modem_sms.store(self.sms()))

    def test_telegram_off_keeps_sms_but_clears_forward_queue(self):
        modem_sms.store(self.sms())
        settings = pathlib.Path(self.temp.name) / "settings.json"
        settings.write_text('{"telegram_enabled":false}')
        with mock.patch.object(modem_sms, "SETTINGS", settings), mock.patch.object(modem_sms, "Telegram") as telegram:
            modem_sms.forward_pending()
            telegram.assert_not_called()
        with sqlite3.connect(modem_sms.DATABASE) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM modem_sms_telegram").fetchone()[0], 0)

    def test_telegram_reply_link_and_retry(self):
        modem_sms.store(self.sms())
        settings = pathlib.Path(self.temp.name) / "settings.json"
        settings.write_text('{"telegram_bot_token":"test","telegram_chat_id":123}')
        with mock.patch.object(modem_sms, "SETTINGS", settings), mock.patch.object(modem_sms, "Telegram") as telegram:
            telegram.return_value.call.side_effect = RuntimeError("temporary")
            with self.assertRaises(RuntimeError):
                modem_sms.forward_pending()
            telegram.return_value.call.side_effect = None
            telegram.return_value.call.return_value = {"result": {"message_id": 456}}
            modem_sms.forward_pending()
            modem_sms.forward_pending()
            self.assertEqual(telegram.return_value.call.call_count, 2)
        with sqlite3.connect(modem_sms.DATABASE) as db:
            self.assertEqual(db.execute("SELECT sender FROM telegram_reply_targets WHERE message_id=456").fetchone()[0],
                             "+15551234567")
