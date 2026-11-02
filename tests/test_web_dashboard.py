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

    def test_persistent_session_survives_new_requests_and_password_change_revokes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = pathlib.Path(directory) / "settings.json"
            with mock.patch.object(dashboard, "SETTINGS_PATH", settings):
                dashboard.save_dashboard_credentials("jackwang", "123456", "123456")
                token, lifetime = dashboard.create_session_token("jackwang", True)
                self.assertEqual(lifetime, dashboard.SESSION_TTL)
                self.assertTrue(dashboard.verify_session_token(token))
                dashboard.save_dashboard_credentials("jackwang", "654321", "654321")
                self.assertFalse(dashboard.verify_session_token(token))

    def test_login_page_defaults_to_thirty_day_session(self):
        document = dashboard.login_page(next_path="/#conversations")
        self.assertIn("Keep me signed in for 30 days", document)
        self.assertIn('name="remember" type="checkbox" value="yes" checked', document)
        self.assertIn('name="next" value="/#conversations"', document)
        self.assertNotIn("@@", document)

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

    def test_delete_selected_erases_contents_and_preserves_other_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            database = pathlib.Path(directory) / "messages.sqlite3"
            with mock.patch.object(dashboard, "SMS_DATABASE", database):
                first = dashboard.store_outgoing("+15551234567", "Delete this")
                second = dashboard.store_outgoing("+15551234567", "Keep this")
                self.assertEqual(dashboard.delete_messages(ids=str(first["id"])), 1)
                self.assertEqual(dashboard.delete_messages(ids=str(first["id"])), 0)
                self.assertEqual([m["id"] for m in dashboard.messages()], [second["id"]])
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("SELECT sender, body FROM messages WHERE id = ?", (first["id"],)).fetchone(), ("", ""))

    def test_delete_conversation_preserves_other_peers_and_new_arrivals(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(dashboard, "SMS_DATABASE", pathlib.Path(directory) / "messages.sqlite3"):
                first = dashboard.store_outgoing("+15551234567", "Old message")
                other = dashboard.store_outgoing("+15557654321", "Other conversation")
                arrival = dashboard.store_outgoing("+15551234567", "New arrival during confirmation")
                self.assertEqual(dashboard.delete_messages(peer=first["peer"], before_id=str(first["id"])), 1)
                self.assertEqual([m["id"] for m in dashboard.messages()], [other["id"], arrival["id"]])

    def test_delete_all_does_not_reuse_message_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(dashboard, "SMS_DATABASE", pathlib.Path(directory) / "messages.sqlite3"):
                first = dashboard.store_outgoing("+15551234567", "Old message")
                dashboard.delete_messages(ids=str(first["id"]))
                self.assertEqual(dashboard.messages(), [])
                next_message = dashboard.store_outgoing(first["peer"], "New message")
                self.assertGreater(next_message["id"], first["id"])

    def test_delete_rejects_ambiguous_or_invalid_targets(self):
        for kwargs in ({}, {"ids": "1 OR 1=1"}, {"ids": "0"}, {"ids": "-1"},
                       {"ids": "1,2", "peer": "+15551234567"}, {"peer": "+15551234567"},
                       {"ids": "9223372036854775808"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                dashboard.delete_messages(**kwargs)

    def test_delete_api_requires_authentication_and_csrf(self):
        for authenticated, csrf in ((False, dashboard.CSRF_TOKEN), (True, "wrong-token")):
            handler = mock.Mock()
            handler.path = "/api/messages/delete"
            handler.form.return_value = {"csrf": csrf, "ids": "1"}
            handler.authenticate.return_value = authenticated
            with mock.patch.object(dashboard, "delete_messages") as delete:
                dashboard.Handler.do_POST(handler)
            delete.assert_not_called()
            if authenticated:
                self.assertEqual(handler.respond_json.call_args.args[1], 403)

    def test_page_contains_separate_conversation_view(self):
        with (
            mock.patch.object(dashboard, "run", return_value=(0, "off")),
            mock.patch.object(dashboard, "messages", return_value=[]),
            mock.patch.object(dashboard, "gateway_status", return_value={"registration": "Searching", "services": {}}),
        ):
            document = dashboard.page()
        self.assertIn('<main id="main"', document)
        self.assertIn('id="new-message"', document)
        self.assertIn('id="conversation-search"', document)
        self.assertIn('id="jump-latest"', document)
        self.assertIn('id="signal-value"', document)
        self.assertIn('id="profile-selection"', document)
        self.assertIn('id="refresh-status"', document)
        self.assertIn('id="access-form"', document)
        self.assertIn("/api/sms", document)
        self.assertIn("/api/status", document)
        self.assertIn("/api/access", document)
        self.assertIn("preserveScroll", document)
        self.assertIn("touch-action:pan-y", document)
        self.assertIn('class="sheet"', document)
        self.assertIn('class="side"', document)
        self.assertNotIn('class="rail"', document)
        self.assertIn("location.hash==='#status'?'status'", document)
        self.assertIn('rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png"', document)
        self.assertNotIn("@@", document)

    def test_dashboard_injects_safe_config_without_bot_token(self):
        settings = {"telegram_bot_token": "private-bot-token", "telegram_chat_id": 42,
                    "dashboard_username": "</script><script>bad</script>"}
        with (
            mock.patch.object(dashboard, "load_settings", return_value=settings),
            mock.patch.object(dashboard, "messages", return_value=[]),
            mock.patch.object(dashboard, "gateway_status", return_value={}),
        ):
            document = dashboard.page()
        self.assertNotIn("private-bot-token", document)
        self.assertNotIn("</script><script>bad", document)
        self.assertIn('"telegram_configured": true', document)

    def test_gateway_status_reports_live_sim_network_fields(self):
        modem = {
            "modem": {
                "generic": {
                    "state": "registered",
                    "state-failed-reason": "--",
                    "sim": "/org/freedesktop/ModemManager1/SIM/0",
                    "unlock-required": "sim-pin2",
                    "signal-quality": {"value": "73", "recent": "yes"},
                    "access-technologies": ["lte"],
                    "own-numbers": ["+15551234567"],
                    "revision": "QDC507GLEFM21",
                    "carrier-configuration": "Commercial-TMO_VoLTE",
                },
                "3gpp": {
                    "registration-state": "home",
                    "operator-code": "46011",
                    "operator-name": "--",
                    "eps": {"initial-bearer": {"settings": {"apn": "CTNET"}}},
                },
            }
        }
        sim = {"sim": {"properties": {"operator-code": "46011", "operator-name": "--"}}}

        def json_command(command, _timeout=8):
            if command[1:3] == ["-L", "-J"]:
                return {"modem-list": ["/org/freedesktop/ModemManager1/Modem/0"]}
            if command[1] == "-m":
                return modem
            return sim

        def command(command, _timeout=15):
            if command[0] == "nmcli":
                return 0, "yes\n"
            if command[0] == "sudo":
                return 0, "off (connection inactive)"
            return 0, "active"

        with (
            mock.patch.object(dashboard, "_json_command", side_effect=json_command),
            mock.patch.object(dashboard, "run", side_effect=command),
            mock.patch.object(dashboard, "provider_name", return_value="China Telecom"),
        ):
            status = dashboard._gateway_status()
        self.assertTrue(status["registered"])
        self.assertEqual(status["registration"], "Registered")
        self.assertEqual(status["signal"], 73)
        self.assertEqual(status["carrier"], "China Telecom")
        self.assertEqual(status["phone_number"], "+15551234567")
        self.assertEqual(status["technology"], "LTE")
        self.assertEqual(status["firmware"], "QDC507GLEFM21")
        self.assertEqual(status["carrier_profile"], "Commercial-TMO_VoLTE")
        self.assertEqual(status["sim_home_operator"], "China Telecom · 46011")
        self.assertEqual(status["profile_selection"], "Automatic")
        self.assertEqual(status["apn"], "CTNET")
        self.assertEqual(status["apn_mode"], "Automatic")

    def test_home_screen_icon_assets_exist(self):
        self.assertTrue((dashboard.ASSET_DIR / "dji-phone-gateway-icon.svg").is_file())
        self.assertTrue((dashboard.ASSET_DIR / "apple-touch-icon.png").is_file())


if __name__ == "__main__":
    unittest.main()
