"""AI reasoning for headlines whose impact is indirect.

A headline rarely says "stock X will rise". "Government raises import duty on
steel" means: imported steel gets costlier -> Indian steel makers can charge
more (up) -> companies that buy steel pay more (down). Keyword rules can't do
that chain of reasoning; a language model can.

Works with a free key from either provider (chosen by the key's prefix):
  * Google Gemini  (key starts with "AIza")  - aistudio.google.com/apikey
  * Groq           (key starts with "gsk_")  - console.groq.com/keys
"""
import json
import logging
import re
import time

import requests

log = logging.getLogger("ai")

PROMPT = """You are an Indian equity analyst. Read this news headline and work out which
NSE-listed companies' share prices it is likely to move, including SECOND-ORDER effects
(suppliers, customers, competitors, import/export exposure, input costs).

Headline: "{headline}"
Source: {source}
Stocks the user owns (pay special attention): {portfolio}

Rules:
- Only real NSE-listed Indian companies, with their exact NSE trading symbol (e.g. TATASTEEL, LICI, HAL).
- At most 5 companies, most affected first. Empty list if the headline is not price-relevant
  (opinion pieces, generic market commentary, stock tips, already-reported price moves).
- direction: "up" or "down". magnitude: "small" (<2%), "medium" (2-5%), "large" (>5%) expected move.
- confidence: 0.0-1.0, how sure you are about the direction. Be conservative.
- why: one short sentence with the cause -> effect chain.
- priced_in: true if the headline reports a move that already happened ("shares jump 8%").

Reply with JSON only:
{{"relevant": true/false, "priced_in": true/false,
  "impacts": [{{"symbol": "...", "direction": "up|down", "magnitude": "small|medium|large",
               "confidence": 0.0, "order": "direct|second-order", "why": "..."}}]}}"""


class AIReader:
    def __init__(self, key: str = "", max_per_day: int = 400, min_gap_s: float = 4.5):
        self.key = key or ""
        self.max_per_day = max_per_day
        self.min_gap_s = min_gap_s     # free tiers allow roughly 15 requests/minute
        self.last_call = 0.0
        self.day = ""
        self.calls_today = 0
        self.errors = 0

    @property
    def provider(self) -> str:
        if self.key.startswith("AIza"):
            return "gemini"
        if self.key.startswith("gsk_"):
            return "groq"
        return ""

    def budget_left(self) -> bool:
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.calls_today = today, 0
        return bool(self.provider) and self.calls_today < self.max_per_day

    def read(self, headline: str, source: str, portfolio: list) -> dict:
        """Returns {"relevant", "priced_in", "impacts": [...]} or None if unavailable/failed."""
        if not self.budget_left():
            return None
        wait = self.min_gap_s - (time.time() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        self.last_call = time.time()
        self.calls_today += 1
        prompt = PROMPT.format(headline=headline.replace('"', "'"), source=source,
                               portfolio=", ".join(sorted(portfolio)) or "none")
        try:
            text = self._gemini(prompt) if self.provider == "gemini" else self._groq(prompt)
            data = parse_json(text)
            self.errors = 0
            return data
        except Exception as e:
            self.errors += 1
            log.warning("ai read failed: %s", e)
            return None

    def _gemini(self, prompt: str) -> str:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{self.model or 'gemini-2.5-flash-lite'}:generateContent")
        r = requests.post(url, params={"key": self.key}, timeout=20, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}})
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    def _groq(self, prompt: str) -> str:
        r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=20,
                          headers={"Authorization": f"Bearer {self.key}"}, json={
                              "model": self.model or "llama-3.3-70b-versatile", "temperature": 0.2,
                              "response_format": {"type": "json_object"},
                              "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    model = ""


def parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0) if m else text)
    out = {"relevant": bool(data.get("relevant")), "priced_in": bool(data.get("priced_in")), "impacts": []}
    for i in data.get("impacts") or []:
        try:
            sym = re.sub(r"[^A-Z0-9&-]", "", str(i.get("symbol", "")).upper())
            d = str(i.get("direction", "")).lower()
            if not sym or d not in ("up", "down"):
                continue
            out["impacts"].append({
                "symbol": sym, "direction": d,
                "magnitude": str(i.get("magnitude", "small")).lower(),
                "confidence": max(0.0, min(1.0, float(i.get("confidence", 0)))),
                "order": str(i.get("order", "direct")).lower(),
                "why": str(i.get("why", ""))[:200]})
        except (TypeError, ValueError):
            continue
    out["impacts"] = out["impacts"][:5]
    return out
