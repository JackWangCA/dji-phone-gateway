#!/usr/bin/env python3
"""Persistent, per-device Web Push subscriptions and incoming SMS delivery."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

DATA_DIR = Path(os.getenv("PUSH_DATA_DIR", "/var/lib/dji-phone-gateway"))
PUSH_DATABASE = DATA_DIR / "push.sqlite3"
PRIVATE_KEY = DATA_DIR / "push-vapid.pem"
SMS_DATABASE = Path(os.getenv("SMS_DATABASE", str(DATA_DIR / "messages.sqlite3")))
KEY_LOCK = threading.Lock()
LOG = logging.getLogger("gateway.push")


def database():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PUSH_DATABASE, timeout=5)
    PUSH_DATABASE.chmod(0o600)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, subscription TEXT NOT NULL, "
               "cursor INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
               "last_sent REAL, next_attempt REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0)")
    return db


def public_key() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    with KEY_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not PRIVATE_KEY.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            content = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
            descriptor = os.open(PRIVATE_KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
        key = serialization.load_pem_private_key(PRIVATE_KEY.read_bytes(), password=None)
        raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def validate_subscription(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Invalid push subscription.")
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise ValueError("Invalid push endpoint.")
    url = urlsplit(endpoint)
    host = url.hostname or ""
    allowed = (host == "fcm.googleapis.com" or host.endswith(".push.apple.com")
               or host.endswith(".push.services.mozilla.com"))
    if not allowed or url.scheme != "https" or url.port not in (None, 443) or url.username or url.password or url.fragment:
        raise ValueError("Unsupported push service. Use Safari, Chrome, or Firefox.")
    keys = value.get("keys")
    try:
        if not isinstance(keys, dict):
            raise ValueError()
        public = base64.urlsafe_b64decode(keys["p256dh"] + "=" * (-len(keys["p256dh"]) % 4))
        auth = base64.urlsafe_b64decode(keys["auth"] + "=" * (-len(keys["auth"]) % 4))
        if len(public) != 65 or public[0] != 4 or len(auth) != 16:
            raise ValueError()
        from cryptography.hazmat.primitives.asymmetric import ec
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid push encryption keys.") from exc
    return {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}


def latest_id() -> int:
    if not SMS_DATABASE.exists():
        return 0
    with closing(sqlite3.connect(SMS_DATABASE, timeout=5)) as db:
        try:
            return int(db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])
        except sqlite3.OperationalError:
            return 0


def subscribe(value: object) -> str:
    subscription = validate_subscription(value)
    public_key()
    device_id = hashlib.sha256(subscription["endpoint"].encode()).hexdigest()
    cursor = latest_id()
    with closing(database()) as db, db:
        db.execute("INSERT INTO devices(id, subscription, cursor) VALUES (?, ?, ?) "
                   "ON CONFLICT(id) DO UPDATE SET subscription=excluded.subscription, enabled=1, "
                   "cursor=CASE WHEN devices.enabled=0 THEN excluded.cursor ELSE devices.cursor END, "
                   "failures=0, next_attempt=0", (device_id, json.dumps(subscription), cursor))
    return device_id


def device_state(device_id: str) -> dict:
    with closing(database()) as db:
        row = db.execute("SELECT enabled, last_sent, failures FROM devices WHERE id=?", (device_id,)).fetchone()
    return {"enabled": bool(row and row["enabled"]), "last_sent": row["last_sent"] if row else None,
            "retrying": bool(row and row["failures"])}


def disable(device_id: str) -> None:
    with closing(database()) as db, db:
        db.execute("UPDATE devices SET enabled=0 WHERE id=?", (device_id,))


def deliver(subscription: dict, payload: dict) -> None:
    from pywebpush import webpush
    webpush(subscription_info=subscription, data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=str(PRIVATE_KEY),
            vapid_claims={"sub": os.getenv("PUSH_VAPID_SUBJECT", "https://github.com")},
            ttl=3600, timeout=12, headers={"Urgency": "high"})


def notification(message_id: int, sender: str, body: str) -> dict:
    return {"title": sender or "Unknown number", "body": (body or "Empty message")[:500],
            "tag": f"sms-{message_id}", "url": "/#conversations", "peer": sender}


def test_notification(device_id: str) -> None:
    with closing(database()) as db:
        row = db.execute("SELECT subscription FROM devices WHERE id=? AND enabled=1", (device_id,)).fetchone()
    if not row:
        raise ValueError("Enable notifications on this device first.")
    deliver(json.loads(row[0]), {"title": "SMS Forwarder", "body": "Notifications are working on this device.",
                              "tag": "push-test", "url": "/#settings"})


def process_once() -> None:
    with closing(database()) as db:
        devices = db.execute("SELECT * FROM devices WHERE enabled=1 AND next_attempt<=?", (time.time(),)).fetchall()
    if not devices or not SMS_DATABASE.exists():
        return
    for device in devices:
        with closing(sqlite3.connect(SMS_DATABASE, timeout=5)) as sms:
            columns = {row[1] for row in sms.execute("PRAGMA table_info(messages)")}
            if "direction" not in columns:
                continue
            deleted = " AND deleted_at IS NULL" if "deleted_at" in columns else ""
            rows = sms.execute("SELECT id, sender, body FROM messages WHERE id>? AND direction='incoming'" +
                               deleted + " ORDER BY id LIMIT 20", (device["cursor"],)).fetchall()
        for message_id, sender, body in rows:
            # Respect a concurrent opt-out, even midway through a backlog.
            if not device_state(device["id"])["enabled"]:
                break
            try:
                deliver(json.loads(device["subscription"]), notification(message_id, sender, body))
            except Exception as exc:
                response = getattr(exc, "response", None)
                code = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
                if code in (404, 410):
                    disable(device["id"])
                else:
                    failures = device["failures"] + 1
                    with closing(database()) as db, db:
                        db.execute("UPDATE devices SET failures=?, next_attempt=? WHERE id=?",
                                   (failures, time.time() + min(300, 5 * 2**min(failures, 6)), device["id"]))
                # Never log endpoints, encryption keys, or SMS contents.
                LOG.warning("Push delivery failed (%s); device %s", code or type(exc).__name__, device["id"][:8])
                break
            with closing(database()) as db, db:
                db.execute("UPDATE devices SET cursor=?, last_sent=?, failures=0, next_attempt=0 WHERE id=?",
                           (message_id, time.time(), device["id"]))


def worker() -> None:
    while True:
        try:
            process_once()
        except Exception:
            LOG.warning("Push worker will retry after an internal error", exc_info=False)
        time.sleep(3)


def start_worker() -> None:
    threading.Thread(target=worker, name="web-push", daemon=True).start()
