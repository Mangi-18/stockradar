"""Live news watcher: polls RSS feeds every ~45 s and links each headline to
stocks (by company name / symbol) or to sectors (by theme keywords).

Why RSS: it is the fastest free machine-readable feed news sites publish,
usually seconds after the article goes live, and costs nothing.
"""
import re
import xml.etree.ElementTree as ET

import requests

DEFAULT_FEEDS = [
    # Economic Times – markets and stock news
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://economictimes.indiatimes.com/markets/stocks/news/rssfeeds/2146842.cms",
    # Moneycontrol – latest and buzzing stocks
    "https://www.moneycontrol.com/rss/latestnews.xml",
    "https://www.moneycontrol.com/rss/buzzingstocks.xml",
    # Livemint markets
    "https://www.livemint.com/rss/markets",
    # Business Standard, Financial Express, Hindu BusinessLine, NDTV Profit
    "https://www.business-standard.com/rss/markets-106.rss",
    "https://www.business-standard.com/rss/companies-101.rss",
    "https://www.thehindubusinessline.com/markets/feeder/default.rss",
    "https://feeds.feedburner.com/ndtvprofit-latest",
    # Government press releases (policy, contracts, duties, approvals)
    "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
    # Google News aggregates hundreds of Indian outlets; titles end with " - Publisher"
    "https://news.google.com/rss/search?q=NSE+shares+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=bags+order+OR+wins+contract+shares+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=Sensex+Nifty+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=GIFT+Nifty+OR+%22Wall+Street%22+OR+crude+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
]

# Theme -> (keywords, stocks most exposed). Policy and macro news moves whole
# sectors before any single company files anything.
THEMES = {
    "Defence": (r"defen[cs]e (order|deal|procurement|ministry|export|budget)|dac approv|indigeni[sz]ation|missile|fighter jet",
                ["HAL", "BEL", "BDL", "MAZDOCK", "COCHINSHIP", "BEML", "DATAPATTNS", "SOLARINDS"]),
    "Railways": (r"railway|vande bharat|rail (budget|project|order)|kavach|wagon",
                 ["IRFC", "RVNL", "IRCTC", "IRCON", "RAILTEL", "TITAGARH", "TEXRAIL", "JWL"]),
    "Crude oil": (r"crude|brent|opec|oil price|windfall tax",
                  ["ONGC", "OIL", "BPCL", "IOC", "HINDPETRO", "RELIANCE", "ASIANPAINT", "INDIGO"]),
    "Steel / metals": (r"steel (import|duty|price)|safeguard duty|anti-?dumping|iron ore|aluminium|copper price|metal prices",
                       ["TATASTEEL", "JSWSTEEL", "SAIL", "JINDALSTEL", "HINDALCO", "NATIONALUM", "NMDC", "VEDL"]),
    "RBI / rates": (r"\brbi\b|repo rate|monetary policy|rate (cut|hike)|crr\b|liquidity",
                    ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "BAJFINANCE", "LICHSGFIN", "DLF", "GODREJPROP"]),
    "Pharma / USFDA": (r"usfda|fda (approval|warning|inspection)|drug pric|pharma tariff",
                       ["SUNPHARMA", "DRREDDY", "CIPLA", "LUPIN", "AUROPHARMA", "ZYDUSLIFE", "GLENMARK", "BIOCON"]),
    "IT / US": (r"h-?1b|visa fee|outsourcing tax|us recession|rupee (falls|weakens|record low)|tech spending",
                ["TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "LTIM", "PERSISTENT", "COFORGE", "MPHASIS"]),
    "Power / renewables": (r"power demand|peak demand|solar (tender|module|duty)|renewable|green hydrogen|transmission",
                           ["NTPC", "POWERGRID", "TATAPOWER", "ADANIGREEN", "SUZLON", "NHPC", "JSWENERGY", "WAAREEENER"]),
    "Gold": (r"gold (price|import duty|hits|rall)|import duty on gold",
             ["TITAN", "KALYANKJIL", "SENCOGOLD", "MANAPPURAM", "MUTHOOTFIN"]),
    "Telecom": (r"tariff hike|spectrum|agr dues|telecom",
                ["BHARTIARTL", "IDEA", "INDUSTOWER", "TATACOMM"]),
    "Autos": (r"auto sales|ev (policy|subsidy)|fame scheme|pm e-drive|vehicle sales",
              ["MARUTI", "M&M", "TATAMOTORS", "BAJAJ-AUTO", "EICHERMOT", "HEROMOTOCO", "TVSMOTOR", "ASHOKLEY"]),
    "Rural / FMCG": (r"monsoon|rainfall|msp hike|rural demand|gst (cut|rate)",
                     ["HINDUNILVR", "DABUR", "MARICO", "BRITANNIA", "ITC", "COLPAL", "M&M", "UPL"]),
    "Infra / capex": (r"infrastructure push|capex|highway|nhai|metro project|smart cit",
                      ["LT", "IRB", "KNRCON", "PNCINFRA", "NCC", "ULTRACEMCO", "SIEMENS", "ABB"]),
}
# Global cues: logged for the morning brief, alerted only when dramatic.
GLOBAL = re.compile(r"gift nifty|wall street|dow jones|\bdow\b|nasdaq|s&p 500|\bfed\b|federal reserve|"
                    r"us (inflation|cpi|jobs|payroll|recession|tariff)|treasury yield|dollar index|nikkei|"
                    r"hang seng|asian markets|global markets|\bfii|\bfpi|foreign investors", re.I)
DRAMATIC = re.compile(r"crash|plunge|tumble|sell-?off|record (high|low)|surge|soar|war|attack|emergency", re.I)

# Words too generic to identify a company on their own ("Indian Bank" vs "Indian bank stocks").
COMMON = set("""india indian bank banks finance financial capital power energy steel global international
national general united the new star sun one first city gold oil gas home housing tech technologies
technology systems solutions services products chemicals chemical pharma pharmaceuticals foods food agro
textiles motors auto infra infrastructure projects realty holdings investments trust fund life insurance
industries enterprises limited ltd and & co corporation corp company of mills cement paper electric
electricals engineering exports trading money market markets securities""".split())

_THEMES = [(name, re.compile(rx, re.I), stocks) for name, (rx, stocks) in THEMES.items()]

POS = re.compile(r"\b(surge|soar|jump|rall(y|ies)|zoom|upgrade|beats?|record (high|profit)|bags|wins|"
                 r"approv|order|boost|hike in stake|buys stake|outperform|bullish)", re.I)
NEG = re.compile(r"\b(plunge|slump|crash|tank|fall|drop|downgrade|miss(es)?|probe|raid|penalt|fraud|"
                 r"default|ban|resign|loss|cut stake|sells stake|bearish|warning)", re.I)
# Headlines that only report a move that already happened ("shares jump 8%").
ALREADY_MOVED = re.compile(r"(shares?|stock|scrip)s?\s+(\w+\s+){0,3}(jump|surge|soar|rall|zoom|"
                           r"plunge|slump|crash|tank|fall|drop|rise|gain|decline)\w*\s+(\w+\s+){0,3}\d+(\.\d+)?\s?%",
                           re.I)

# Common short names used in headlines that differ from the official company name.
ALIASES = {
    "RELIANCE": ["reliance industries", "ril", "reliance"], "HDFCBANK": ["hdfc bank"],
    "ICICIBANK": ["icici bank"], "SBIN": ["sbi", "state bank of india"], "INFY": ["infosys"],
    "TCS": ["tcs", "tata consultancy"], "LT": ["l&t", "larsen"], "BHARTIARTL": ["airtel", "bharti airtel"],
    "M&M": ["mahindra & mahindra", "m&m"], "BAJFINANCE": ["bajaj finance"], "HINDUNILVR": ["hul", "hindustan unilever"],
    "TATAMOTORS": ["tata motors"], "TATASTEEL": ["tata steel"], "MARUTI": ["maruti"], "ITC": ["itc"],
    "KOTAKBANK": ["kotak mahindra bank", "kotak bank"], "AXISBANK": ["axis bank"], "ADANIENT": ["adani enterprises"],
    "SUNPHARMA": ["sun pharma"], "ONGC": ["ongc"], "NTPC": ["ntpc"], "HAL": ["hal", "hindustan aeronautics"],
    "BEL": ["bel", "bharat electronics"], "IRFC": ["irfc"], "RVNL": ["rvnl", "rail vikas"],
    "LICI": ["lic", "lic of india", "life insurance corporation"], "CUPID": ["cupid"],
}
_SUFFIX = re.compile(r"\b(limited|ltd\.?|corporation|corp\.?|company|co\.?|industries|india|"
                     r"\(india\)|enterprises|of)\b", re.I)


def build_market_matcher(names: dict) -> list:
    """Whole-market matcher (~2,000 NSE companies). Only distinctive full names are used,
    never bare symbols, to keep false matches low."""
    out = []
    for sym, company in names.items():
        clean = re.sub(r"\s+", " ", _SUFFIX.sub(" ", company or "")).strip()
        words = [w for w in re.split(r"[\s&.-]+", clean.lower()) if w]
        if len(clean) < 6 or not words or all(w in COMMON for w in words):
            continue
        out.append((sym, re.compile(r"(?i:\b" + re.escape(clean) + r"\b)")))
    return out


def split_source(title: str, default: str):
    """Google News titles look like 'Headline - Publisher'."""
    m = re.match(r"^(.*\S)\s+-\s+([^-]{2,40})$", title)
    return (m.group(1), m.group(2).strip()) if m else (title, default)


def build_matcher(names: dict) -> list:
    """names: {symbol: company name}. Returns [(symbol, compiled regex)].
    Short/ambiguous names are matched only as whole uppercase words (e.g. 'ITC')."""
    out = []
    for sym, company in names.items():
        terms = set(ALIASES.get(sym, []))
        clean = re.sub(r"\s+", " ", _SUFFIX.sub(" ", company or "")).strip()
        words = [w for w in re.split(r"[\s&.-]+", clean.lower()) if w]
        if len(clean) >= 5 and not all(w in COMMON for w in words):  # skip generic names like "Life Insurance"
            terms.add(clean.lower())
        long_terms = [t for t in terms if len(t) >= 5]
        short_terms = [t for t in terms if len(t) < 5] + ([sym] if len(sym) >= 3 else [])
        parts = []
        if long_terms:
            parts.append("(?i:" + "|".join(r"\b" + re.escape(t) + r"\b" for t in sorted(long_terms)) + ")")
        if short_terms:  # case-sensitive: 'ITC' yes, 'itc' inside a word no
            parts.append("|".join(r"\b" + re.escape(t.upper()) + r"\b" for t in sorted(set(short_terms))))
        if parts:
            out.append((sym, re.compile("|".join(parts))))
    return out


def tone(headline: str) -> str:
    p, n = bool(POS.search(headline)), bool(NEG.search(headline))
    return "+" if p and not n else "-" if n and not p else "?"


def analyse_headline(headline: str, matcher: list, market: list = ()) -> dict:
    stocks = [sym for sym, rx in matcher if rx.search(headline)]
    others = [sym for sym, rx in market if sym not in stocks and rx.search(headline)]
    themes = [(name, list(stocks_)) for name, rx, stocks_ in _THEMES if rx.search(headline)]
    return {"stocks": stocks[:5], "others": others[:3], "themes": themes[:2], "tone": tone(headline),
            "global": bool(GLOBAL.search(headline)), "dramatic": bool(DRAMATIC.search(headline)),
            "already_moved": bool(ALREADY_MOVED.search(headline))}


def parse_rss(xml_bytes: bytes) -> list:
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter():
        if it.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        get = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in it}
        link = get.get("link", "")
        if not link:
            for c in it:
                if c.tag.rsplit("}", 1)[-1] == "link" and c.get("href"):
                    link = c.get("href")
        title = re.sub(r"\s+", " ", get.get("title", "")).strip()
        if title:
            items.append({"title": title, "link": link, "id": get.get("guid") or link or title})
    return items


class NewsFeeds:
    def __init__(self, feeds: list, timeout: float = 8):
        self.feeds = feeds
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

    def fetch(self):
        """Yields (feed_url, items or Exception) so one broken feed never blocks the rest."""
        for url in self.feeds:
            try:
                r = self.s.get(url, timeout=self.timeout)
                r.raise_for_status()
                yield url, parse_rss(r.content)
            except Exception as e:
                yield url, e
