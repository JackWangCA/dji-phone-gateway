#!/usr/bin/env python3
"""Authenticated, LAN-only dashboard for the DJI phone gateway."""

from __future__ import annotations

import base64
import binascii
import html
import json
import os
import secrets
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sms_bridge import Config, Telegram, ami_command, safe_phone, safe_sms

HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
SETTINGS_PATH = Path(os.getenv("GATEWAY_SETTINGS", "/var/lib/dji-phone-gateway/settings.json"))
SMS_DATABASE = Path(os.getenv("SMS_DATABASE", "/var/lib/dji-phone-gateway/messages.sqlite3"))
CSRF_TOKEN = secrets.token_urlsafe(32)


def run(command: list[str], timeout: int = 15) -> tuple[int, str]:
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    return result.returncode, (result.stdout + result.stderr).strip()


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def save_settings(token: str, chat_id: int) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"telegram_bot_token": token, "telegram_chat_id": chat_id}) + "\n", encoding="utf-8")
    temporary.chmod(0o640)
    temporary.replace(SETTINGS_PATH)


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


def inbox() -> list[tuple]:
    try:
        with sqlite3.connect(f"file:{SMS_DATABASE}?mode=ro", uri=True) as db:
            return db.execute("SELECT received_at, sender, body FROM messages ORDER BY id DESC LIMIT 50").fetchall()
    except sqlite3.Error:
        return []


def page(message: str = "", discovered: list[dict] | None = None) -> str:
    settings = load_settings()
    token_set = bool(settings.get("telegram_bot_token"))
    chat_id = settings.get("telegram_chat_id", "")
    _, data_status = run(["sudo", "-n", os.getenv("DATA_COMMAND", "/opt/dji-phone-gateway/bin/dji-data"), "status"])
    modem_present = Path("/dev/dji-modem-at").exists()
    audio_present = Path("/dev/dji-modem-audio").exists()
    rows = "".join(
        f"<tr><td>{html.escape(str(date))}</td><td>{html.escape(str(sender))}</td><td>{html.escape(str(body))}</td></tr>"
        for date, sender, body in inbox()
    ) or '<tr><td colspan="3" class="muted">No locally received messages yet.</td></tr>'
    chats = ""
    if discovered is not None:
        items = "".join(
            f"<li><code>{html.escape(str(item['id']))}</code> — {html.escape(item.get('label', 'Telegram chat'))}</li>"
            for item in discovered
        )
        chats = f'<div class="notice"><strong>Recent chats</strong><ul>{items or "<li>None. Send the bot a message, then try again.</li>"}</ul></div>'
    banner = f'<div class="notice">{html.escape(message)}</div>' if message else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DJI Phone Gateway</title><link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23151c30'/%3E%3Cpath d='M17 23h30v18H17z' fill='none' stroke='%2370a5ff' stroke-width='5'/%3E%3Ccircle cx='24' cy='32' r='3' fill='%2358d68d'/%3E%3Cpath d='M33 29h9M33 35h9' stroke='%23edf2ff' stroke-width='3'/%3E%3C/svg%3E"><style>
:root{{--bg:#0b1020;--card:#151c30;--text:#edf2ff;--muted:#9ba8c7;--accent:#70a5ff;--ok:#58d68d;--bad:#ff7b86;--line:#293453}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(135deg,#08101f,#111a31);color:var(--text);font:16px system-ui,sans-serif}}
main{{max-width:980px;margin:auto;padding:28px 18px 60px}}h1{{font-size:28px;margin:0 0 4px}}h2{{font-size:17px;margin:0 0 16px}}.muted{{color:var(--muted)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-top:22px}}.card{{background:rgba(21,28,48,.94);border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 12px 30px #0004}}
.status{{display:grid;grid-template-columns:1fr auto;gap:10px}}.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}label{{display:block;color:var(--muted);margin:11px 0 5px}}
input{{width:100%;padding:11px;border-radius:9px;border:1px solid var(--line);background:#0c1325;color:var(--text)}}button{{border:0;border-radius:9px;background:var(--accent);color:#061021;font-weight:700;padding:10px 14px;margin:10px 6px 0 0;cursor:pointer}}button.secondary{{background:#2a3656;color:var(--text)}}button.danger{{background:#ff9c72}}
.notice{{background:#1e2b48;border-left:4px solid var(--accent);padding:12px;margin:16px 0;border-radius:8px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}code{{color:#a8c7ff}}form{{margin:0}}
</style></head><body><main><h1>DJI Phone Gateway</h1><div class="muted">Private cellular gateway on smspi</div>{banner}{chats}
<div class="grid"><section class="card"><h2>Gateway status</h2><div class="status">
<span>Modem AT interface</span><b class="{'ok' if modem_present else 'bad'}">{'Connected' if modem_present else 'Missing'}</b>
<span>Serial audio interface</span><b class="{'ok' if audio_present else 'bad'}">{'Connected' if audio_present else 'Missing'}</b>
<span>Asterisk</span><b>{html.escape(service_state('asterisk'))}</b><span>Telegram bridge</span><b>{html.escape(service_state('dji-sms-bridge'))}</b>
<span>Telegram configured</span><b>{'Yes' if token_set and chat_id else 'No'}</b></div><p class="muted">{html.escape(data_status)}</p>
<form method="post" action="/data"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><button name="state" value="off" class="secondary">Data off</button><button name="state" value="on" class="danger">Data on</button></form></section>
<section class="card"><h2>Telegram setup</h2><p class="muted">Paste the token from @BotFather. Your saved token is never displayed.</p>
<form method="post" action="/telegram"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><label>Bot token</label><input type="password" name="token" placeholder="{'Saved — leave blank to keep it' if token_set else '123456:ABC…'}" autocomplete="off"><label>Authorized chat ID</label><input name="chat_id" value="{html.escape(str(chat_id))}" inputmode="numeric"><button name="action" value="save">Save & send test</button><button name="action" value="discover" class="secondary">Find chat IDs</button></form></section>
<section class="card"><h2>Send SMS</h2><form method="post" action="/sms"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><label>Phone number</label><input name="number" placeholder="+15551234567"><label>Message</label><input name="message" maxlength="670"><button>Send SMS</button></form></section></div>
<section class="card" style="margin-top:16px"><h2>Received SMS</h2><table><thead><tr><th>Received</th><th>From</th><th>Message</th></tr></thead><tbody id="sms-inbox">{rows}</tbody></table></section>
<script>
async function refreshInbox() {{
  if (document.visibilityState !== 'visible') return;
  try {{
    const response = await fetch('/messages', {{cache: 'no-store'}});
    if (!response.ok) return;
    const messages = await response.json();
    const body = document.getElementById('sms-inbox');
    body.replaceChildren();
    if (!messages.length) {{
      const row = body.insertRow();
      const cell = row.insertCell();
      cell.colSpan = 3;
      cell.className = 'muted';
      cell.textContent = 'No locally received messages yet.';
      return;
    }}
    for (const message of messages) {{
      const row = body.insertRow();
      for (const value of [message.received_at, message.sender, message.body]) {{
        row.insertCell().textContent = value;
      }}
    }}
  }} catch (_) {{}}
}}
setInterval(refreshInbox, 5000);
</script>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "DJIGateway/1.0"

    def authorized(self) -> bool:
        if not PASSWORD:
            return False
        header = self.headers.get("Authorization", "")
        try:
            scheme, encoded = header.split(" ", 1)
            username, password = base64.b64decode(encoded).decode().split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        return scheme.lower() == "basic" and secrets.compare_digest(username, "admin") and secrets.compare_digest(password, PASSWORD)

    def authenticate(self) -> bool:
        if self.authorized():
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="DJI Phone Gateway"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def respond(self, content: str, status: int = 200) -> None:
        body = content.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, value: object) -> None:
        body = json.dumps(value).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def form(self) -> dict[str, str]:
        length = min(int(self.headers.get("Content-Length", "0")), 16384)
        values = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"), keep_blank_values=True)
        return {key: items[-1] for key, items in values.items()}

    def do_GET(self) -> None:
        if not self.authenticate():
            return
        if self.path == "/":
            self.respond(page())
        elif self.path == "/messages":
            self.respond_json(
                [
                    {"received_at": str(received_at), "sender": str(sender), "body": str(body)}
                    for received_at, sender, body in inbox()
                ]
            )
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if not self.authenticate():
            return
        form = self.form()
        if not secrets.compare_digest(form.get("csrf", ""), CSRF_TOKEN):
            self.respond(page("Request expired; reload the dashboard."), 403)
            return
        try:
            if self.path == "/data":
                state = form.get("state")
                if state not in ("on", "off"):
                    raise ValueError("Invalid data state")
                code, output = run(["sudo", "-n", os.getenv("DATA_COMMAND", "/opt/dji-phone-gateway/bin/dji-data"), state], 30)
                if code:
                    raise RuntimeError(output)
                self.respond(page(output))
            elif self.path == "/telegram":
                settings = load_settings()
                token = form.get("token", "").strip() or str(settings.get("telegram_bot_token", ""))
                if not token:
                    raise ValueError("Enter the bot token from @BotFather.")
                tg = Telegram(token)
                identity = tg.call("getMe", {})["result"]
                if form.get("action") == "discover":
                    updates = tg.call("getUpdates", {"timeout": 0, "allowed_updates": json.dumps(["message"])})["result"]
                    found = {}
                    for update in updates:
                        chat = update.get("message", {}).get("chat", {})
                        if "id" in chat:
                            label = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or "Telegram chat"
                            found[int(chat["id"])] = {"id": int(chat["id"]), "label": label}
                    self.respond(page(f"Connected to @{identity.get('username', 'bot')}. Select a chat ID below.", list(found.values())))
                else:
                    chat_id = int(form.get("chat_id", "").strip())
                    tg.send(chat_id, "DJI phone gateway connected successfully.")
                    save_settings(token, chat_id)
                    run(["sudo", "-n", "/usr/bin/systemctl", "restart", "dji-sms-bridge.service"], 15)
                    self.respond(page(f"Telegram connected to @{identity.get('username', 'bot')} and test message sent."))
            elif self.path == "/sms":
                number = form.get("number", "").strip()
                message = safe_sms(form.get("message", ""))
                if not safe_phone(number) or not message:
                    raise ValueError("Enter a valid phone number and message.")
                if len(message) > 670:
                    raise ValueError("Message is too long (maximum 670 characters).")
                cfg = bridge_config()
                output = ami_command(cfg, f"quectel sms send {cfg.modem} {number} {message}")
                if "SMS queued for send" not in output:
                    raise RuntimeError(output[-1200:])
                self.respond(page("SMS queued."))
            else:
                self.send_error(404)
        except (ValueError, RuntimeError, OSError, KeyError, urllib.error.URLError) as exc:
            self.respond(page(f"Error: {exc}"), 400)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.client_address[0]} {fmt % args}", flush=True)


if __name__ == "__main__":
    if not PASSWORD or PASSWORD == "CHANGE_ME":
        raise SystemExit("DASHBOARD_PASSWORD is not configured")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
