"""Minimal Telegram Bot API client: send alerts, read commands."""
import logging

import requests

log = logging.getLogger("telegram")


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.offset = 0
        self.enabled = bool(token and chat_id)

    def send(self, html: str) -> bool:
        if not self.enabled:
            print("[telegram disabled]\n" + html + "\n")
            return False
        try:
            r = requests.post(self.base + "/sendMessage", timeout=10, json={
                "chat_id": self.chat_id, "text": html[:4000], "parse_mode": "HTML",
                "disable_web_page_preview": True})
            if not r.ok:
                log.warning("send failed: %s", r.text[:200])
            return r.ok
        except requests.RequestException as e:
            log.warning("send error: %s", e)
            return False

    def send_long(self, lines: list) -> None:
        """Send a long message as several Telegram messages (limit ~4,000 characters each)."""
        chunk = ""
        for ln in lines:
            if len(chunk) + len(ln) + 1 > 3800:
                self.send(chunk)
                chunk = ""
            chunk += ln + "\n"
        if chunk.strip():
            self.send(chunk)

    def commands(self) -> list:
        """Return [(text)] of new messages from YOUR chat only (others are ignored)."""
        if not self.enabled:
            return []
        try:
            r = requests.get(self.base + "/getUpdates", timeout=10,
                             params={"offset": self.offset, "timeout": 0})
            out = []
            for u in r.json().get("result", []):
                self.offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id")) == self.chat_id and msg.get("text"):
                    out.append(msg["text"].strip())
            return out
        except (requests.RequestException, ValueError) as e:
            log.warning("getUpdates error: %s", e)
            return []
