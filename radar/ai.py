"""AI reasoning for headlines whose impact is indirect.

A headline rarely says "stock X will rise". "Government raises import duty on
steel" means: imported steel gets costlier -> Indian steel makers can charge
more (up) -> companies that buy steel pay more (down). Keyword rules can't do
that chain of reasoning; a language model can.

Works with a free key from either provider (chosen by the key's prefix):
  * Google Gemini  (key starts with "AIza" or "AQ.")  - aistudio.google.com/apikey
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
(suppliers, customers, competitors, regulation, taxes, import/export exposure, input costs).
Example: "FM hints at review of STT and LTCG tax" -> lower trading costs and more trading
-> exchanges and brokers (MCX, BSE, ANGELONE, CDSL) up, even though none is named.

Headline: "{headline}"
Source: {source}
Stocks the user owns (pay special attention): {portfolio}

Rules:
- Only real NSE-listed Indian companies, with their exact NSE trading symbol (e.g. TATASTEEL, LICI, HAL).
- At most 6 companies, most affected first. Empty list if the headline is not price-relevant
  (opinion pieces, generic market commentary, stock tips).
- direction: "up" or "down". magnitude: "small" (<2%), "medium" (2-5%), "large" (>5%) expected move.
- confidence: 0.0-1.0, how sure you are about the direction. Be conservative.
- why: one short sentence with the cause -> effect chain, in plain words.
- moved: true only if the headline itself reports that THIS company's share price already moved
  (a headline saying "Sensex gains on X" does not mean the second-order stocks have moved).
- summary: one or two plain sentences saying what actually happened (no jargon, no headline copy).
- importance: how much this matters to Indian investors overall: "low", "medium", or "high"
  (high = budget/tax/RBI/SEBI/government decisions, wars, big global shocks, sector-wide changes).
- topic: 2-5 word label for the underlying event, the same for every outlet's version of it
  (e.g. "STT LTCG review").

Reply with JSON only:
{{"relevant": true/false, "priced_in": true/false, "summary": "...", "importance": "low|medium|high",
  "topic": "...",
  "impacts": [{{"symbol": "...", "direction": "up|down", "magnitude": "small|medium|large",
               "confidence": 0.0, "order": "direct|second-order", "moved": false, "why": "..."}}]}}
priced_in = true only if every company listed has already moved on this news."""


SUMMARY_PROMPT = """You are an Indian equity analyst. Today is {today}. Below are the news items about
{company} (NSE: {symbol}) from the last 3 days, newest first, each with its publication date.
Weigh ALL of them together: positives against negatives, how material each one is for THIS
company's earnings and share price, and how recent it is.
Rules:
- Use ONLY the items below. Do not add facts from memory (no past quarters, past results or
  past events that are not in the list).
- Ignore any item about an older period (e.g. last year's results) and items that only report
  a price move that already happened.
- If nothing below is price-relevant, answer direction "none".

{items}

Reply with JSON only:
{{"direction": "up|down|mixed|none", "confidence": 0.0,
  "positives": ["short phrase", ...], "negatives": ["short phrase", ...],
  "summary": "one or two plain sentences on the net picture"}}"""


ANALYSIS_PROMPT = """You are an Indian equity analyst. Today is {today}. Analyse {company} (NSE: {symbol})
using ONLY the facts below. Do not add facts from memory (no past results, past events, targets
or numbers that are not listed). The news list has two kinds of items:
  [C] news about the company itself
  [S] sector, policy, tax, regulatory and macro news that may affect it INDIRECTLY
For [S] items, think through the cause -> effect chain for THIS company (e.g. a cut in
securities transaction tax -> more trading -> more fee income for an exchange).

FACTS
{facts}

Reply with JSON only:
{{"direction": "up|down|mixed|none", "confidence": 0.0,
  "summary": "2-3 plain sentences: what is going on with this stock right now and why",
  "positives": ["short phrase", ...], "negatives": ["short phrase", ...],
  "vs_peers": "one sentence on how it is doing compared with its sector peers",
  "news": [{{"ref": 1, "what": "what happened, in 1-2 plain sentences of your own (not the headline)",
            "effect": "what it means for {symbol} and why, one sentence",
            "direction": "up|down|neutral", "importance": 1}}]}}
direction = which way the current news and data point for the next few sessions; "none" if nothing points either way.
news: the items that actually matter for {symbol}, most important first, at most 8. Merge items that report
the same event into one (use the ref of the clearest one). Leave out items irrelevant to {symbol}, duplicates,
and items that only report a price move. importance: 3 = could move the stock a lot, 2 = noticeable, 1 = minor."""

DRIVERS_PROMPT = """You are an Indian equity analyst. List what kinds of NEWS move the share price of
{company} (NSE: {symbol}){industry}, beyond news that names the company: regulation, taxes, government
policy, commodity or currency prices, sector events, key customers or competitors.
Give 6 short Google News search queries (2-4 words each) that would find such news in India today.
Examples for an exchange: "securities transaction tax", "SEBI F&O rules", "commodity derivatives volume".
Reply with JSON only: {{"queries": ["...", ...]}}"""

MOVE_PROMPT = """You are an Indian equity analyst. Today is {today}. The user asks why {company} (NSE: {symbol})
{asked}. Explain using ONLY the facts below; do not invent news or numbers. Separate causes specific
to the company from sector-wide and market-wide moves (compare the stock with its peers and the Nifty).
If the stock did not actually move the way the user says, say so plainly. If the facts don't explain
the move, say that no clear news explains it and list what the data does show.

FACTS
{facts}

Reply with JSON only:
{{"actual_move": "up|down|flat", "matches_user": true,
  "headline": "one sentence answer",
  "reasons": [{{"reason": "short", "type": "company|sector|market|unclear", "evidence": "which fact shows it"}}],
  "confidence": 0.0}}"""


class AIReader:
    def __init__(self, key: str = "", max_per_day: int = 400, min_gap_s: float = 4.5):
        self.key = key or ""
        self.max_per_day = max_per_day
        self.min_gap_s = min_gap_s     # free tiers allow roughly 15 requests/minute
        self.last_call = 0.0
        self.day = ""
        self.calls_today = 0
        self.errors = 0
        self.last_error = ""

    @property
    def provider(self) -> str:
        if self.key.startswith(("AIza", "AQ.")):  # AI Studio now issues "AQ." auth keys
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
            self.last_error = str(e)[:300]
            log.warning("ai read failed: %s", e)
            return None

    def summarize(self, symbol: str, company: str, items: list) -> dict:
        """items: [(tone, headline, source)] newest first. One overall reading, or None."""
        if not self.budget_left() or not items:
            return None
        wait = self.min_gap_s - (time.time() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        self.last_call = time.time()
        self.calls_today += 1
        lines = "\n".join(f"- {h} ({src})" for _t, h, src in items[:15])
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y")
        prompt = SUMMARY_PROMPT.format(company=company or symbol, symbol=symbol, items=lines, today=today)
        try:
            text = self._gemini(prompt) if self.provider == "gemini" else self._groq(prompt)
            m = re.search(r"\{.*\}", text, re.S)
            d = json.loads(m.group(0) if m else text)
            self.errors = 0
            return {"direction": str(d.get("direction", "none")).lower(),
                    "confidence": max(0.0, min(1.0, float(d.get("confidence", 0) or 0))),
                    "positives": [str(x)[:120] for x in (d.get("positives") or [])][:4],
                    "negatives": [str(x)[:120] for x in (d.get("negatives") or [])][:4],
                    "summary": str(d.get("summary", ""))[:400]}
        except Exception as e:
            self.errors += 1
            self.last_error = str(e)[:300]
            log.warning("ai summary failed: %s", e)
            return None

    def _json(self, prompt: str):
        if not self.budget_left():
            return None
        wait = self.min_gap_s - (time.time() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        self.last_call = time.time()
        self.calls_today += 1
        try:
            text = self._gemini(prompt) if self.provider == "gemini" else self._groq(prompt)
            m = re.search(r"\{.*\}", text, re.S)
            d = json.loads(m.group(0) if m else text)
            self.errors = 0
            return d
        except Exception as e:
            self.errors += 1
            self.last_error = str(e)[:300]
            log.warning("ai call failed: %s", e)
            return None

    @staticmethod
    def _today() -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %H:%M IST")

    def analyse(self, symbol: str, company: str, facts: str) -> dict:
        d = self._json(ANALYSIS_PROMPT.format(today=self._today(), company=company or symbol,
                                              symbol=symbol, facts=facts))
        if not d:
            return None
        try:
            conf = max(0.0, min(1.0, float(d.get("confidence", 0) or 0)))
        except (TypeError, ValueError):
            conf = 0.0
        return {"direction": str(d.get("direction", "none")).lower(), "confidence": conf,
                "summary": str(d.get("summary", ""))[:500],
                "positives": [str(x)[:140] for x in (d.get("positives") or [])][:4],
                "negatives": [str(x)[:140] for x in (d.get("negatives") or [])][:4],
                "vs_peers": str(d.get("vs_peers", ""))[:250], "news": _news_items(d.get("news"))}

    def drivers(self, symbol: str, company: str, industry: str = "") -> list:
        d = self._json(DRIVERS_PROMPT.format(symbol=symbol, company=company or symbol,
                                             industry=f", industry: {industry}" if industry else ""))
        qs = [re.sub(r'["\n]', "", str(q)).strip()[:60] for q in ((d or {}).get("queries") or [])]
        return [q for q in qs if len(q) >= 3][:6]

    def explain_move(self, symbol: str, company: str, asked: str, facts: str) -> dict:
        d = self._json(MOVE_PROMPT.format(today=self._today(), company=company or symbol, symbol=symbol,
                                          asked=asked, facts=facts))
        if not d:
            return None
        reasons = []
        for r in (d.get("reasons") or [])[:5]:
            if isinstance(r, dict) and r.get("reason"):
                reasons.append({"reason": str(r["reason"])[:160], "type": str(r.get("type", "unclear")).lower(),
                                "evidence": str(r.get("evidence", ""))[:160]})
        try:
            conf = max(0.0, min(1.0, float(d.get("confidence", 0) or 0)))
        except (TypeError, ValueError):
            conf = 0.0
        return {"actual_move": str(d.get("actual_move", "")).lower(), "matches_user": bool(d.get("matches_user", True)),
                "headline": str(d.get("headline", ""))[:300], "reasons": reasons, "confidence": conf}

    def _gemini(self, prompt: str) -> str:
        model = self.model or "gemini-3.5-flash-lite"
        for _ in range(2):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            # The key goes in a header: new "AQ." keys are rejected when sent as ?key=
            r = requests.post(url, headers={"x-goog-api-key": self.key}, timeout=20, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}})
            # Google retires models over time and names the replacement in the error; follow it once.
            m = re.search(r"use models/([\w.-]+)", r.text) if r.status_code == 404 else None
            if m and m.group(1) != model:
                model = self.model = m.group(1)
                continue
            break
        if r.status_code >= 400:
            raise RuntimeError(f"Gemini {r.status_code}: {r.text[:200]}")
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    def _groq(self, prompt: str) -> str:
        r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=20,
                          headers={"Authorization": f"Bearer {self.key}"}, json={
                              "model": self.model or "llama-3.3-70b-versatile", "temperature": 0.2,
                              "response_format": {"type": "json_object"},
                              "messages": [{"role": "user", "content": prompt}]})
        if r.status_code >= 400:
            raise RuntimeError(f"Groq {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"]

    model = ""


def _news_items(raw) -> list:
    out = []
    for n in raw or []:
        if not isinstance(n, dict) or not n.get("what"):
            continue
        try:
            ref = int(n.get("ref", 0))
        except (TypeError, ValueError):
            ref = 0
        try:
            imp = max(1, min(3, int(n.get("importance", 1))))
        except (TypeError, ValueError):
            imp = 1
        d = str(n.get("direction", "neutral")).lower()
        out.append({"ref": ref, "what": str(n["what"])[:300], "effect": str(n.get("effect", ""))[:250],
                    "direction": d if d in ("up", "down") else "neutral", "importance": imp})
    return out[:8]


def parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0) if m else text)
    imp = str(data.get("importance", "low")).lower()
    out = {"relevant": bool(data.get("relevant")), "priced_in": bool(data.get("priced_in")), "impacts": [],
           "summary": str(data.get("summary", ""))[:400], "importance": imp if imp in ("low", "medium", "high") else "low",
           "topic": re.sub(r"\s+", " ", str(data.get("topic", ""))).strip().lower()[:60]}
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
                "moved": bool(i.get("moved", False)),
                "why": str(i.get("why", ""))[:200]})
        except (TypeError, ValueError):
            continue
    out["impacts"] = out["impacts"][:6]
    return out
