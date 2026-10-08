"""NSE website JSON endpoints (the same ones nseindia.com's own pages call).

NSE needs browser-like headers and session cookies, which it hands out when you
open the home page. When cookies expire, NSE answers 401/403, so we refresh and
retry once.
"""
import logging
import time
from datetime import date

import requests

log = logging.getLogger("nse")

BASE = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}


class NSE:
    def __init__(self, timeout: float = 10):
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._cookies_at = 0.0

    def _warm(self) -> None:
        self.s.cookies.clear()
        self.s.get(BASE, timeout=self.timeout)
        self.s.get(BASE + "/market-data/live-equity-market", timeout=self.timeout)
        self._cookies_at = time.time()

    def get(self, path: str, params: dict = None):
        if time.time() - self._cookies_at > 240:  # refresh cookies every ~4 min
            self._warm()
        r = self.s.get(BASE + path, params=params, timeout=self.timeout)
        if r.status_code in (401, 403):
            self._warm()
            r = self.s.get(BASE + path, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def fetch_bytes(self, url: str) -> bytes:
        """Download an attachment (XBRL/PDF) hosted on nsearchives / nseindia."""
        r = self.s.get(url, timeout=20)
        r.raise_for_status()
        return r.content

    # --- endpoints --------------------------------------------------------
    def announcements(self) -> list:
        """Latest corporate announcements, newest first."""
        data = self.get("/api/corporate-announcements", {"index": "equities"})
        return data if isinstance(data, list) else data.get("data", [])

    def financial_results(self) -> list:
        """Latest quarterly result filings, each with an 'xbrl' link."""
        data = self.get("/api/corporates-financial-results",
                        {"index": "equities", "period": "Quarterly"})
        return data if isinstance(data, list) else data.get("data", [])

    def board_meetings(self, start: date, end: date) -> list:
        fmt = "%d-%m-%Y"
        data = self.get("/api/corporate-board-meetings", {
            "index": "equities", "from_date": start.strftime(fmt), "to_date": end.strftime(fmt)})
        return data if isinstance(data, list) else data.get("data", [])

    def equity_list(self) -> dict:
        """Every NSE-listed company: {symbol: company name} (used to match news market-wide)."""
        import csv, io
        r = self.s.get("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv", timeout=20)
        r.raise_for_status()
        rows = csv.DictReader(io.StringIO(r.content.decode("utf-8", "ignore")))
        return {row["SYMBOL"].strip(): row.get("NAME OF COMPANY", "").strip()
                for row in rows if row.get("SYMBOL")}

    def quote(self, symbol: str) -> dict:
        """Live quote for one stock (used for portfolio stocks outside the scanned indices).
        Returned in the same shape as an index row."""
        d = self.get("/api/quote-equity", {"symbol": symbol})
        p = d.get("priceInfo", {})
        return {"symbol": symbol, "lastPrice": p.get("lastPrice"), "pChange": p.get("pChange"),
                "yearHigh": (p.get("weekHighLow") or {}).get("max"), "totalTradedVolume": None,
                "timestamp": d.get("metadata", {}).get("lastUpdateTime", "")}

    def pre_open(self, key: str = "ALL") -> list:
        """Pre-open session (9:00-9:08 IST): indicative opening price for each stock.
        Returns flat rows {symbol, pChange, iep, previousClose, totalTurnover}."""
        data = self.get("/api/market-data-pre-open", {"key": key})
        out = []
        for row in data.get("data", []):
            m = row.get("metadata", row)  # NSE nests the useful fields under "metadata"
            out.append({
                "symbol": m.get("symbol"),
                "pChange": m.get("pChange"),
                "iep": m.get("iep", m.get("lastPrice")),
                "previousClose": m.get("previousClose"),
                "turnover": m.get("totalTurnover"),
            })
        return out

    def large_deals(self) -> list:
        """Today's bulk and block deals: big investors buying/selling a stake."""
        data = self.get("/api/snapshot-capital-market-largedeal")
        out = []
        for kind, key in (("Bulk", "BULK_DEALS_DATA"), ("Block", "BLOCK_DEALS_DATA")):
            for r in data.get(key, []) or []:
                out.append({**r, "kind": kind})
        return out

    def insider_trades(self, start: date, end: date) -> list:
        """SEBI PIT disclosures: promoters/directors buying or selling their own stock."""
        fmt = "%d-%m-%Y"
        data = self.get("/api/corporates-pit", {
            "index": "equities", "from_date": start.strftime(fmt), "to_date": end.strftime(fmt)})
        return data if isinstance(data, list) else data.get("data", [])

    def index_stocks(self, index_name: str):
        """Live quote rows for every stock in an index (one call per index).
        Returns (rows, timestamp_text) — the timestamp tells us whether the data
        is from today's session (on holidays NSE keeps showing the last session)."""
        data = self.get("/api/equity-stockIndices", {"index": index_name})
        rows = data.get("data", [])
        # The first row is the index itself (priority 1); keep only stocks.
        stocks = [r for r in rows if r.get("priority", 0) == 0 and r.get("symbol") != index_name]
        return stocks, data.get("timestamp", "")
