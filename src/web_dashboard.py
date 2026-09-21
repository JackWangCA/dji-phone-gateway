#!/usr/bin/env python3
"""Authenticated, LAN-only dashboard for the DJI phone gateway."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sms_bridge import Config, Telegram, ami_command, safe_phone, safe_sms

HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
SETTINGS_PATH = Path(os.getenv("GATEWAY_SETTINGS", "/var/lib/dji-phone-gateway/settings.json"))
SMS_DATABASE = Path(os.getenv("SMS_DATABASE", "/var/lib/dji-phone-gateway/messages.sqlite3"))
ASSET_DIR = Path(__file__).resolve().parent
CSRF_TOKEN = secrets.token_urlsafe(32)
SETTINGS_LOCK = threading.Lock()
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
SESSION_COOKIE = "dji_gateway_session"
SESSION_TTL = 30 * 24 * 60 * 60


def run(command: list[str], timeout: int = 15) -> tuple[int, str]:
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    return result.returncode, (result.stdout + result.stderr).strip()


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def update_settings(values: dict[str, object]) -> None:
    with SETTINGS_LOCK:
        settings = load_settings()
        settings.update(values)
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings) + "\n", encoding="utf-8")
        temporary.chmod(0o640)
        temporary.replace(SETTINGS_PATH)


def save_settings(token: str, chat_id: int) -> None:
    update_settings({"telegram_bot_token": token, "telegram_chat_id": chat_id})


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (16384, 8, 1):
            return False
        actual = hashlib.scrypt(
            password.encode(), salt=base64.urlsafe_b64decode(salt),
            n=int(n), r=int(r), p=int(p), dklen=32,
        )
        return secrets.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError, binascii.Error):
        return False


def dashboard_username(settings: dict | None = None) -> str:
    settings = load_settings() if settings is None else settings
    return str(settings.get("dashboard_username") or os.getenv("DASHBOARD_USERNAME", "admin"))


def save_dashboard_credentials(username: str, password: str, confirmation: str) -> None:
    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username must be 3–32 letters, numbers, dots, underscores, or hyphens.")
    if password != confirmation:
        raise ValueError("The new passwords do not match.")
    if not 6 <= len(password) <= 128:
        raise ValueError("Password must be 6–128 characters.")
    update_settings({"dashboard_username": username, "dashboard_password_hash": hash_password(password)})


def session_key(settings: dict, create: bool = False) -> bytes | None:
    secret = str(settings.get("dashboard_session_secret") or "")
    if not secret and create:
        secret = secrets.token_urlsafe(32)
        update_settings({"dashboard_session_secret": secret})
        settings["dashboard_session_secret"] = secret
    password_material = str(settings.get("dashboard_password_hash") or PASSWORD)
    if not secret or not password_material:
        return None
    return hashlib.sha256(f"{secret}|{password_material}".encode()).digest()


def create_session_token(username: str, remember: bool) -> tuple[str, int]:
    settings = load_settings()
    key = session_key(settings, create=True)
    if key is None:
        raise RuntimeError("Dashboard login is not configured.")
    lifetime = SESSION_TTL if remember else 24 * 60 * 60
    expires = int(time.time()) + lifetime
    payload = f"{username}\n{expires}".encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(key, encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}", lifetime


def verify_session_token(token: str) -> bool:
    try:
        encoded, signature = token.rsplit(".", 1)
        settings = load_settings()
        key = session_key(settings)
        if key is None or not hmac.compare_digest(signature, hmac.new(key, encoded.encode(), hashlib.sha256).hexdigest()):
            return False
        padding = "=" * (-len(encoded) % 4)
        username, expiry = base64.urlsafe_b64decode(encoded + padding).decode().split("\n", 1)
        return secrets.compare_digest(username, dashboard_username(settings)) and int(expiry) >= int(time.time())
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False


def bridge_config() -> Config:
    settings = load_settings()
    return Config(
        token=str(settings.get("telegram_bot_token", "")),
        chat_id=int(settings.get("telegram_chat_id", 0) or 0),
        ami_user=os.environ["ASTERISK_AMI_USER"],
        ami_password=os.environ["ASTERISK_AMI_PASSWORD"],
        ami_host=os.getenv("ASTERISK_HOST", "127.0.0.1"),
        ami_port=int(os.getenv("ASTERISK_PORT", "5038")),
        modem=os.getenv("MODEM_NAME", "quectel0"),
        data_command=os.getenv("DATA_COMMAND", "/opt/dji-phone-gateway/bin/dji-data"),
    )


def service_state(name: str) -> str:
    _, output = run(["systemctl", "is-active", name], 5)
    return output or "unknown"


def ensure_message_schema(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "id INTEGER PRIMARY KEY, received_at TEXT DEFAULT CURRENT_TIMESTAMP, "
        "sender TEXT NOT NULL, body TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'incoming')"
    )
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(messages)")}
    if "direction" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN direction TEXT NOT NULL DEFAULT 'incoming'")


def messages(limit: int = 500) -> list[dict[str, object]]:
    try:
        with sqlite3.connect(SMS_DATABASE, timeout=3) as db:
            ensure_message_schema(db)
            rows = db.execute(
                "SELECT id, received_at, sender, body, direction FROM "
                "(SELECT id, received_at, sender, body, direction FROM messages ORDER BY id DESC LIMIT ?) "
                "ORDER BY id ASC",
                (limit,),
            ).fetchall()
        return [
            {
                "id": int(message_id),
                "timestamp": str(timestamp),
                "peer": str(peer),
                "body": str(body),
                "direction": "outgoing" if direction == "outgoing" else "incoming",
            }
            for message_id, timestamp, peer, body, direction in rows
        ]
    except sqlite3.Error:
        return []


def store_outgoing(peer: str, body: str) -> dict[str, object]:
    SMS_DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(SMS_DATABASE, timeout=3) as db:
        ensure_message_schema(db)
        cursor = db.execute(
            "INSERT INTO messages(sender, body, direction) VALUES (?, ?, 'outgoing')",
            (peer, body),
        )
        row = db.execute("SELECT id, received_at FROM messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {"id": int(row[0]), "timestamp": str(row[1]), "peer": peer, "body": body, "direction": "outgoing"}


def send_sms(number: str, body: str) -> dict[str, object]:
    number = number.strip().replace(" ", "").replace("-", "")
    body = safe_sms(body)
    if not safe_phone(number) or not body:
        raise ValueError("Enter a valid phone number and message.")
    if len(body) > 670:
        raise ValueError("Message is too long (maximum 670 characters).")
    cfg = bridge_config()
    output = ami_command(cfg, f"quectel sms send {cfg.modem} {number} {body}")
    if "SMS queued for send" not in output:
        raise RuntimeError(output[-1200:])
    try:
        return store_outgoing(number, body)
    except sqlite3.Error as exc:
        # The carrier send has already been accepted. Report that truthfully even
        # if local history is temporarily unavailable, instead of inviting a
        # duplicate retry from the user.
        return {
            "id": int(time.time() * 1000),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "peer": number,
            "body": body,
            "direction": "outgoing",
            "history_warning": f"SMS queued, but conversation history could not be updated: {exc}",
        }


LOGIN_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · DJI Phone Gateway</title><meta name="theme-color" content="#ffffff"><link rel="icon" type="image/svg+xml" href="/icon.svg"><link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png"><style>
:root{--paper:#fff;--field:#f7f7f8;--ink:#111;--muted:#686868;--accent:#e4002b}*{box-sizing:border-box}html,body{min-height:100%;background:var(--paper)}body{margin:0;color:var(--ink);font:16px/1.4 "Helvetica Neue",Helvetica,Arial,sans-serif}.login{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(320px,.65fr);width:min(1080px,calc(100% - 40px));min-height:650px;margin:clamp(20px,8vh,90px) auto;border:1px solid var(--ink)}.identity{position:relative;display:flex;align-items:flex-end;min-height:420px;border-left:20px solid var(--accent);border-right:1px solid var(--ink);padding:42px;overflow:hidden}.identity h1{position:relative;z-index:1;max-width:620px;margin:0;font-size:clamp(3rem,7vw,6.5rem);font-weight:700;line-height:.86;letter-spacing:-.075em}.folio{position:absolute;right:18px;top:18px;color:var(--field);font-size:clamp(9rem,22vw,17rem);font-weight:700;line-height:.8;letter-spacing:-.11em;user-select:none}.form-panel{display:flex;flex-direction:column;justify-content:flex-end;padding:38px}.form-panel h2{margin:0 0 8px;font-size:1.6rem;letter-spacing:-.025em}.hint{margin:0 0 26px;color:var(--muted);font-size:.9rem}.error{margin:0 0 18px;border-left:5px solid var(--accent);padding:10px 12px;background:var(--field);font-weight:700}label{display:block;margin:17px 0 7px;font-size:.75rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase}input[type=text],input[type=password]{width:100%;height:48px;border:1px solid var(--ink);border-radius:0;background:var(--paper);color:var(--ink);padding:10px 12px;font:inherit;outline:0}input:focus{border-color:var(--accent);box-shadow:inset 0 -3px 0 var(--accent)}.remember{display:flex;align-items:center;gap:9px;margin:18px 0 22px;font-size:.88rem;font-weight:600}.remember input{width:18px;height:18px;accent-color:var(--accent)}button{width:100%;min-height:48px;border:1px solid var(--ink);border-radius:0;background:var(--ink);color:#fff;font:700 1rem/1.2 "Helvetica Neue",Helvetica,Arial,sans-serif;cursor:pointer}button:hover,button:focus-visible{background:var(--accent);border-color:var(--accent);outline:0}@media(max-width:720px){.login{display:block;width:calc(100% - 28px);min-height:0;margin:14px auto}.identity{min-height:300px;border-right:0;border-bottom:1px solid var(--ink);padding:28px}.form-panel{padding:28px}}
</style></head><body><main class="login"><section class="identity"><div class="folio" aria-hidden="true">4G</div><h1>DJI Phone Gateway</h1></section><section class="form-panel"><h2>Sign in</h2><p class="hint">Use your dashboard username and password.</p>@@ERROR@@<form method="post" action="/login"><input type="hidden" name="csrf" value="@@CSRF@@"><input type="hidden" name="next" value="@@NEXT@@"><label for="username">Username</label><input id="username" name="username" type="text" autocomplete="username" required autofocus><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required><label class="remember"><input name="remember" type="checkbox" value="yes" checked>Keep me signed in for 30 days</label><button type="submit">Sign in</button></form></section></main></body></html>"""


def login_page(error: str = "", next_path: str = "/") -> str:
    safe_next = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
    return (LOGIN_HTML
            .replace("@@ERROR@@", f'<p class="error">{html.escape(error)}</p>' if error else "")
            .replace("@@CSRF@@", html.escape(CSRF_TOKEN, quote=True))
            .replace("@@NEXT@@", html.escape(safe_next, quote=True)))


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DJI Phone Gateway</title><meta name="theme-color" content="#ffffff"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="default"><meta name="apple-mobile-web-app-title" content="DJI Messages"><link rel="icon" type="image/svg+xml" href="/icon.svg"><link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png"><style>
:root{--paper:#fff;--field:#f7f7f8;--ink:#111;--muted:#686868;--accent:#e4002b;--line:#c9c9c9}
*{box-sizing:border-box}[hidden]{display:none!important}html{background:var(--paper)}body{margin:0;color:var(--ink);background:var(--paper);font:16px/1.4 "Helvetica Neue",Helvetica,Arial,sans-serif}button,input,textarea{font:inherit}button{border-radius:0}main{max-width:1280px;margin:0 auto;padding:0 32px 72px}.masthead{position:relative;display:grid;grid-template-columns:minmax(0,1fr) auto;min-height:260px;border-left:18px solid var(--accent);border-bottom:1px solid var(--ink);padding:32px 36px 22px}.brand{align-self:end;position:relative;z-index:1}h1{max-width:700px;margin:0;font-size:clamp(2.8rem,7vw,6.8rem);font-weight:700;line-height:.86;letter-spacing:-.075em}.host{margin:18px 0 0;font-size:.875rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase}.folio{align-self:start;margin-top:-19px;font-size:clamp(8rem,20vw,17rem);font-weight:700;line-height:.8;letter-spacing:-.11em;color:var(--field);user-select:none}
.primary-nav{display:flex;border-bottom:1px solid var(--ink)}.nav-tab{min-width:180px;border:0;border-right:1px solid var(--ink);background:var(--paper);color:var(--ink);padding:15px 20px;text-align:left;font-weight:700;cursor:pointer}.nav-tab[aria-selected="true"]{background:var(--ink);color:#fff}.nav-tab:hover,.nav-tab:focus-visible{background:var(--accent);color:#fff;outline:0}.notice{margin:0;border-bottom:1px solid var(--ink);padding:18px 36px;background:var(--accent);color:#fff;font-weight:700}.notice ul{margin:10px 0 0;padding-left:20px}code{font-family:"Helvetica Neue",Helvetica,Arial,sans-serif;font-variant-numeric:tabular-nums}.view[hidden]{display:none}
.nav-logout{margin:0 0 0 auto}.nav-logout input{display:none}.nav-logout button{height:100%;border:0;border-left:1px solid var(--ink);background:var(--paper);color:var(--ink);padding:0 20px;font-weight:700;cursor:pointer}.nav-logout button:hover,.nav-logout button:focus-visible{background:var(--accent);color:#fff;outline:0}
.section-head{display:grid;grid-template-columns:70px 1fr auto;align-items:baseline;border-bottom:1px solid var(--ink);padding:22px 0 14px}.section-no{color:var(--accent);font-size:1.1rem;font-weight:700;font-variant-numeric:tabular-nums}h2{margin:0;font-size:1.3rem;line-height:1.1;letter-spacing:-.025em}.status-block{border-bottom:1px solid var(--ink)}.status-grid{display:grid;grid-template-columns:repeat(5,1fr)}.status-item{min-width:0;min-height:118px;padding:18px 16px;border-right:1px solid var(--line)}.status-item:last-child{border-right:0}.status-label{display:block;min-height:38px;color:var(--muted);font-size:.75rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase}.status-value{display:block;margin-top:18px;font-size:1.05rem;overflow-wrap:anywhere}.bad{color:var(--accent)}.data-readout{display:grid;grid-template-columns:70px 1fr;padding:14px 0;border-top:1px solid var(--line);font-size:.85rem}.data-readout span{color:var(--muted);font-weight:700}.data-readout code{overflow-wrap:anywhere}
.control-grid{display:grid;grid-template-columns:.8fr 1.3fr 1fr;border-bottom:1px solid var(--ink)}.panel{min-width:0;padding:0 22px 28px;border-right:1px solid var(--ink)}.panel:first-child{padding-left:0}.panel:last-child{padding-right:0;border-right:0}.panel .section-head{grid-template-columns:54px 1fr}.hint{min-height:44px;margin:18px 0;color:var(--muted);font-size:.88rem}.form-status{margin:12px 0 0;color:var(--accent);font-size:.85rem;font-weight:700}label{display:block;margin:17px 0 7px;font-size:.75rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase}input,textarea{width:100%;border:1px solid var(--ink);border-radius:0;background:var(--paper);color:var(--ink);padding:10px 12px;outline:none}input{height:46px}textarea{min-height:90px;resize:vertical}input:focus,textarea:focus{border-color:var(--accent);box-shadow:inset 0 -3px 0 var(--accent)}input::placeholder,textarea::placeholder{color:#8a8a8a}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}.button{min-height:42px;border:1px solid var(--ink);background:var(--ink);color:#fff;padding:9px 14px;font-weight:700;cursor:pointer}.button:hover,.button:focus-visible{background:var(--accent);border-color:var(--accent)}.button.secondary{background:var(--paper);color:var(--ink)}.button.secondary:hover,.button.secondary:focus-visible{background:var(--ink);color:#fff}.button.danger{background:var(--accent);border-color:var(--accent)}form{margin:0}
.conversation-heading{padding-top:8px}.new-button{position:relative;width:44px;height:44px;border:1px solid var(--ink);background:var(--accent);cursor:pointer}.new-button::before,.new-button::after{content:"";position:absolute;left:12px;right:12px;top:20px;height:2px;background:#fff}.new-button::after{transform:rotate(90deg)}.new-button:hover,.new-button:focus-visible{background:var(--ink);outline:0}.conversation-shell{display:grid;grid-template-columns:minmax(260px,34%) minmax(0,1fr);height:min(680px,calc(100dvh - 205px));min-height:520px;border-bottom:1px solid var(--ink)}.conversation-index{min-width:0;min-height:0;border-right:1px solid var(--ink);overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch}.index-label,.thread-head{height:76px;border-bottom:1px solid var(--ink);padding:16px 18px}.index-label{display:flex;align-items:flex-end;color:var(--muted);font-size:.72rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase}.conversation-list{margin:0;padding:0;list-style:none}.conversation-row{position:relative;width:100%;border:0;border-bottom:1px solid var(--line);background:var(--paper);color:var(--ink);padding:17px 18px;text-align:left;cursor:pointer}.conversation-row:hover,.conversation-row:focus-visible{background:var(--field);outline:0}.conversation-row.active{background:var(--ink);color:#fff}.conversation-row.fresh::before{content:"";position:absolute;left:0;top:0;bottom:0;width:5px;background:var(--accent);animation:red-mark .75s ease both}.conversation-peer{display:flex;justify-content:space-between;gap:12px;font-weight:700}.conversation-time{flex:none;color:var(--muted);font-size:.72rem;font-weight:500}.active .conversation-time{color:#c9c9c9}.conversation-preview{margin-top:7px;color:var(--muted);font-size:.86rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.active .conversation-preview{color:#fff}.thread{display:grid;grid-template-rows:76px minmax(0,1fr) auto;min-width:0;min-height:0}.thread-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px}.thread-head strong{font-size:1.1rem}.thread-count{color:var(--muted);font-size:.75rem}.message-stream{display:flex;min-height:0;flex-direction:column;gap:12px;overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;touch-action:pan-y;padding:24px 22px}.empty-thread{margin:auto;color:var(--muted);max-width:280px;text-align:center}.message{max-width:min(78%,620px);border:1px solid var(--ink);padding:11px 13px 8px;overflow-wrap:anywhere}.message.incoming{align-self:flex-start;background:var(--paper)}.message.outgoing{align-self:flex-end;background:var(--ink);color:#fff}.message.pending{opacity:.55}.message-time{display:block;margin-top:7px;color:var(--muted);font-size:.68rem;font-variant-numeric:tabular-nums}.outgoing .message-time{color:#c9c9c9}.message.arriving{animation:message-arrive .42s cubic-bezier(.2,.8,.2,1) both}.composer{display:grid;grid-template-columns:1fr auto;border-top:1px solid var(--ink)}.composer textarea{min-height:76px;border:0;padding:17px 18px}.composer textarea:focus{box-shadow:inset 0 0 0 2px var(--accent)}.composer .button{height:100%;min-width:104px;border-width:0 0 0 1px}.composer[hidden]{display:none}.send-error{padding:9px 18px;background:var(--accent);color:#fff;font-weight:700}
dialog{width:min(540px,calc(100% - 28px));border:1px solid var(--ink);border-radius:0;padding:0;background:var(--paper);color:var(--ink)}dialog::backdrop{background:rgba(0,0,0,.48)}dialog[open]{animation:dialog-in .25s ease both}.dialog-head{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--ink);padding:18px 20px}.dialog-head h2{font-size:1.5rem}.dialog-close{position:relative;width:38px;height:38px;border:0;background:transparent;cursor:pointer}.dialog-close::before,.dialog-close::after{content:"";position:absolute;left:8px;right:8px;top:18px;height:2px;background:var(--ink);transform:rotate(45deg)}.dialog-close::after{transform:rotate(-45deg)}.dialog-close:hover::before,.dialog-close:hover::after,.dialog-close:focus-visible::before,.dialog-close:focus-visible::after{background:var(--accent)}.dialog-body{padding:2px 20px 22px}
@keyframes message-arrive{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}@keyframes red-mark{0%{transform:scaleY(0)}100%{transform:scaleY(1)}}@keyframes dialog-in{from{opacity:0;transform:translateY(18px)}to{opacity:1;transform:none}}
@media(max-width:850px){main{padding:0 20px 48px}.masthead{min-height:210px;padding:26px 24px 20px;border-left-width:12px}.folio{font-size:8rem}.status-grid{grid-template-columns:repeat(2,1fr)}.status-item{border-bottom:1px solid var(--line)}.status-item:nth-child(even){border-right:0}.status-item:last-child{border-right:1px solid var(--line)}.control-grid{grid-template-columns:1fr}.panel,.panel:first-child,.panel:last-child{padding:0 0 26px;border-right:0;border-bottom:1px solid var(--ink)}.panel:last-child{border-bottom:0}.conversation-shell{grid-template-columns:230px minmax(0,1fr)}.message{max-width:88%}}
@media(max-width:620px){main{padding:0 14px 40px}.masthead{display:block;min-height:230px;padding:24px 18px 18px;overflow:hidden}.brand{position:absolute;left:18px;right:14px;bottom:20px}.folio{position:absolute;right:10px;top:15px;margin:0;font-size:8.4rem}h1{font-size:3rem;max-width:300px}.host{font-size:.7rem}.primary-nav{display:grid;grid-template-columns:1fr 1fr}.nav-tab{min-width:0}.status-grid{grid-template-columns:1fr}.status-item,.status-item:nth-child(even),.status-item:last-child{min-height:92px;border-right:0}.status-label{min-height:auto}.status-value{margin-top:10px}.section-head{grid-template-columns:48px 1fr auto}.data-readout{grid-template-columns:1fr;gap:6px}.conversation-shell{display:block;height:auto;min-height:0}.conversation-index{max-height:270px;border-right:0;border-bottom:1px solid var(--ink)}.thread{height:560px}.index-label{height:54px}.thread-head{height:66px}.message-stream{padding:18px 12px}.message{max-width:92%}.composer{grid-template-columns:1fr}.composer .button{min-height:52px;border-width:1px 0 0}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;animation-duration:.01ms!important;animation-iteration-count:1!important}}
</style></head><body><main><header class="masthead"><div class="brand"><h1>DJI Phone Gateway</h1><p class="host">Private cellular gateway · smspi</p></div><div class="folio" aria-hidden="true">4G</div></header>
<nav class="primary-nav" aria-label="Gateway sections"><button class="nav-tab" data-view="dashboard" aria-selected="false">Dashboard</button><button class="nav-tab" data-view="conversations" aria-selected="true">Conversations</button><form class="nav-logout" method="post" action="/logout"><input type="hidden" name="csrf" value="@@CSRF@@"><button type="submit">Sign out</button></form></nav>
@@BANNER@@@@CHATS@@
<div id="dashboard-view" class="view"><section class="status-block"><div class="section-head"><span class="section-no">00</span><h2>Gateway status</h2></div><div class="status-grid">@@STATUS@@</div><div class="data-readout"><span>Data</span><code>@@DATA_STATUS@@</code></div></section>
<div class="control-grid"><section class="panel"><div class="section-head"><span class="section-no">01</span><h2>Cellular data</h2></div><p class="hint">Mobile data stays off unless you enable it here.</p><form method="post" action="/data"><input type="hidden" name="csrf" value="@@CSRF@@"><div class="actions"><button class="button secondary" name="state" value="off">Data off</button><button class="button danger" name="state" value="on">Data on</button></div></form></section>
<section class="panel"><div class="section-head"><span class="section-no">02</span><h2>Telegram setup</h2></div><p class="hint">Paste the token from @BotFather. Your saved token is never displayed.</p><form method="post" action="/telegram"><input type="hidden" name="csrf" value="@@CSRF@@"><label>Bot token</label><input type="password" name="token" placeholder="@@TOKEN_PLACEHOLDER@@" autocomplete="off"><label>Authorized chat ID</label><input name="chat_id" value="@@CHAT_ID@@" inputmode="numeric"><div class="actions"><button class="button" name="action" value="save">Save settings</button><button class="button secondary" name="action" value="test">Send test</button><button class="button secondary" name="action" value="discover">Find chat IDs</button></div></form></section>
<section class="panel"><div class="section-head"><span class="section-no">03</span><h2>Dashboard access</h2></div><p class="hint">Change the username and password used by this dashboard.</p><form id="access-form"><label>Username</label><input name="username" value="@@DASHBOARD_USERNAME@@" autocomplete="username" required><label>New password</label><input type="password" name="password" minlength="6" maxlength="128" autocomplete="new-password" required><label>Confirm password</label><input type="password" name="confirmation" minlength="6" maxlength="128" autocomplete="new-password" required><p id="access-status" class="form-status" hidden></p><div class="actions"><button class="button">Change login</button></div></form></section></div></div>
<div id="conversations-view" class="view" hidden><section class="conversation-heading"><div class="section-head"><span class="section-no">04</span><h2>Conversations</h2><button id="new-message" class="new-button" aria-label="New message" title="New message"></button></div><div class="conversation-shell"><aside class="conversation-index"><div class="index-label">Recent conversations</div><ol id="conversation-list" class="conversation-list"></ol></aside><article class="thread"><header class="thread-head"><strong id="thread-peer">Select a conversation</strong><span id="thread-count" class="thread-count"></span></header><div id="message-stream" class="message-stream"><p class="empty-thread">Incoming and sent messages will appear here.</p></div><div id="send-error" class="send-error" hidden></div><form id="reply-form" class="composer" hidden><textarea name="message" maxlength="670" aria-label="Message" placeholder="Message" required></textarea><button class="button">Send</button></form></article></div></section></div>
<dialog id="new-dialog"><div class="dialog-head"><h2>New message</h2><button class="dialog-close" type="button" aria-label="Close"></button></div><form id="new-form" class="dialog-body"><label>Phone number</label><input name="number" inputmode="tel" autocomplete="tel" placeholder="+15551234567" required><label>Message</label><textarea name="message" maxlength="670" required></textarea><div class="actions"><button class="button">Send</button><button class="button secondary cancel-new" type="button">Cancel</button></div></form></dialog>
<script>
const csrf=@@CSRF_JSON@@;let allMessages=@@MESSAGES_JSON@@;let selectedPeer=null;let knownIds=new Set(allMessages.map(message=>message.id));let freshPeers=new Set();const list=document.getElementById('conversation-list'),stream=document.getElementById('message-stream'),replyForm=document.getElementById('reply-form'),sendError=document.getElementById('send-error');
function displayNumber(value){const digits=String(value).replace(/\D/g,'');if(digits.length===11&&digits[0]==='1')return `+1 (${digits.slice(1,4)}) ${digits.slice(4,7)}-${digits.slice(7)}`;if(digits.length===10)return `(${digits.slice(0,3)}) ${digits.slice(3,6)}-${digits.slice(6)}`;return value}
function timeLabel(value){const parsed=new Date(String(value).replace(' ','T')+(String(value).includes('T')?'':'Z'));if(Number.isNaN(parsed.getTime()))return value;const today=new Date();return parsed.toDateString()===today.toDateString()?parsed.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}):parsed.toLocaleDateString([],{month:'short',day:'numeric'})}
function groups(){const map=new Map();for(const message of allMessages){if(!map.has(message.peer))map.set(message.peer,[]);map.get(message.peer).push(message)}return [...map.entries()].sort((a,b)=>b[1][b[1].length-1].id-a[1][a[1].length-1].id)}
function switchView(name){document.querySelectorAll('.view').forEach(view=>view.hidden=view.id!==`${name}-view`);document.querySelectorAll('.nav-tab').forEach(tab=>tab.setAttribute('aria-selected',String(tab.dataset.view===name)));history.replaceState(null,'',name==='conversations'?'#conversations':'#dashboard');if(name==='conversations'&&!selectedPeer&&groups().length)selectPeer(groups()[0][0])}
document.querySelectorAll('.nav-tab').forEach(tab=>tab.addEventListener('click',()=>switchView(tab.dataset.view)));
function renderList(){list.replaceChildren();const entries=groups();if(!entries.length){const item=document.createElement('li');item.className='conversation-row';item.textContent='No conversations yet.';list.append(item);return}for(const [peer,items] of entries){const last=items[items.length-1],item=document.createElement('li'),button=document.createElement('button');button.className='conversation-row'+(peer===selectedPeer?' active':'')+(freshPeers.has(peer)?' fresh':'');button.type='button';const top=document.createElement('span');top.className='conversation-peer';const name=document.createElement('span');name.textContent=displayNumber(peer);const time=document.createElement('span');time.className='conversation-time';time.textContent=timeLabel(last.timestamp);top.append(name,time);const preview=document.createElement('div');preview.className='conversation-preview';preview.textContent=(last.direction==='outgoing'?'You: ':'')+last.body;button.append(top,preview);button.addEventListener('click',()=>selectPeer(peer));item.append(button);list.append(item)}}
function selectPeer(peer,animateIds=new Set(),preserveScroll=false){const previousTop=stream.scrollTop;selectedPeer=peer;freshPeers.delete(peer);document.getElementById('thread-peer').textContent=displayNumber(peer);const items=allMessages.filter(message=>message.peer===peer);document.getElementById('thread-count').textContent=`${items.length} ${items.length===1?'message':'messages'}`;stream.replaceChildren();for(const message of items){const block=document.createElement('div');block.className=`message ${message.direction}`+(message.pending?' pending':'')+(animateIds.has(message.id)?' arriving':'');const text=document.createElement('div');text.textContent=message.body;const time=document.createElement('span');time.className='message-time';time.textContent=message.pending?'Sending…':timeLabel(message.timestamp);block.append(text,time);stream.append(block)}replyForm.hidden=false;renderList();requestAnimationFrame(()=>{stream.scrollTop=preserveScroll?previousTop:stream.scrollHeight})}
async function fetchMessages(){try{const response=await fetch('/messages',{cache:'no-store'});if(!response.ok)return;const next=await response.json();if(JSON.stringify(next)===JSON.stringify(allMessages))return;const newIds=new Set(),wasReadingHistory=stream.scrollHeight-stream.scrollTop-stream.clientHeight>64;for(const message of next){if(!knownIds.has(message.id)){newIds.add(message.id);freshPeers.add(message.peer)}}allMessages=next;knownIds=new Set(next.map(message=>message.id));renderList();if(selectedPeer)selectPeer(selectedPeer,newIds,wasReadingHistory)}catch(_){}}
async function transmit(number,body){const data=new URLSearchParams({csrf,number,message:body});const response=await fetch('/api/sms',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:data});const result=await response.json();if(!response.ok)throw new Error(result.error||'Message could not be sent.');if(result.message.history_warning){sendError.textContent=result.message.history_warning;sendError.hidden=false}return result.message}
replyForm.addEventListener('submit',async event=>{event.preventDefault();const textarea=replyForm.elements.message,body=textarea.value.trim();if(!body||!selectedPeer)return;sendError.hidden=true;const temporary={id:Date.now(),timestamp:new Date().toISOString(),peer:selectedPeer,body,direction:'outgoing',pending:true};allMessages.push(temporary);textarea.value='';selectPeer(selectedPeer,new Set([temporary.id]));try{const saved=await transmit(selectedPeer,body);allMessages=allMessages.filter(message=>message!==temporary);allMessages.push(saved);knownIds.add(saved.id);selectPeer(selectedPeer,new Set([saved.id]))}catch(error){allMessages=allMessages.filter(message=>message!==temporary);selectPeer(selectedPeer);sendError.textContent=error.message;sendError.hidden=false}});
const dialog=document.getElementById('new-dialog'),newForm=document.getElementById('new-form');document.getElementById('new-message').addEventListener('click',()=>dialog.showModal());document.querySelector('.dialog-close').addEventListener('click',()=>dialog.close());document.querySelector('.cancel-new').addEventListener('click',()=>dialog.close());newForm.addEventListener('submit',async event=>{event.preventDefault();const number=newForm.elements.number.value.trim(),body=newForm.elements.message.value.trim(),submit=newForm.querySelector('button');submit.disabled=true;try{const saved=await transmit(number,body);allMessages.push(saved);knownIds.add(saved.id);newForm.reset();dialog.close();selectPeer(saved.peer,new Set([saved.id]));switchView('conversations')}catch(error){alert(error.message)}finally{submit.disabled=false}});
const accessForm=document.getElementById('access-form'),accessStatus=document.getElementById('access-status');accessForm.addEventListener('submit',async event=>{event.preventDefault();accessStatus.hidden=true;const data=new URLSearchParams({csrf,username:accessForm.elements.username.value,password:accessForm.elements.password.value,confirmation:accessForm.elements.confirmation.value});const response=await fetch('/api/access',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:data});const result=await response.json();if(!response.ok){accessStatus.textContent=result.error||'Login could not be changed.';accessStatus.hidden=false;return}accessStatus.textContent='Login changed. Sign in again with the new credentials.';accessStatus.hidden=false;accessForm.elements.password.value='';accessForm.elements.confirmation.value='';setTimeout(()=>location.reload(),1200)});
renderList();switchView(location.hash==='#dashboard'?'dashboard':'conversations');setInterval(fetchMessages,4000);document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')fetchMessages()});
</script></main></body></html>"""


def page(message: str = "", discovered: list[dict] | None = None) -> str:
    settings = load_settings()
    token_set = bool(settings.get("telegram_bot_token"))
    chat_id = settings.get("telegram_chat_id", "")
    _, data_status = run(["sudo", "-n", os.getenv("DATA_COMMAND", "/opt/dji-phone-gateway/bin/dji-data"), "status"])
    modem_present = Path("/dev/dji-modem-at").exists()
    audio_present = Path("/dev/dji-modem-audio").exists()
    status = "".join(
        f'<div class="status-item"><span class="status-label">{html.escape(label)}</span><strong class="status-value {"bad" if bad else ""}">{html.escape(value)}</strong></div>'
        for label, value, bad in (
            ("Modem AT interface", "Connected" if modem_present else "Missing", not modem_present),
            ("Serial audio interface", "Connected" if audio_present else "Missing", not audio_present),
            ("Asterisk", service_state("asterisk"), False),
            ("Telegram bridge", service_state("dji-sms-bridge"), False),
            ("Telegram configured", "Yes" if token_set and chat_id else "No", False),
        )
    )
    chats = ""
    if discovered is not None:
        items = "".join(f"<li><code>{html.escape(str(item['id']))}</code> — {html.escape(item.get('label', 'Telegram chat'))}</li>" for item in discovered)
        chats = f'<div class="notice"><strong>Recent chats</strong><ul>{items or "<li>None. Send the bot a message, then try again.</li>"}</ul></div>'
    replacements = {
        "@@BANNER@@": f'<div class="notice">{html.escape(message)}</div>' if message else "", "@@CHATS@@": chats,
        "@@STATUS@@": status, "@@DATA_STATUS@@": html.escape(data_status), "@@CSRF@@": html.escape(CSRF_TOKEN, quote=True),
        "@@CSRF_JSON@@": json.dumps(CSRF_TOKEN), "@@TOKEN_PLACEHOLDER@@": "Saved — leave blank to keep it" if token_set else "123456:ABC…",
        "@@CHAT_ID@@": html.escape(str(chat_id), quote=True), "@@DASHBOARD_USERNAME@@": html.escape(dashboard_username(settings), quote=True),
        "@@MESSAGES_JSON@@": json.dumps(messages()).replace("</", "<\\/"),
    }
    document = HTML
    for marker, value in replacements.items():
        document = document.replace(marker, value)
    return document


class Handler(BaseHTTPRequestHandler):
    server_version = "DJIGateway/1.0"

    def respond_asset(self, filename: str, content_type: str) -> None:
        try:
            body = (ASSET_DIR / filename).read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            session = cookies.get(SESSION_COOKIE)
            return session is not None and verify_session_token(session.value)
        except (CookieError, KeyError, TypeError):
            return False

    def credentials_match(self, username: str, password: str) -> bool:
        settings = load_settings()
        expected_username = dashboard_username(settings)
        encoded_password = str(settings.get("dashboard_password_hash") or "")
        password_matches = verify_password(password, encoded_password) if encoded_password else bool(PASSWORD) and secrets.compare_digest(password, PASSWORD)
        return secrets.compare_digest(username, expected_username) and password_matches

    def request_is_secure(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https"

    def redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def authenticate(self) -> bool:
        if self.authorized():
            return True
        if self.path.startswith("/api/") or urllib.parse.urlsplit(self.path).path == "/messages":
            self.respond_json({"error": "Sign in to continue."}, HTTPStatus.UNAUTHORIZED)
        else:
            self.redirect("/login?next=" + urllib.parse.quote(self.path, safe=""))
        return False

    def respond(self, content: str, status: int = 200) -> None:
        body = content.encode(); self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "DENY"); self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def respond_json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value).encode(); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def form(self) -> dict[str, str]:
        length = min(int(self.headers.get("Content-Length", "0")), 16384); values = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"), keep_blank_values=True); return {key: items[-1] for key, items in values.items()}

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/icon.svg":
            self.respond_asset("dji-phone-gateway-icon.svg", "image/svg+xml")
            return
        if path == "/apple-touch-icon.png":
            self.respond_asset("apple-touch-icon.png", "image/png")
            return
        if path == "/login":
            if self.authorized():
                self.redirect("/")
                return
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self.respond(login_page(next_path=query.get("next", ["/"])[-1]))
            return
        if not self.authenticate(): return
        if path == "/": self.respond(page())
        elif path == "/messages": self.respond_json(messages())
        else: self.send_error(404)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        form = self.form()
        if path == "/login":
            if not secrets.compare_digest(form.get("csrf", ""), CSRF_TOKEN):
                self.respond(login_page("This sign-in page expired. Reload it and try again.", form.get("next", "/")), 403)
                return
            username, password = form.get("username", ""), form.get("password", "")
            if not self.credentials_match(username, password):
                self.respond(login_page("The username or password is incorrect.", form.get("next", "/")), 401)
                return
            remember = form.get("remember") == "yes"
            token, lifetime = create_session_token(username, remember)
            cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
            if remember:
                cookie += f"; Max-Age={lifetime}"
            if self.request_is_secure():
                cookie += "; Secure"
            next_path = form.get("next", "/")
            if not next_path.startswith("/") or next_path.startswith("//"):
                next_path = "/"
            self.redirect(next_path, cookie)
            return
        if not self.authenticate(): return
        if path == "/logout":
            if not secrets.compare_digest(form.get("csrf", ""), CSRF_TOKEN):
                self.respond(page("Request expired; reload the dashboard."), 403)
                return
            cookie = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            if self.request_is_secure():
                cookie += "; Secure"
            self.redirect("/login", cookie)
            return
        is_api = path.startswith("/api/")
        if not secrets.compare_digest(form.get("csrf", ""), CSRF_TOKEN):
            if is_api: self.respond_json({"error": "Request expired; reload the dashboard."}, 403)
            else: self.respond(page("Request expired; reload the dashboard."), 403)
            return
        try:
            if path == "/data":
                state = form.get("state")
                if state not in ("on", "off"): raise ValueError("Invalid data state")
                code, output = run(["sudo", "-n", os.getenv("DATA_COMMAND", "/opt/dji-phone-gateway/bin/dji-data"), state], 30)
                if code: raise RuntimeError(output)
                self.respond(page(output))
            elif path == "/telegram":
                settings = load_settings(); token = form.get("token", "").strip() or str(settings.get("telegram_bot_token", ""))
                if not token: raise ValueError("Enter the bot token from @BotFather.")
                tg = Telegram(token); identity = tg.call("getMe", {})["result"]
                if form.get("action") == "discover":
                    updates = tg.call("getUpdates", {"timeout": 0, "allowed_updates": json.dumps(["message"])})["result"]; found = {}
                    for update in updates:
                        chat = update.get("message", {}).get("chat", {})
                        if "id" in chat:
                            label = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or "Telegram chat"; found[int(chat["id"])] = {"id": int(chat["id"]), "label": label}
                    self.respond(page(f"Connected to @{identity.get('username', 'bot')}. Select a chat ID below.", list(found.values())))
                elif form.get("action") == "test":
                    chat_id = int(form.get("chat_id", "").strip() or settings.get("telegram_chat_id", 0))
                    if not chat_id: raise ValueError("Enter the authorized chat ID before sending a test.")
                    tg.send(chat_id, "Test message: Telegram notifications are working."); self.respond(page(f"Test notification sent through @{identity.get('username', 'bot')}."))
                else:
                    chat_id = int(form.get("chat_id", "").strip()); save_settings(token, chat_id); run(["sudo", "-n", "/usr/bin/systemctl", "restart", "dji-sms-bridge.service"], 15); self.respond(page(f"Telegram settings saved for @{identity.get('username', 'bot')}."))
            elif path in ("/sms", "/api/sms"):
                saved = send_sms(form.get("number", ""), form.get("message", ""))
                if is_api: self.respond_json({"message": saved})
                else: self.respond(page("SMS queued."))
            elif path == "/api/access":
                save_dashboard_credentials(form.get("username", ""), form.get("password", ""), form.get("confirmation", ""))
                self.respond_json({"ok": True})
            else: self.send_error(404)
        except (ValueError, RuntimeError, OSError, KeyError, sqlite3.Error, urllib.error.URLError) as exc:
            if is_api: self.respond_json({"error": str(exc)}, 400)
            else: self.respond(page(f"Error: {exc}"), 400)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.client_address[0]} {fmt % args}", flush=True)


if __name__ == "__main__":
    saved_password = str(load_settings().get("dashboard_password_hash") or "")
    if (not PASSWORD or PASSWORD == "CHANGE_ME") and not saved_password: raise SystemExit("DASHBOARD_PASSWORD is not configured")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
