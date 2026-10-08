"""Telegram Bot API client: alerts with tap-able buttons, the command menu, and
reading what you type or tap."""
import json
import logging

import requests

log = logging.getLogger("telegram")


def keyboard(rows):
    """rows = [[(label, callback_data), ...], ...] -> Telegram inline keyboard."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d[:64]} for t, d in row] for row in rows if row]}


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.offset = 0
        self.enabled = bool(token and chat_id)

    def _post(self, method: str, payload: dict):
        if not self.enabled:
            return None
        try:
            r = requests.post(f"{self.base}/{method}", timeout=10, json=payload)
            if not r.ok:
                log.warning("%s failed: %s", method, r.text[:200])
            return r
        except requests.RequestException as e:
            log.warning("%s error: %s", method, e)
            return None

    def send(self, html: str, buttons=None, reply_keyboard=None) -> bool:
        """buttons: inline buttons under the message, [[(label, data), ...], ...].
        reply_keyboard: the always-visible button panel above the typing box, [[label, ...], ...]."""
        if not self.enabled:
            print("[telegram disabled]\n" + html + "\n")
            return False
        payload = {"chat_id": self.chat_id, "text": html[:4000], "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = keyboard(buttons)
        elif reply_keyboard:
            payload["reply_markup"] = {"keyboard": [[{"text": t} for t in row] for row in reply_keyboard],
                                       "resize_keyboard": True, "is_persistent": True}
        r = self._post("sendMessage", payload)
        return bool(r is not None and r.ok)

    def edit(self, message_id: int, html: str, buttons=None) -> None:
        payload = {"chat_id": self.chat_id, "message_id": message_id, "text": html[:4000],
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = keyboard(buttons)
        self._post("editMessageText", payload)

    def answer(self, callback_id: str, text: str = "") -> None:
        """Stops the little loading spinner on a tapped button (optionally shows a toast)."""
        self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})

    def set_menu(self, commands) -> None:
        """The ☰ Menu button next to the typing box: a list of commands with descriptions."""
        self._post("setMyCommands", {"commands": [{"command": c, "description": d[:256]} for c, d in commands]})
        self._post("setChatMenuButton", {"chat_id": self.chat_id, "menu_button": {"type": "commands"}})

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
        """New things from YOUR chat only: {"type": "text", "text"} for typed messages and
        {"type": "tap", "data", "id", "msg_id"} for tapped buttons. Others are ignored."""
        if not self.enabled:
            return []
        try:
            r = requests.get(self.base + "/getUpdates", timeout=10,
                             params={"offset": self.offset, "timeout": 0,
                                     "allowed_updates": json.dumps(["message", "callback_query"])})
            out = []
            for u in r.json().get("result", []):
                self.offset = u["update_id"] + 1
                cb = u.get("callback_query")
                if cb and str(cb.get("message", {}).get("chat", {}).get("id")) == self.chat_id:
                    out.append({"type": "tap", "data": cb.get("data", ""), "id": cb["id"],
                                "msg_id": cb["message"]["message_id"]})
                    continue
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id")) == self.chat_id and msg.get("text"):
                    out.append({"type": "text", "text": msg["text"].strip()})
            return out
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("getUpdates error: %s", e)
            return []
