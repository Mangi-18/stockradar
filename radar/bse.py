"""BSE announcements API. Some companies file on BSE a little before NSE,
so watching both means you get whichever exchange is first."""
import re
from datetime import datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30))
URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
ATTACH = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}


def symbol_from_nsurl(nsurl: str) -> str:
    """BSE company URLs look like .../stock-share-price/<name>/<shortcode>/<scripcode>/.
    The short code usually equals the NSE symbol."""
    parts = [p for p in (nsurl or "").split("/") if p]
    if len(parts) >= 2 and parts[-1].isdigit():
        return re.sub(r"[^A-Z0-9&-]", "", parts[-2].upper())
    return ""


class BSE:
    def __init__(self, timeout: float = 10):
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def announcements(self) -> list:
        today = datetime.now(IST).strftime("%Y%m%d")
        params = {"pageno": 1, "strCat": -1, "strPrevDate": today, "strScrip": "",
                  "strSearch": "P", "strToDate": today, "strType": "C", "subcategory": -1}
        r = self.s.get(URL, params=params, timeout=self.timeout)
        r.raise_for_status()
        rows = r.json().get("Table", []) or []
        out = []
        for row in rows:
            att = row.get("ATTACHMENTNAME") or ""
            out.append({
                "id": f"bse:{row.get('NEWSID')}",
                "symbol": symbol_from_nsurl(row.get("NSURL", "")),
                "company": row.get("SLONGNAME", ""),
                "headline": (row.get("NEWSSUB") or row.get("HEADLINE") or "").strip(),
                "category": f"{row.get('CATEGORYNAME') or ''} {row.get('SUBCATNAME') or ''}".strip(),
                "url": ATTACH + att if att else "",
                "exchange": "BSE",
            })
        return out
