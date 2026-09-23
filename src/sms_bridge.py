#!/usr/bin/env python3
"""Authenticated Telegram bridge for chan_quectel and the modem data switch."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

LOG = logging.getLogger("dji-sms-bridge")
BOT_TOKEN_RE = re.compile(r"^[0-9]{5,12}:[A-Za-z0-9_-]{20,100}$")


def valid_bot_token(token: str) -> bool:
    """Accept the numeric-id:secret format issued by Telegram's BotFather."""
    return bool(BOT_TOKEN_RE.fullmatch(token.strip()))


@dataclass(frozen=True)
class Config:
    token: str
    chat_id: int
    ami_user: str
    ami_password: str
    ami_host: str = "127.0.0.1"
    ami_port: int = 5038
    modem: str = "quectel0"
    data_command: str = "/opt/dji-phone-gateway/bin/dji-data"
    poll_timeout: int = 25
    sms_database: str = "/var/lib/dji-phone-gateway/messages.sqlite3"

    @classmethod
    def from_env(cls) -> "Config":
        settings = {}
        settings_path = os.getenv("GATEWAY_SETTINGS", "/var/lib/dji-phone-gateway/settings.json")
        try:
            with open(settings_path, encoding="utf-8") as stream:
                settings = json.load(stream)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        token = str(settings.get("telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        chat_id = str(settings.get("telegram_chat_id") or os.environ.get("TELEGRAM_CHAT_ID", ""))
        required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ASTERISK_AMI_USER", "ASTERISK_AMI_PASSWORD"]
        values = {**os.environ, "TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id}
        missing = [key for key in required if not values.get(key)]
        if missing:
            raise RuntimeError("missing configuration: " + ", ".join(missing))
        return cls(
            token=token,
            chat_id=int(chat_id),
            ami_user=os.environ["ASTERISK_AMI_USER"],
            ami_password=os.environ["ASTERISK_AMI_PASSWORD"],
            ami_host=os.getenv("ASTERISK_HOST", "127.0.0.1"),
            ami_port=int(os.getenv("ASTERISK_PORT", "5038")),
            modem=os.getenv("MODEM_NAME", "quectel0"),
            data_command=os.getenv("DATA_COMMAND", "/opt/dji-phone-gateway/bin/dji-data"),
            poll_timeout=max(5, min(50, int(os.getenv("POLL_TIMEOUT", "25")))),
            sms_database=os.getenv("SMS_DATABASE", "/var/lib/dji-phone-gateway/messages.sqlite3"),
        )


class Telegram:
    def __init__(self, token: str):
        if not valid_bot_token(token):
            raise ValueError("Invalid Telegram bot token. Paste only the token from @BotFather.")
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, values: dict, timeout: int = 35) -> dict:
        req = urllib.request.Request(
            self.base + method,
            data=urllib.parse.urlencode(values).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = json.load(response)
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body}")
        return body

    def send(self, chat_id: int, text: str) -> None:
        self.call("sendMessage", {"chat_id": chat_id, "text": text[:4096]})


def ami_command(cfg: Config, command: str) -> str:
    """Run an Asterisk CLI command through localhost AMI."""
    def packet(fields: dict[str, str]) -> bytes:
        return ("\r\n".join(f"{k}: {v}" for k, v in fields.items()) + "\r\n\r\n").encode()

    chunks = []
    with socket.create_connection((cfg.ami_host, cfg.ami_port), timeout=10) as sock:
        sock.settimeout(10)
        sock.recv(1024)
        sock.sendall(packet({"Action": "Login", "Username": cfg.ami_user, "Secret": cfg.ami_password, "Events": "off"}))
        login = sock.recv(4096).decode(errors="replace")
        if "Response: Success" not in login:
            raise RuntimeError("Asterisk AMI login failed")
        sock.sendall(packet({"Action": "Command", "Command": command}))
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data.decode(errors="replace"))
            response = "".join(chunks)
            if "--END COMMAND--" in response or response.endswith("\r\n\r\n"):
                break
        sock.sendall(packet({"Action": "Logoff"}))
    return "".join(chunks)


def parse_command(text: str) -> tuple[str, list[str]]:
    try:
        parts = shlex.split(text.strip())
    except ValueError as exc:
        return "error", [str(exc)]
    if not parts:
        return "", []
    return parts[0].split("@", 1)[0].lower(), parts[1:]


def safe_phone(number: str) -> bool:
    normalized = number.replace(" ", "").replace("-", "")
    return normalized.lstrip("+").isdigit() and 3 <= len(normalized.lstrip("+")) <= 15


def safe_sms(message: str) -> str:
    """Asterisk CLI is line-oriented; flatten control characters before AMI."""
    return " ".join(message.splitlines()).replace("\x00", "").strip()


def run_data(cfg: Config, operation: str) -> str:
    result = subprocess.run(["sudo", "-n", cfg.data_command, operation], text=True, capture_output=True, timeout=30, check=False)
    output = (result.stdout + result.stderr).strip()
    if result.returncode:
        raise RuntimeError(output or f"data command exited {result.returncode}")
    return output


def resolve_reply_target(cfg: Config, chat_id: int, reply_message_id: int) -> str | None:
    """Resolve the sender linked to an exact quoted Telegram SMS notification."""
    try:
        with sqlite3.connect(f"file:{cfg.sms_database}?mode=ro", uri=True, timeout=2) as db:
            row = db.execute(
                "SELECT sender FROM telegram_reply_targets WHERE chat_id = ? AND message_id = ?",
                (chat_id, reply_message_id),
            ).fetchone()
    except sqlite3.Error as exc:
        LOG.warning("could not resolve Telegram SMS reply target: %s", exc)
        return None
    return str(row[0]) if row else None


def queue_sms(cfg: Config, number: str, message: str) -> tuple[bool, str]:
    number = number.strip().replace(" ", "").replace("-", "")
    output = ami_command(cfg, f"quectel sms send {cfg.modem} {number} {message}")
    queued = "SMS queued for send" in output
    if queued:
        try:
            with sqlite3.connect(cfg.sms_database, timeout=3) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS messages ("
                    "id INTEGER PRIMARY KEY, received_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                    "sender TEXT NOT NULL, body TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'incoming')"
                )
                columns = {str(row[1]) for row in db.execute("PRAGMA table_info(messages)")}
                if "direction" not in columns:
                    db.execute("ALTER TABLE messages ADD COLUMN direction TEXT NOT NULL DEFAULT 'incoming'")
                db.execute(
                    "INSERT INTO messages(sender, body, direction) VALUES (?, ?, 'outgoing')",
                    (number, message),
                )
        except sqlite3.Error as exc:
            LOG.warning("SMS queued but could not be added to conversation history: %s", exc)
    return queued, output


def handle(cfg: Config, tg: Telegram, chat_id: int, text: str, reply_message_id: int | None = None) -> None:
    if chat_id != cfg.chat_id:
        LOG.warning("ignored update from unauthorized chat %s", chat_id)
        return
    stripped = text.strip()
    command, args = parse_command(stripped) if stripped.startswith("/") else ("", [])
    if command in ("/start", "/help"):
        tg.send(chat_id, "Reply to a forwarded text to answer it. Unquoted messages are ignored.\n\nCommands:\n/sms <number> <message>\n/status\n/data_on\n/data_off")
    elif command == "/sms":
        if len(args) < 2 or not safe_phone(args[0]):
            tg.send(chat_id, "Usage: /sms +15551234567 message")
            return
        message = safe_sms(" ".join(args[1:]))
        if len(message) > 670:
            tg.send(chat_id, "SMS is too long (maximum 670 characters).")
            return
        queued, output = queue_sms(cfg, args[0], message)
        tg.send(chat_id, f"SMS queued to {args[0]}." if queued else "SMS failed:\n" + output[-1500:])
    elif command == "/status":
        modem = ami_command(cfg, f"quectel show device status {cfg.modem}")
        data = run_data(cfg, "status")
        tg.send(chat_id, (modem[-3200:] + "\n\nData: " + data)[-4096:])
    elif command in ("/data_on", "/data_off"):
        state = "on" if command.endswith("on") else "off"
        tg.send(chat_id, run_data(cfg, state))
    elif command:
        tg.send(chat_id, "Unknown command. Use /help.")
    elif stripped:
        if reply_message_id is None:
            return
        target = resolve_reply_target(cfg, chat_id, reply_message_id)
        if not target:
            tg.send(chat_id, "That forwarded message is too old or is not linked to an SMS.")
            return
        if not safe_phone(target):
            tg.send(chat_id, "The sender number cannot receive an SMS reply.")
            return
        message = safe_sms(stripped)
        if len(message) > 670:
            tg.send(chat_id, "SMS is too long (maximum 670 characters).")
            return
        queued, output = queue_sms(cfg, target, message)
        tg.send(chat_id, f"SMS queued to {target}." if queued else "SMS failed:\n" + output[-1500:])


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    tg = Telegram(cfg.token)
    offset = 0
    delay = 1
    while True:
        try:
            result = tg.call("getUpdates", {"offset": offset, "timeout": cfg.poll_timeout, "allowed_updates": json.dumps(["message"])}, cfg.poll_timeout + 10)
            for update in result["result"]:
                offset = max(offset, int(update["update_id"]) + 1)
                msg = update.get("message", {})
                if "text" in msg and "chat" in msg:
                    reply = msg.get("reply_to_message", {})
                    reply_message_id = int(reply["message_id"]) if "message_id" in reply else None
                    handle(cfg, tg, int(msg["chat"]["id"]), msg["text"], reply_message_id)
            delay = 1
        except (OSError, RuntimeError, urllib.error.URLError, json.JSONDecodeError) as exc:
            LOG.error("bridge loop: %s", exc)
            time.sleep(delay)
            delay = min(delay * 2, 60)


if __name__ == "__main__":
    main()
