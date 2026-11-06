"""Collect SMS received over ModemManager's QMI interface alongside Asterisk."""
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from sms_bridge import Telegram
import conversation_store

DATABASE = Path(os.getenv("SMS_DATABASE", "/var/lib/dji-phone-gateway/messages.sqlite3"))
SETTINGS = Path(os.getenv("GATEWAY_SETTINGS", "/var/lib/dji-phone-gateway/settings.json"))
LOG = logging.getLogger("gateway.modem_sms")


def initialize():
    with closing(sqlite3.connect(DATABASE, timeout=5)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, "
                   "received_at TEXT DEFAULT CURRENT_TIMESTAMP, sender TEXT NOT NULL, body TEXT NOT NULL, "
                   "direction TEXT NOT NULL DEFAULT 'incoming', deleted_at TEXT)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        for column, definition in (("direction", "TEXT NOT NULL DEFAULT 'incoming'"), ("deleted_at", "TEXT")):
            if column not in columns:
                db.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
        db.execute("CREATE TABLE IF NOT EXISTS modem_sms_telegram (message_id INTEGER PRIMARY KEY)")


def mmcli(*args):
    result = subprocess.run(["mmcli", *args, "-J"], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError("ModemManager query failed")
    return json.loads(result.stdout)


def store(sms):
    """Remember source identities even after deletion; never import old modem archives."""
    properties, content = sms["properties"], sms["content"]
    if properties.get("state") != "received":
        return False
    stamp = properties.get("timestamp", "")
    try:
        received = datetime.fromisoformat(stamp).astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return False
    sender, body = content.get("number"), content.get("text")
    if not sender or not body or body == "--":
        return False
    fingerprint = hashlib.sha256(json.dumps([stamp, sender, body], ensure_ascii=False).encode()).hexdigest()
    with closing(sqlite3.connect(DATABASE, timeout=5)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS modem_sms_seen (fingerprint TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS modem_sms_meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS modem_sms_telegram (message_id INTEGER PRIMARY KEY)")
        # Freeze the initial cutoff. Import messages missed since the last saved SMS,
        # but not the years of unrelated messages sometimes left in modem storage.
        cutoff = db.execute("SELECT value FROM modem_sms_meta WHERE key='cutoff'").fetchone()
        if not cutoff:
            last = db.execute("SELECT MAX(received_at) FROM messages").fetchone()[0]
            cutoff = (last or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),)
            db.execute("INSERT INTO modem_sms_meta VALUES ('cutoff', ?)", cutoff)
        if not db.execute("INSERT OR IGNORE INTO modem_sms_seen VALUES (?)", (fingerprint,)).rowcount:
            return False
        timestamp = received.strftime("%Y-%m-%d %H:%M:%S")
        if timestamp <= cutoff[0]:
            return False
        duplicate = db.execute("SELECT id FROM messages WHERE direction='incoming' AND sender=? AND body=? "
                               "AND ABS(julianday(received_at)-julianday(?))*86400 < 120",
                               (sender, body, timestamp)).fetchone()
        if duplicate:
            return False
        message_id = db.execute("INSERT INTO messages(received_at,sender,body,direction) VALUES (?,?,?,'incoming')",
                                (timestamp, sender, body)).lastrowid
        db.execute("INSERT INTO modem_sms_telegram VALUES (?)", (message_id,))
    LOG.info("Imported incoming SMS from ModemManager (message %s)", message_id)
    return True


def forward_pending():
    try:
        settings = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        settings = {}
    token = settings.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat = settings.get("telegram_chat_id") or os.getenv("TELEGRAM_CHAT_ID")
    with closing(sqlite3.connect(DATABASE, timeout=5)) as db, db:
        if settings.get("telegram_enabled", True) is False or not token or not chat:
            db.execute("DELETE FROM modem_sms_telegram")
            return
        rows = db.execute("SELECT m.id,m.sender,m.body FROM messages m JOIN modem_sms_telegram p "
                          "ON m.id=p.message_id WHERE m.deleted_at IS NULL ORDER BY m.id LIMIT 20").fetchall()
        for message_id, sender, body in rows:
            if not conversation_store.forwarding_allowed(sender, DATABASE):
                db.execute("DELETE FROM modem_sms_telegram WHERE message_id=?", (message_id,))
                db.commit()
                continue
            digits = re.sub(r"\D", "", sender)
            name = (f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
                    if len(digits) == 11 and digits.startswith("1") else sender)
            name = conversation_store.get(sender, DATABASE)["name"] or name
            result = Telegram(str(token)).call("sendMessage", {"chat_id": chat, "text": f"{name}: {body}"[:4096],
                "disable_notification": "true" if conversation_store.get(sender, DATABASE)["muted"] else "false"})
            db.execute("CREATE TABLE IF NOT EXISTS telegram_reply_targets (message_id INTEGER NOT NULL, "
                       "chat_id INTEGER NOT NULL, sender TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                       "PRIMARY KEY(message_id,chat_id))")
            db.execute("INSERT OR REPLACE INTO telegram_reply_targets(message_id,chat_id,sender) VALUES (?,?,?)",
                       (result["result"]["message_id"], int(chat), sender))
            db.execute("DELETE FROM modem_sms_telegram WHERE message_id=?", (message_id,))
            db.commit()


def process_once():
    initialize()
    modems = mmcli("-L").get("modem-list", [])
    for modem in modems:
        paths = mmcli("-m", modem, "--messaging-list-sms").get("modem.messaging.sms", [])
        for path in paths:
            sms = mmcli("-s", path)["sms"]
            store(sms)
    forward_pending()


def worker():
    while True:
        try:
            process_once()
        except Exception as exc:
            # Do not log message contents, subscription details, or bot credentials.
            LOG.warning("Modem SMS collector will retry (%s)", type(exc).__name__)
        time.sleep(5)


def start_worker():
    threading.Thread(target=worker, name="modem-sms", daemon=True).start()
