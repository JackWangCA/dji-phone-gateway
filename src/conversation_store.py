"""Shared contacts, conversation preferences, read cursors, and badge counts."""
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

DATABASE = Path(os.getenv("SMS_DATABASE", "/var/lib/dji-phone-gateway/messages.sqlite3"))
DEFAULT = {"name": "", "pinned": False, "muted": False, "archived": False,
           "blocked": False, "read_id": 0, "manual_unread": False}


def normalize(peer):
    value = str(peer).strip()
    if not value or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise ValueError("Enter a valid number or sender ID.")
    return re.sub(r"[ ()-]", "", value) if re.fullmatch(r"[+0-9 ()-]+", value) else value


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS conversations (peer TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
               "pinned INTEGER NOT NULL DEFAULT 0, muted INTEGER NOT NULL DEFAULT 0, "
               "archived INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0, "
               "read_id INTEGER NOT NULL DEFAULT 0, manual_unread INTEGER NOT NULL DEFAULT 0)")


def preferences(path=DATABASE):
    if not Path(path).exists():
        return {}
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as db:
        try:
            rows = db.execute("SELECT peer,name,pinned,muted,archived,blocked,read_id,manual_unread FROM conversations").fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return {}
            raise
    return {row[0]: {key: (bool(value) if key not in ("name", "read_id") else value)
                     for key, value in zip(DEFAULT, row[1:])} for row in rows}


def get(peer, path=DATABASE):
    return {**DEFAULT, **preferences(path).get(normalize(peer), {})}


def display_name(peer, path=DATABASE):
    return get(peer or "unknown", path)["name"] or str(peer or "Unknown number")


def forwarding_allowed(peer, path=DATABASE):
    value = get(peer, path)
    return not value["blocked"]


def push_allowed(peer, path=DATABASE):
    value = get(peer, path)
    return not value["muted"] and not value["blocked"]


def unread_count(path=DATABASE):
    if not Path(path).exists():
        return 0
    meta = preferences(path)
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
        if "direction" not in columns:
            return 0
        deleted = " WHERE deleted_at IS NULL" if "deleted_at" in columns else ""
        rows = db.execute("SELECT sender,MAX(CASE WHEN direction='incoming' THEN id ELSE 0 END) "
                          "FROM messages" + deleted + " GROUP BY sender").fetchall()
    latest = {}
    for peer, message_id in rows:
        key = normalize(peer)
        latest[key] = max(latest.get(key, 0), message_id)
    return sum(1 for peer, message_id in latest.items()
               if not any(meta.get(peer, DEFAULT)[flag] for flag in ("muted", "blocked", "archived"))
               and (meta.get(peer, DEFAULT)["manual_unread"] or message_id > meta.get(peer, DEFAULT)["read_id"]))


def state(path=DATABASE):
    return {"peers": preferences(path), "unread_count": unread_count(path)}


def update(peer, changes, path=DATABASE):
    peer = normalize(peer)
    if not isinstance(changes, dict) or not changes or set(changes) - set(DEFAULT):
        raise ValueError("Invalid conversation settings.")
    for key, value in changes.items():
        if key == "name":
            if not isinstance(value, str) or len(value) > 100 or any(ord(c) < 32 for c in value):
                raise ValueError("Contact names must be at most 100 characters.")
            changes[key] = value.strip()
        elif key == "read_id":
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise ValueError("Invalid read position.")
        elif type(value) is not bool:
            raise ValueError("Invalid conversation switch.")
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        initialize(db)
        db.execute("INSERT OR IGNORE INTO conversations(peer) VALUES (?)", (peer,))
        for key, value in changes.items():
            if key == "read_id":
                # Do not let an older browser move the shared read cursor backwards.
                row = db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()
                value = min(value, row[0])
                db.execute("UPDATE conversations SET read_id=MAX(read_id,?) WHERE peer=?", (value, peer))
            else:
                db.execute(f"UPDATE conversations SET {key}=? WHERE peer=?", (value, peer))
    return state(path)


def verification_code(body):
    """Only detect numeric codes near an explicit authentication-code cue."""
    cues = list(re.finditer(r"验证码|驗證碼|校验码|校驗碼|动态码|動態碼|verification|security\s+code|"
                           r"one[-\s]*time|\botp\b|passcode|(?:login|sign[-\s]*in|authentication)\s+code|"
                           r"\bcode\s*(?:is|:)|\buse\s+(?:the\s+)?code", body, re.IGNORECASE))
    candidates = []
    for match in re.finditer(r"(?<![A-Za-z0-9_])([0-9]{4,8})(?![A-Za-z0-9_])", body):
        distance = min((min(abs(match.start()-cue.end()), abs(cue.start()-match.end())) for cue in cues), default=999)
        if distance <= 70:
            candidates.append((distance, match.start(), match.group(1)))
    return min(candidates)[2] if candidates else None
