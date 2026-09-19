#!/usr/bin/env python3
"""Authenticated Telegram bridge for chan_quectel and the modem data switch."""

from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

LOG = logging.getLogger("dji-sms-bridge")


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

    @classmethod
    def from_env(cls) -> "Config":
        required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ASTERISK_AMI_USER", "ASTERISK_AMI_PASSWORD"]
        missing = [key for key in required if not os.environ.get(key)]
        if missing:
            raise RuntimeError("missing configuration: " + ", ".join(missing))
        return cls(
            token=os.environ["TELEGRAM_BOT_TOKEN"],
            chat_id=int(os.environ["TELEGRAM_CHAT_ID"]),
            ami_user=os.environ["ASTERISK_AMI_USER"],
            ami_password=os.environ["ASTERISK_AMI_PASSWORD"],
            ami_host=os.getenv("ASTERISK_HOST", "127.0.0.1"),
            ami_port=int(os.getenv("ASTERISK_PORT", "5038")),
            modem=os.getenv("MODEM_NAME", "quectel0"),
            data_command=os.getenv("DATA_COMMAND", "/opt/dji-phone-gateway/bin/dji-data"),
            poll_timeout=max(5, min(50, int(os.getenv("POLL_TIMEOUT", "25")))),
        )


class Telegram:
    def __init__(self, token: str):
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
            if "--END COMMAND--" in chunks[-1]:
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


def handle(cfg: Config, tg: Telegram, chat_id: int, text: str) -> None:
    if chat_id != cfg.chat_id:
        LOG.warning("ignored update from unauthorized chat %s", chat_id)
        return
    command, args = parse_command(text)
    if command in ("/start", "/help"):
        tg.send(chat_id, "Commands:\n/sms <number> <message>\n/status\n/data_on\n/data_off")
    elif command == "/sms":
        if len(args) < 2 or not safe_phone(args[0]):
            tg.send(chat_id, "Usage: /sms +15551234567 message")
            return
        message = safe_sms(" ".join(args[1:]))
        if len(message) > 670:
            tg.send(chat_id, "SMS is too long (maximum 670 characters).")
            return
        output = ami_command(cfg, f"quectel sms send {cfg.modem} {args[0]} {message}")
        tg.send(chat_id, "SMS queued." if "error" not in output.lower() else "SMS failed:\n" + output[-1500:])
    elif command == "/status":
        modem = ami_command(cfg, f"quectel show device status {cfg.modem}")
        data = run_data(cfg, "status")
        tg.send(chat_id, (modem[-3200:] + "\n\nData: " + data)[-4096:])
    elif command in ("/data_on", "/data_off"):
        state = "on" if command.endswith("on") else "off"
        tg.send(chat_id, run_data(cfg, state))
    elif command:
        tg.send(chat_id, "Unknown command. Use /help.")


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
                    handle(cfg, tg, int(msg["chat"]["id"]), msg["text"])
            delay = 1
        except (OSError, RuntimeError, urllib.error.URLError, json.JSONDecodeError) as exc:
            LOG.error("bridge loop: %s", exc)
            time.sleep(delay)
            delay = min(delay * 2, 60)


if __name__ == "__main__":
    main()
