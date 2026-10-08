"""Read public Telegram channels without logging in.

Every public channel has a web preview at https://t.me/s/<username> that shows its
latest posts. We poll it like a news feed. Private channels and groups can't be read
this way (that would need logging in with a personal Telegram account).
"""
import html
import re

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


def clean_name(text: str) -> str:
    """'@ETMarkets', 't.me/ETMarkets', 'https://t.me/s/ETMarkets' -> 'ETMarkets'."""
    t = text.strip()
    t = re.sub(r"^https?://", "", t)
    t = re.sub(r"^(t\.me|telegram\.me)/(s/)?", "", t)
    t = t.lstrip("@").split("/")[0].split("?")[0]
    return t if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", t) else ""


def parse_channel(page: str) -> list:
    """Returns [{"id": "channel/123", "text": "..."}] oldest first."""
    posts = []
    for m in re.finditer(r'data-post="([^"]+)"(.*?)(?=data-post="|\Z)', page, re.S):
        pid, chunk = m.group(1), m.group(2)
        t = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
        if not t:
            continue
        text = re.sub(r"<br\s*/?>", " ", t.group(1))
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            posts.append({"id": pid, "text": text})
    return posts


class Channels:
    def __init__(self, timeout: float = 10):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.timeout = timeout

    def fetch(self, name: str) -> list:
        r = self.s.get(f"https://t.me/s/{name}", timeout=self.timeout)
        r.raise_for_status()
        if "tgme_channel_info" not in r.text and "tgme_widget_message" not in r.text:
            raise ValueError("not a public channel (or it has no web preview)")
        return parse_channel(r.text)
