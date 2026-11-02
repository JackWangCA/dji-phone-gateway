import base64
import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))
import web_push


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "Web Push requires cryptography")
class WebPushTests(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.patchers = [mock.patch.object(web_push, name, path) for name, path in (
            ("DATA_DIR", self.root), ("PUSH_DATABASE", self.root / "push.sqlite3"),
            ("PRIVATE_KEY", self.root / "push-vapid.pem"), ("SMS_DATABASE", self.root / "messages.sqlite3"))]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        encode = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
        self.subscription = {"endpoint": "https://web.push.apple.com/test-subscription",
                             "keys": {"p256dh": encode(public), "auth": encode(b"a" * 16)}}
        self.add_message("Existing SMS")

    def add_message(self, body, direction="incoming", deleted=None):
        with sqlite3.connect(web_push.SMS_DATABASE) as db:
            db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, sender TEXT, "
                       "body TEXT, direction TEXT, deleted_at TEXT)")
            return db.execute("INSERT INTO messages(sender,body,direction,deleted_at) VALUES (?,?,?,?)",
                              ("+15551234567", body, direction, deleted)).lastrowid

    def test_key_survives_restart_and_stays_private(self):
        key = web_push.public_key()
        self.assertEqual(key, web_push.public_key())
        self.assertEqual(len(base64.urlsafe_b64decode(key + "==")), 65)
        self.assertEqual(web_push.PRIVATE_KEY.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(importlib.util.find_spec("pywebpush"), "Requires installed push library")
    def test_real_payload_encryption_and_vapid_signing(self):
        from requests import Response
        import http_ece
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        private = ec.generate_private_key(ec.SECP256R1())
        public = private.public_key().public_bytes(serialization.Encoding.X962,
                                                    serialization.PublicFormat.UncompressedPoint)
        subscription = {**self.subscription, "keys": {
            "p256dh": base64.urlsafe_b64encode(public).decode().rstrip("="),
            "auth": base64.urlsafe_b64encode(b"a" * 16).decode().rstrip("="),
        }}
        response = Response()
        response.status_code = 201
        response._content = b""
        web_push.public_key()
        payload = web_push.notification(2, "+15551234567", "Encrypted SMS")
        with mock.patch("requests.post", return_value=response) as request:
            web_push.deliver(subscription, payload)
        encrypted = request.call_args.kwargs["data"]
        self.assertNotIn(b"Encrypted SMS", encrypted)
        decrypted = http_ece.decrypt(encrypted, private_key=private, auth_secret=b"a" * 16,
                                     version="aes128gcm")
        self.assertEqual(json.loads(decrypted)["body"], "Encrypted SMS")
        self.assertIn("vapid", request.call_args.kwargs["headers"]["Authorization"])

    def test_endpoint_rejects_local_services_and_invalid_keys(self):
        for endpoint in ("http://web.push.apple.com/x", "https://127.0.0.1/x",
                         "https://web.push.apple.com.evil.test/x", "https://web.push.apple.com:8080/x"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                web_push.validate_subscription({**self.subscription, "endpoint": endpoint})
        with self.assertRaises(ValueError):
            web_push.validate_subscription({**self.subscription, "keys": {"p256dh": "bad", "auth": "bad"}})

    def test_only_future_incoming_messages_are_sent_and_not_replayed(self):
        device = web_push.subscribe(self.subscription)
        self.add_message("Outgoing SMS", "outgoing")
        self.add_message("Deleted SMS", deleted="2026-11-01")
        self.add_message("New SMS")
        with mock.patch.object(web_push, "deliver") as deliver:
            web_push.process_once()
            web_push.process_once()
        deliver.assert_called_once()
        self.assertEqual(deliver.call_args.args[1]["body"], "New SMS")
        self.assertTrue(web_push.device_state(device)["enabled"])
        self.assertIsNotNone(web_push.device_state(device)["last_sent"])

    def test_opt_out_and_reenable_do_not_send_old_messages(self):
        device = web_push.subscribe(self.subscription)
        web_push.disable(device)
        self.add_message("During opt out")
        with mock.patch.object(web_push, "deliver") as deliver:
            web_push.process_once()
            web_push.subscribe(self.subscription)
            web_push.process_once()
        deliver.assert_not_called()

    def test_expired_device_is_disabled(self):
        device = web_push.subscribe(self.subscription)
        self.add_message("New SMS")
        error = RuntimeError("expired")
        error.status_code = 410
        with mock.patch.object(web_push, "deliver", side_effect=error):
            web_push.process_once()
        self.assertFalse(web_push.device_state(device)["enabled"])

    def test_temporary_failure_retries_without_advancing_cursor(self):
        device = web_push.subscribe(self.subscription)
        self.add_message("Retry this SMS")
        with mock.patch.object(web_push, "deliver", side_effect=OSError("offline")):
            web_push.process_once()
        self.assertTrue(web_push.device_state(device)["retrying"])
        with sqlite3.connect(web_push.PUSH_DATABASE) as db:
            db.execute("UPDATE devices SET next_attempt=0")
        with mock.patch.object(web_push, "deliver") as deliver:
            web_push.process_once()
        self.assertEqual(deliver.call_args.args[1]["body"], "Retry this SMS")

    def test_test_notification_targets_only_the_selected_device(self):
        device = web_push.subscribe(self.subscription)
        with mock.patch.object(web_push, "deliver") as deliver:
            web_push.test_notification(device)
        self.assertEqual(deliver.call_args.args[1]["tag"], "push-test")
        web_push.disable(device)
        with self.assertRaises(ValueError):
            web_push.test_notification(device)
