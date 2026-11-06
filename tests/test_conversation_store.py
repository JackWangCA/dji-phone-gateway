import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))
import conversation_store as store
import web_dashboard as dashboard


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name) / "sms.sqlite3"
        with sqlite3.connect(self.path) as db:
            dashboard.ensure_message_schema(db)
            db.executemany("INSERT INTO messages(sender,body,direction) VALUES (?,?,?)", [
                ("+15551234567", "Hello", "incoming"), ("+15551234567", "Hello back", "outgoing"),
                ("BANK", "Your code is 123456", "incoming"), ("outgoing", "Hi", "outgoing")])

    def test_shared_names_and_preferences_survive_reopening(self):
        store.update("+1 (555) 123-4567", {"name": "Alice", "pinned": True}, self.path)
        self.assertEqual(store.display_name("+15551234567", self.path), "Alice")
        self.assertTrue(store.get("+15551234567", self.path)["pinned"])
        store.update("+15551234567", {"name": ""}, self.path)
        self.assertEqual(store.display_name("+15551234567", self.path), "+15551234567")

    def test_read_cursor_is_monotonic_and_manual_unread_is_supported(self):
        self.assertEqual(store.unread_count(self.path), 2)
        store.update("+15551234567", {"read_id": 1}, self.path)
        store.update("+15551234567", {"read_id": 0}, self.path)
        self.assertEqual(store.unread_count(self.path), 1)
        store.update("outgoing", {"manual_unread": True}, self.path)
        self.assertEqual(store.unread_count(self.path), 2)
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO messages(sender,body,direction) VALUES ('+15551234567','New','incoming')")
        self.assertEqual(store.unread_count(self.path), 3)

    def test_archived_muted_blocked_and_deleted_are_excluded_from_badges(self):
        for flag in ("archived", "muted", "blocked"):
            store.update("BANK", {flag: True}, self.path)
            self.assertEqual(store.unread_count(self.path), 1)
            store.update("BANK", {flag: False}, self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE messages SET deleted_at=CURRENT_TIMESTAMP,sender='',body='' WHERE sender='BANK'")
        self.assertEqual(store.unread_count(self.path), 1)

    def test_mute_allows_silent_telegram_but_blocks_push_and_block_stops_both(self):
        store.update("BANK", {"muted": True}, self.path)
        self.assertTrue(store.forwarding_allowed("BANK", self.path))
        self.assertFalse(store.push_allowed("BANK", self.path))
        store.update("BANK", {"muted": False, "blocked": True}, self.path)
        self.assertFalse(store.forwarding_allowed("BANK", self.path))
        self.assertFalse(store.push_allowed("BANK", self.path))

    def test_invalid_changes_do_not_write_arbitrary_columns_or_html(self):
        for peer, changes in (("", {"name": "A"}), ("BANK", {"deleted_at": True}),
                              ("BANK", {"muted": "false"}), ("BANK", {"read_id": -1}),
                              ("BANK", {"name": "Line\nbreak"})):
            with self.subTest(peer=peer, changes=changes), self.assertRaises(ValueError):
                store.update(peer, changes, self.path)

    def test_verification_code_requires_auth_context_and_avoids_phone_numbers(self):
        for body, expected in (("Your verification code is 192837. Expires in 10 minutes", "192837"),
                               ("【支付宝】验证码：2518，请勿泄露", "2518"), ("验证码123456", "123456"),
                               ("OTP: 87654321", "87654321"), ("Call 15551234567 for your verification code", None),
                               ("Order 123456 is ready", None), ("Your code is 12345678901", None)):
            with self.subTest(body=body):
                self.assertEqual(store.verification_code(body), expected)

    def test_api_requires_authentication_and_csrf(self):
        for authenticated, csrf in ((False, dashboard.CSRF_TOKEN), (True, "invalid")):
            handler = mock.Mock()
            handler.path = "/api/conversations"
            handler.form.return_value = {"csrf": csrf, "peer": "BANK", "changes": '{"muted":true}'}
            handler.authenticate.return_value = authenticated
            with mock.patch.object(store, "update") as update:
                dashboard.Handler.do_POST(handler)
            update.assert_not_called()
