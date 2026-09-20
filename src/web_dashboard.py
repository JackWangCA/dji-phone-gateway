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
<title>DJI Phone Gateway</title><link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' fill='%23fff'/%3E%3Crect width='15' height='64' fill='%23E4002B'/%3E%3Cpath d='M24 18h27v28H24z' fill='none' stroke='%23000' stroke-width='5'/%3E%3C/svg%3E"><style>
:root{{--paper:#fff;--field:#f7f7f8;--ink:#111;--muted:#686868;--accent:#e4002b;--line:#c9c9c9}}
*{{box-sizing:border-box}}html{{background:var(--paper)}}body{{margin:0;color:var(--ink);background:var(--paper);font:16px/1.4 "Helvetica Neue",Helvetica,Arial,sans-serif}}
button,input{{font:inherit}}main{{max-width:1280px;margin:0 auto;padding:0 32px 72px}}.masthead{{position:relative;display:grid;grid-template-columns:minmax(0,1fr) auto;min-height:260px;border-left:18px solid var(--accent);border-bottom:1px solid var(--ink);padding:32px 36px 22px}}.brand{{align-self:end;position:relative;z-index:1}}h1{{max-width:700px;margin:0;font-size:clamp(2.8rem,7vw,6.8rem);font-weight:700;line-height:.86;letter-spacing:-.075em}}.host{{margin:18px 0 0;font-size:.875rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase}}.folio{{align-self:start;margin-top:-19px;font-size:clamp(8rem,20vw,17rem);font-weight:700;line-height:.8;letter-spacing:-.11em;color:var(--field);user-select:none}}
.notice{{margin:0;border-bottom:1px solid var(--ink);padding:18px 36px;background:var(--accent);color:#fff;font-weight:700}}.notice ul{{margin:10px 0 0;padding-left:20px}}code{{font-family:"Helvetica Neue",Helvetica,Arial,sans-serif;font-variant-numeric:tabular-nums}}
.section-head{{display:grid;grid-template-columns:70px 1fr;align-items:baseline;border-bottom:1px solid var(--ink);padding:22px 0 14px}}.section-no{{color:var(--accent);font-size:1.1rem;font-weight:700;font-variant-numeric:tabular-nums}}h2{{margin:0;font-size:1.3rem;line-height:1.1;letter-spacing:-.025em}}
.status-block{{border-bottom:1px solid var(--ink)}}.status-grid{{display:grid;grid-template-columns:repeat(5,1fr)}}.status-item{{min-width:0;min-height:118px;padding:18px 16px;border-right:1px solid var(--line)}}.status-item:last-child{{border-right:0}}.status-label{{display:block;min-height:38px;color:var(--muted);font-size:.75rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase}}.status-value{{display:block;margin-top:18px;font-size:1.05rem;overflow-wrap:anywhere}}.ok{{color:var(--ink)}}.bad{{color:var(--accent)}}.data-readout{{display:grid;grid-template-columns:70px 1fr;padding:14px 0;border-top:1px solid var(--line);font-size:.85rem}}.data-readout span{{color:var(--muted);font-weight:700}}.data-readout code{{overflow-wrap:anywhere}}
.control-grid{{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid var(--ink)}}.panel{{min-width:0;padding:0 22px 28px;border-right:1px solid var(--ink)}}.panel:first-child{{padding-left:0}}.panel:last-child{{padding-right:0;border-right:0}}.panel .section-head{{grid-template-columns:54px 1fr}}.hint{{min-height:44px;margin:18px 0;color:var(--muted);font-size:.88rem}}
label{{display:block;margin:17px 0 7px;font-size:.75rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase}}input{{width:100%;height:46px;border:1px solid var(--ink);border-radius:0;background:var(--paper);color:var(--ink);padding:10px 12px;outline:none}}input:focus{{border-color:var(--accent);box-shadow:inset 0 -3px 0 var(--accent)}}input::placeholder{{color:#8a8a8a}}.actions{{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}}button{{min-height:42px;border:1px solid var(--ink);border-radius:0;background:var(--ink);color:#fff;padding:9px 14px;font-weight:700;cursor:pointer}}button:hover,button:focus-visible{{background:var(--accent);border-color:var(--accent)}}button.secondary{{background:var(--paper);color:var(--ink)}}button.secondary:hover,button.secondary:focus-visible{{background:var(--ink);color:#fff}}button.danger{{background:var(--accent);border-color:var(--accent)}}button.danger:hover,button.danger:focus-visible{{background:var(--ink);border-color:var(--ink)}}
.inbox{{padding-top:8px}}.table-wrap{{overflow-x:auto}}table{{width:100%;min-width:680px;border-collapse:collapse;table-layout:fixed}}th,td{{padding:16px 14px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{color:var(--muted);font-size:.72rem;letter-spacing:.06em;text-transform:uppercase}}th:first-child,td:first-child{{width:190px;padding-left:0;font-variant-numeric:tabular-nums}}th:nth-child(2),td:nth-child(2){{width:180px}}th:last-child,td:last-child{{padding-right:0;overflow-wrap:anywhere}}.muted{{color:var(--muted)}}form{{margin:0}}
@media(max-width:850px){{main{{padding:0 20px 48px}}.masthead{{min-height:210px;padding:26px 24px 20px;border-left-width:12px}}.folio{{font-size:8rem}}.status-grid{{grid-template-columns:repeat(2,1fr)}}.status-item{{border-bottom:1px solid var(--line)}}.status-item:nth-child(even){{border-right:0}}.status-item:last-child{{border-right:1px solid var(--line)}}.control-grid{{grid-template-columns:1fr}}.panel,.panel:first-child,.panel:last-child{{padding:0 0 26px;border-right:0;border-bottom:1px solid var(--ink)}}.panel:last-child{{border-bottom:0}}}}
@media(max-width:520px){{main{{padding:0 14px 40px}}.masthead{{display:block;min-height:230px;padding:24px 18px 18px;overflow:hidden}}.brand{{position:absolute;left:18px;right:14px;bottom:20px}}.folio{{position:absolute;right:10px;top:15px;margin:0;font-size:8.4rem}}h1{{font-size:3rem;max-width:300px}}.host{{font-size:.7rem}}.status-grid{{grid-template-columns:1fr}}.status-item,.status-item:nth-child(even),.status-item:last-child{{min-height:92px;border-right:0}}.status-label{{min-height:auto}}.status-value{{margin-top:10px}}.section-head{{grid-template-columns:48px 1fr}}.data-readout{{grid-template-columns:1fr;gap:6px}}}}
</style></head><body><main><header class="masthead"><div class="brand"><h1>DJI Phone Gateway</h1><p class="host">Private cellular gateway · smspi</p></div><div class="folio" aria-hidden="true">4G</div></header>{banner}{chats}
<section class="status-block"><div class="section-head"><span class="section-no">00</span><h2>Gateway status</h2></div><div class="status-grid">
<div class="status-item"><span class="status-label">Modem AT interface</span><strong class="status-value {'ok' if modem_present else 'bad'}">{'Connected' if modem_present else 'Missing'}</strong></div>
<div class="status-item"><span class="status-label">Serial audio interface</span><strong class="status-value {'ok' if audio_present else 'bad'}">{'Connected' if audio_present else 'Missing'}</strong></div>
<div class="status-item"><span class="status-label">Asterisk</span><strong class="status-value">{html.escape(service_state('asterisk'))}</strong></div>
<div class="status-item"><span class="status-label">Telegram bridge</span><strong class="status-value">{html.escape(service_state('dji-sms-bridge'))}</strong></div>
<div class="status-item"><span class="status-label">Telegram configured</span><strong class="status-value">{'Yes' if token_set and chat_id else 'No'}</strong></div></div>
<div class="data-readout"><span>Data</span><code>{html.escape(data_status)}</code></div></section>
<div class="control-grid"><section class="panel"><div class="section-head"><span class="section-no">01</span><h2>Cellular data</h2></div><p class="hint">Mobile data stays off unless you enable it here.</p>
<form method="post" action="/data"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><div class="actions"><button name="state" value="off" class="secondary">Data off</button><button name="state" value="on" class="danger">Data on</button></div></form></section>
<section class="panel"><div class="section-head"><span class="section-no">02</span><h2>Telegram setup</h2></div><p class="hint">Paste the token from @BotFather. Your saved token is never displayed.</p>
<form method="post" action="/telegram"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><label>Bot token</label><input type="password" name="token" placeholder="{'Saved — leave blank to keep it' if token_set else '123456:ABC…'}" autocomplete="off"><label>Authorized chat ID</label><input name="chat_id" value="{html.escape(str(chat_id))}" inputmode="numeric"><div class="actions"><button name="action" value="save">Save settings</button><button name="action" value="test" class="secondary">Send test</button><button name="action" value="discover" class="secondary">Find chat IDs</button></div></form></section>
<section class="panel"><div class="section-head"><span class="section-no">03</span><h2>Send SMS</h2></div><p class="hint">Send a text through the connected cellular number.</p><form method="post" action="/sms"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><label>Phone number</label><input name="number" placeholder="+15551234567"><label>Message</label><input name="message" maxlength="670"><div class="actions"><button>Send SMS</button></div></form></section></div>
<section class="inbox"><div class="section-head"><span class="section-no">04</span><h2>Received SMS</h2></div><div class="table-wrap"><table><thead><tr><th>Received</th><th>From</th><th>Message</th></tr></thead><tbody id="sms-inbox">{rows}</tbody></table></div></section>
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
                elif form.get("action") == "test":
                    chat_id = int(form.get("chat_id", "").strip() or settings.get("telegram_chat_id", 0))
                    if not chat_id:
                        raise ValueError("Enter the authorized chat ID before sending a test.")
                    tg.send(chat_id, "Test message: Telegram notifications are working.")
                    self.respond(page(f"Test notification sent through @{identity.get('username', 'bot')}."))
                else:
                    chat_id = int(form.get("chat_id", "").strip())
                    save_settings(token, chat_id)
                    run(["sudo", "-n", "/usr/bin/systemctl", "restart", "dji-sms-bridge.service"], 15)
                    self.respond(page(f"Telegram settings saved for @{identity.get('username', 'bot')}."))
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
