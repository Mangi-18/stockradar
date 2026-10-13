"""Price history and sector data, with Yahoo Finance as a fallback when NSE is unreachable.

NSE's website sometimes blocks cloud servers. These public Yahoo endpoints need no key:
  * chart:   daily closes for a year -> today's move, 1-week / 1-month / 1-year returns, 52-week range
  * search:  sector and industry of a listed company
  * similar: companies Yahoo considers comparable (used only if NSE's industry data isn't available)
"""
import logging
import time

import requests

log = logging.getLogger("market")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
      "Accept": "application/json"}


class Yahoo:
    def __init__(self, timeout: float = 8):
        self.s = requests.Session()
        self.s.headers.update(UA)
        self.timeout = timeout
        self._cache = {}

    def _get(self, url, params=None, ttl=600):
        key = (url, tuple(sorted((params or {}).items())))
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        r = self.s.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        self._cache[key] = (time.time(), data)
        return data

    def history(self, symbol: str, yahoo_symbol: str = None) -> dict:
        """{'price','prev','closes':[...],'times':[...]} for an NSE symbol (or a Yahoo index like ^NSEI)."""
        ys = yahoo_symbol or f"{symbol}.NS"
        d = self._get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ys}", {"range": "1y", "interval": "1d"})
        res = d["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        pairs = [(t, c) for t, c in zip(res.get("timestamp") or [], q.get("close") or []) if c is not None]
        price = res["meta"].get("regularMarketPrice") or (pairs[-1][1] if pairs else None)
        times = [t for t, _c in pairs]
        closes = [c for _t, c in pairs]
        # yesterday's close: the last daily bar that isn't today's
        today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 19800))
        prev = None
        for t, c in reversed(pairs):
            if time.strftime("%Y-%m-%d", time.gmtime(t + 19800)) != today:
                prev = c
                break
        return {"price": price, "prev": prev, "closes": closes, "times": times}

    def profile(self, symbol: str) -> dict:
        d = self._get("https://query2.finance.yahoo.com/v1/finance/search",
                      {"q": f"{symbol}.NS", "quotesCount": 3, "newsCount": 0}, ttl=86400)
        for q in d.get("quotes", []):
            if q.get("symbol", "").upper() == f"{symbol}.NS".upper():
                return {"sector": q.get("sectorDisp") or q.get("sector") or "",
                        "industry": q.get("industryDisp") or q.get("industry") or ""}
        return {}

    def similar(self, symbol: str) -> list:
        d = self._get(f"https://query2.finance.yahoo.com/v6/finance/recommendationsbysymbol/{symbol}.NS", ttl=86400)
        out = []
        for r in (d.get("finance", {}).get("result") or [{}])[0].get("recommendedSymbols", []) or []:
            s = r.get("symbol", "")
            if s.endswith(".NS"):
                out.append(s[:-3])
        return out[:8]


def returns(h: dict) -> dict:
    """Today's move and 1-week / 1-month / 1-year returns from a Yahoo history."""
    c, p = h.get("closes") or [], h.get("price")
    out = {}
    if p and h.get("prev"):
        out["today"] = (p - h["prev"]) / h["prev"] * 100
    for name, n in (("1w", 5), ("1m", 21), ("1y", 250)):
        if p and len(c) > n:
            base = c[-n - 1]
            if base:
                out[name] = (p - base) / base * 100
    if c:
        out["high52"], out["low52"] = max(c[-250:]), min(c[-250:])
    return out
