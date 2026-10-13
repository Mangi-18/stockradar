"""Stock Radar engine (family edition).

The market is scanned ONCE for everyone; alerts are delivered PER PERSON:
  * Each person's PORTFOLIO: every relevant filing, news item, deal or price burst
    reaches them (routine items rate-limited per stock; urgent ones always go).
  * The rest of the market (~2,000 stocks): watched silently. A stock reaches a person
    only as ⚡ Breaking, 🎯 Opportunity or 📣 Story, within that person's own limits.
Anyone who presses Start on the bot gets their own portfolio and settings. The owner
(TELEGRAM_CHAT_ID in .env) can also update the engine, set the AI key and manage users.

Run:  python -m radar.main
"""
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from . import config
from .ai import AIReader
from .bse import BSE
from .channels import Channels, clean_name
from .classify import HIGH, MEDIUM, classify
from .outlook import outlook, short
from .news import (ALIASES, DEFAULT_FEEDS, NewsFeeds, analyse_headline, build_market_matcher,
                   THEMES, build_matcher, is_stale, rss_time, search_company_news, search_topic, split_source, tone)
from .market import Yahoo, returns
from .nse import NSE
from .results import analyse, crore
from .scoring import score_events
from .signals import Momentum
from .store import Store
from .telegram import Telegram

IST = timezone(timedelta(hours=5, minutes=30))
log = logging.getLogger("radar")
TONE = {"+": "🟢", "-": "🔴", "?": "🟡"}
ARROW = {1: "↑ likely positive", -1: "↓ likely negative", 0: "direction unclear"}


def now() -> datetime:
    return datetime.now(IST)


def is_weekday(t: datetime) -> bool:
    return t.weekday() < 5


def market_open(t: datetime) -> bool:
    return is_weekday(t) and (9, 15) <= (t.hour, t.minute) <= (15, 31)


def busy_hours(t: datetime) -> bool:
    """Results and big announcements mostly land between 7am and 10pm on weekdays."""
    return is_weekday(t) and 7 <= t.hour < 22


def in_quiet(t: datetime, start: int, end: int) -> bool:
    return (t.hour >= start or t.hour < end) if start > end else (start <= t.hour < end)


def esc(s) -> str:
    return html.escape(str(s or ""))


def pct(v) -> str:
    return "n/a" if v is None else f"{v:+.1f}%"


def event_label(e: dict) -> str:
    """The label a tracked signal was stored under (see Store.track calls)."""
    k, h = e["kind"], e["headline"]
    if k == "filing":
        return h.split(":")[0]
    if k == "ai":
        m = re.search(r"\b(small|medium|large)\b", h)
        return f"AI {m.group(1)}" if m else "AI"
    if k == "results":
        m = re.match(r"Q results (.+?) \(", h)
        return m.group(1) if m else "results"
    if k == "insider":
        return "promoter buy"
    if k == "deal":
        return "bulk/block buy"
    return classify(h)["label"]


# Fallback news searches per theme (used when no AI key is set)
THEME_QUERIES = {"Defence": "defence ministry order", "Railways": "railway budget order", "Crude oil": "crude oil price",
                 "Steel / metals": "steel import duty", "RBI / rates": "RBI repo rate", "Pharma / USFDA": "USFDA India pharma",
                 "IT / US": "H-1B visa IT services", "Power / renewables": "power demand India",
                 "Gold": "gold price India", "Telecom": "telecom tariff", "Autos": "auto sales India",
                 "Rural / FMCG": "monsoon rural demand", "Capital markets / STT": "securities transaction tax",
                 "Infra / capex": "infrastructure capex India"}


# Per-person settings: name -> (config attribute holding the default, type, description)
SETTINGS = {
    "maxopp": ("OPP_MAX_PER_DAY", int, "max opportunity alerts per day"),
    "score": ("OPP_MIN_SCORE", float, "evidence score (0-100) needed to ping you"),
    "digestscore": ("OPP_DIGEST_SCORE", float, "score needed to appear in digests"),
    "cooldown": ("STOCK_COOLDOWN_MIN", float, "minutes between routine pings per portfolio stock (0 = off)"),
    "gap": ("PREOPEN_MIN_GAP", float, "% pre-open gap for your stocks"),
    "outlets": ("STORY_MIN_OUTLETS", int, "outlets covering a stock before a 📣 story alert"),
    "maxstory": ("STORY_MAX_PER_DAY", int, "max 📣 story alerts per day"),
    "breakconf": ("BREAKING_MIN_CONF", float, "AI confidence (0-1) needed for a ⚡ breaking alert"),
    "maxbreak": ("BREAKING_MAX_PER_DAY", int, "max ⚡ breaking alerts per day"),
}


class User:
    """One person using the bot: their own portfolio, mutes, settings, digest and chat."""

    def __init__(self, radar, chat: str, name: str = ""):
        self.r, self.chat, self.name = radar, str(chat), name

    @property
    def is_owner(self) -> bool:
        return self.chat == str(config.TELEGRAM_CHAT_ID)

    def portfolio(self) -> set:
        extra = set(config.EXTRA_WATCHLIST) if self.is_owner else set()
        return extra | set(self.r.store.watchlist(self.chat))

    def muted(self) -> set:
        return self.r.store.muted(self.chat)

    def cfg(self, name: str):
        attr, typ, _ = SETTINGS[name]
        v = self.r.store.get(f"cfg:{self.chat}:{name}")
        return typ(float(v)) if v is not None else getattr(config, attr)

    def quiet_range(self):
        q = self.r.store.get(f"cfg:{self.chat}:quiet")
        if q:
            return (0, 0) if q == "off" else tuple(map(int, q.split("-")))
        return config.QUIET_START, config.QUIET_END

    def quiet(self, t: datetime = None) -> bool:
        s, e = self.quiet_range()
        return in_quiet(t or now(), s, e)

    def send(self, text, buttons=None, reply_keyboard=None):
        return self.r.tg.send(text, buttons=buttons, reply_keyboard=reply_keyboard, chat=self.chat)

    def send_long(self, lines):
        self.r.tg.send_long(lines, chat=self.chat)

    def queue(self, sym, text, score):
        self.r.store.queue(sym, text, score, chat=self.chat)

    def key(self, prefix: str, day: str, sym: str) -> str:
        return f"{prefix}:{day}:{self.chat}:{sym}"

    def count(self, prefix: str, day: str) -> int:
        return self.r.store.count_today(prefix, f"{day}:{self.chat}")


class Radar:
    def __init__(self):
        self.store = Store(config.DB_PATH)
        self.tg = Telegram(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
        self.nse = NSE()
        self.bse = BSE() if config.USE_BSE else None
        self.universe = set()        # index stocks we scan for prices
        self.failures = {}
        self.last_error = {}
        self.last_price = {}         # symbol -> latest price seen (for outcome tracking)
        self.stats, self._stats_at = {}, 0.0
        self._ai_cycle = 0
        self._ai_filing_cycle = 0
        self._story_ai = {}
        self._report_cache = {}      # symbol -> (time, AI analysis)
        self._news_cache = {}        # symbol -> (time, Google News headlines)
        self._ctx_cache = {}         # symbol -> (time, price/sector/peers/news context)
        self._sector_cache = {}      # symbol -> (time, sector/policy headlines)
        self._macro = []             # recent 🏛️ big-news events (time, topic words, symbols) to avoid repeats
        self._rows, self._rows_at, self._rows_fail_at = {}, 0.0, 0.0   # NSE rows used for peer ranking
        self.yahoo = Yahoo()
        self.awaiting = {}           # chat -> what their next typed message means
        self.result_tries = {}
        self.warmed_up = set()
        self.names = {}              # symbol -> company name
        self.matcher = []            # everyone's portfolio + index stocks (aliases, symbols)
        self.market_matcher = []     # every other NSE company, by full name
        self.day_change = {}         # symbol -> today's % change (to spot 'already moved')
        self.momentum = Momentum(config.MOMENTUM_WINDOW_MIN * 60, config.MOMENTUM_MOVE,
                                 config.MOMENTUM_VOL_MULT)
        self.news = NewsFeeds(config.NEWS_FEEDS or DEFAULT_FEEDS)
        self.channels = Channels()
        self._ai_chan_cycle = 0
        if self.store.get("initialized"):
            self.warmed_up = {"nse", "bse", "results", "insider", "deals"}
        owner = str(config.TELEGRAM_CHAT_ID or "")
        if owner:
            self.store.user_add(owner, "owner")
            self.store.migrate_single_user(owner)
        if not self.store.get("cleanup_old_results_v1"):
            self.store.db.execute("DELETE FROM events WHERE kind = 'results' AND headline NOT LIKE 'Q results (quarter to%'")
            self.store.db.commit()
            self.store.set("cleanup_old_results_v1", "1")
        self.ai = AIReader(self.store.get("ai_key") or config.AI_KEY, config.AI_MAX_PER_DAY)
        self.ai.model = config.AI_MODEL

    # ================================================================== people
    def users(self) -> list:
        return [User(self, c, n) for c, n in self.store.users("active")]

    def user(self, chat: str) -> User:
        return User(self, chat)

    @property
    def owner(self) -> User:
        return User(self, str(config.TELEGRAM_CHAT_ID))

    def portfolio(self) -> set:
        """Everyone's portfolios combined (for matching news and fetching prices)."""
        out = set()
        for u in self.users():
            out |= u.portfolio()
        return out

    def holders(self, sym: str) -> list:
        return [u for u in self.users() if sym in u.portfolio() and sym not in u.muted()]

    def outsiders(self, sym: str) -> list:
        """People who don't hold this stock and haven't muted it (candidates for market alerts)."""
        return [u for u in self.users() if sym not in u.portfolio() and sym not in u.muted()]

    def broadcast(self, text: str, buttons=None) -> None:
        for u in self.users():
            u.send(text, buttons=buttons)

    # ================================================================== lists
    def refresh_universe(self) -> None:
        syms = set()
        err = None
        for idx in config.INDICES:
            try:
                rows, _ = self.nse.index_stocks(idx)
            except Exception as e:  # one index failing shouldn't lose the others
                err = e
                continue
            for r in rows:
                syms.add(r["symbol"])
                self.names[r["symbol"]] = (r.get("meta") or {}).get("companyName", "")
        if syms:
            self.universe = syms
        try:
            for s, n in self.nse.equity_list().items():
                self.names.setdefault(s, n)
        except Exception as e:
            self.fail("equity list", e)
        self.rebuild_matcher()
        if err and not syms:
            raise err
        log.info("scan list %d, market-wide news matching %d", len(self.universe), len(self.market_matcher))

    def rebuild_matcher(self) -> None:
        close = self.portfolio() | self.universe
        self.matcher = build_matcher({s: self.names.get(s, "") for s in close})
        self.market_matcher = build_market_matcher(
            {s: n for s, n in self.names.items() if s not in close})

    # ================================================================== routing
    def outlook_of(self, sym: str) -> dict:
        return outlook(self.store.events_for(sym, 24))

    def outlook_line(self, sym: str, o: dict = None) -> str:
        o = o or self.outlook_of(sym)
        if not o["label"]:
            return ""
        line = f"📊 <b>Overall (24h): {o['label']}</b> · {o['pos']} positive vs {o['neg']} negative"
        if o["sign"] and o["drivers"].get(o["sign"]):
            line += f"\n   Biggest factor: {esc(short(o['drivers'][o['sign']]))}"
        return line

    def with_outlook(self, sym: str, text: str) -> tuple:
        """Puts the stock's overall verdict right under the alert's first line, so a single
        green or red item is never read on its own. Also detects a flip of the verdict."""
        o = self.outlook_of(sym)
        line = self.outlook_line(sym, o)
        flipped = False
        if o["sign"]:
            prev = self.store.get(f"outlook:{sym}")
            if prev and int(prev) == -o["sign"]:
                flipped = True
                line = (f"🔄 <b>Outlook flipped to {'positive 🟢' if o['sign'] > 0 else 'negative 🔴'}</b>\n" + line)
            self.store.set(f"outlook:{sym}", o["sign"])
        if not line:
            return text, False
        first, _, rest = text.partition("\n")
        return f"{first}\n{line}" + (f"\n{rest}" if rest else ""), flipped

    def notify(self, sym: str, text: str, urgent: bool = False) -> None:
        """Portfolio alerts, to everyone who holds the stock. Urgent ones always go out;
        routine ones respect each person's quiet hours and per-stock cooldown. Every alert
        carries the stock's overall verdict; a flip of that verdict is always urgent."""
        holders = self.holders(sym)
        if not holders:
            return
        text, flipped = self.with_outlook(sym, text)
        urgent = urgent or flipped
        for u in holders:
            if not urgent:
                cooling = False
                cd = u.cfg("cooldown")
                if cd > 0:  # 0 = no cooldown
                    slot = int(time.time() // (cd * 60))
                    cooling = not self.store.first_time(f"cool:{u.chat}:{sym}:{slot}")
                if u.quiet() or cooling:
                    u.queue(sym, text, 50)
                    continue
            u.send(text)

    def scored(self, sym: str) -> dict:
        evs = self.store.events_for(sym, 24)
        s = score_events(evs, self.day_change.get(sym))
        adj, why = self.learned_adjust(evs)
        if adj:
            s["score"] = max(0, min(100, s["score"] + adj))
            s["reasons"].append(why)
        return s

    def consider(self, sym: str) -> None:
        """A stock produced a signal. Score its evidence once, then decide per person
        (who doesn't hold it): ping, digest, or stay silent."""
        if not sym:
            return
        people = self.outsiders(sym)
        if not people:
            return
        s = self.scored(sym)
        day = now().strftime("%Y-%m-%d")
        for u in people:
            if s["score"] >= u.cfg("score") and not u.quiet():
                used = u.count("opp", day)
                if (used < u.cfg("maxopp") and not self.store.has_seen(u.key("story", day, sym))
                        and not self.store.has_seen(u.key("brk", day, sym))
                        and self.store.first_time(u.key("opp", day, sym))):
                    u.send(self.format_opportunity(sym, s, used + 1, u.cfg("maxopp")), buttons=self.stock_buttons(sym))
                    self.record_prediction(sym, "opportunity", f"{s['score']:.0f}/100", s["direction"],
                                           s["reasons"][0] if s["reasons"] else "")
                    continue
            if s["score"] >= u.cfg("digestscore") and self.store.first_time(u.key("oppq", day, sym)):
                u.queue(sym, self.format_candidate(sym, s), s["score"])

    def format_opportunity(self, sym, s, n, cap) -> str:
        ev = self.store.events_for(sym, 24)[-3:]
        lines = [f"🎯 <b>Opportunity · {esc(sym)}</b> {ARROW[s['direction']]}",
                 f"Evidence score <b>{s['score']:.0f}/100</b> · alert {n} of {cap} today",
                 *[f"   {esc(r)}" for r in s["reasons"]],
                 "<b>Latest evidence</b>",
                 *[f"• {TONE.get(e['tone'], '🟡')} {esc(e['headline'][:150])} <i>({esc(e['source'] or e['kind'])})</i>"
                   for e in reversed(ev)],
                 "<i>Score = strength of evidence, not a guarantee.</i>"]
        return "\n".join(lines)

    def format_candidate(self, sym, s) -> str:
        last = self.store.events_for(sym, 24)[-1:]
        head = esc(last[0]["headline"][:120]) if last else ""
        return f"{'🟢' if s['direction'] > 0 else '🔴' if s['direction'] < 0 else '🟡'} <b>{esc(sym)}</b> {s['score']:.0f}/100 · {head}"

    # ================================================================== filings
    def _nse_filings(self) -> list:
        out = []
        for a in self.nse.announcements():
            out.append({
                "id": f"nse:{a.get('seq_id') or a.get('an_dt')}:{a.get('symbol')}",
                "symbol": (a.get("symbol") or "").upper(),
                "company": a.get("sm_name", ""),
                "headline": (a.get("attchmntText") or a.get("desc") or "").strip(),
                "category": a.get("desc", ""),
                "url": a.get("attchmntFile") or "",
                "exchange": "NSE",
            })
        return out

    def poll_filings(self) -> None:
        self._ai_filing_cycle = 0
        sources = [("nse", self._nse_filings)]
        if self.bse:
            sources.append(("bse", self.bse.announcements))
        for name, fetch in sources:
            try:
                items = fetch()
                self.ok(name)
            except Exception as e:
                self.fail(name, e)
                continue
            first_pass = name not in self.warmed_up
            for it in reversed(items):
                self.handle_filing(it, silent=first_pass)
            self.warmed_up.add(name)
        self.store.set("initialized", "1")

    def handle_filing(self, it: dict, silent: bool = False) -> None:
        if not self.store.first_time(it["id"]):
            return
        c = classify(it["headline"], it["category"])
        sym = it["symbol"]
        day = now().strftime("%Y-%m-%d")
        dedup = (f"filing:{day}:{sym}:{c['label']}:{it['headline'][:40].lower()}"
                 if c["label"] == "Other" else f"filing:{day}:{sym}:{c['label']}")
        if not self.store.first_time(dedup) or not sym:
            return
        self.store.add_filing(sym, it["headline"][:200], c["impact"], it["url"])
        self.store.log_event(sym, "filing", f"{c['label']}: {it['headline'][:250]}", c["tone"],
                             c["impact"], it["exchange"], it["url"])
        if not silent and c["impact"] == HIGH and c["tone"] in "+-":
            self.store.track(sym, "filing", c["label"], 1 if c["tone"] == "+" else -1)
        if silent or c["impact"] not in (HIGH, MEDIUM):
            return
        if self.holders(sym):
            msg = (f"{TONE[c['tone']]} <b>⭐ {esc(sym)}</b> · {esc(c['label'])}\n{esc(it['headline'][:600])}\n"
                   f"<i>{it['exchange']} filing · {now().strftime('%H:%M:%S')}</i>")
            if it["url"]:
                msg += f'\n<a href="{esc(it["url"])}">Open filing</a>'
            if c["label"] == "Quarterly results":
                msg += "\n⏳ Numbers follow as soon as NSE publishes them."
            self.notify(sym, msg, urgent=c["impact"] == HIGH)
        if c["label"] == "Quarterly results":  # others' results are judged on the numbers
            return
        if c["impact"] == HIGH and self.ai.provider and self._ai_filing_cycle < 6 and self.outsiders(sym):
            # An exchange filing is the earliest public source. Ask the AI how big it is
            # for THIS company (a ₹500 cr order is huge for a small cap, nothing for L&T).
            self._ai_filing_cycle += 1
            r = self.ai.read(f"{it.get('company') or sym} (NSE: {sym}) exchange filing — {c['label']}: "
                             f"{it['headline'][:400]}", f"{it['exchange']} filing", sorted(self.portfolio()))
            i = next((x for x in (r or {}).get("impacts", []) if x["symbol"] == sym), None)
            if i and i["confidence"] >= 0.5:
                up = i["direction"] == "up"
                strong = i["magnitude"] == "large" and i["confidence"] >= 0.7
                self.store.log_event(sym, "ai", f"AI: {'↑' if up else '↓'} {i['magnitude']} ({i['confidence']:.0%}) — "
                                     f"{i['why']}", "+" if up else "-", HIGH if strong else MEDIUM, it["exchange"], it["url"])
                self.store.track(sym, "ai", f"AI {i['magnitude']}", 1 if up else -1)
                if not (r or {}).get("priced_in"):
                    self.breaking_check(i, f"{c['label']}: {it['headline'][:300]}", f"{it['exchange']} filing", it["url"])
        self.consider(sym)

    # ================================================================== results
    def poll_results(self) -> None:
        try:
            rows = self.nse.financial_results()
            self.ok("results")
        except Exception as e:
            self.fail("results", e)
            return
        first_pass = "results" not in self.warmed_up
        for r in rows[:60]:
            sym = (r.get("symbol") or "").upper()
            xbrl = r.get("xbrl") or ""
            kind = "Consolidated" if str(r.get("consolidated", "")).lower().startswith("consol") else "Standalone"
            key = f"res:{sym}:{r.get('toDate')}:{kind}"
            if first_pass:
                if self.store.get(key) is None:
                    self.store.set(key, "done")
                continue
            if not xbrl.startswith("http") or not xbrl.lower().endswith(".xml") or self.store.get(key) == "done":
                continue
            tries = self.result_tries.get(key, 0)
            if tries >= 3:
                continue
            self.result_tries[key] = tries + 1
            try:
                a = analyse(self.nse.fetch_bytes(xbrl))
            except Exception as e:
                log.warning("xbrl %s: %s", sym, e)
                continue
            self.store.set(key, "done")
            if not a.get("ok"):
                continue
            try:
                q_end = datetime.fromisoformat(a["quarter_end"])
            except (KeyError, ValueError):
                continue
            if (now().replace(tzinfo=None) - q_end).days > 140:
                log.info("skip old-quarter result %s %s", sym, a["quarter_end"])
                continue  # a late or revised filing for an old quarter isn't news now
            if a["score"]:
                self.store.track(sym, "results", a["verdict"], 1 if a["score"] > 0 else -1)
            tone = "+" if a["score"] > 0 else "-" if a["score"] < 0 else "?"
            self.store.log_event(sym, "results", f"Q results (quarter to {a['quarter_end']}) {a['verdict']} ({a['score']:+d}): "
                                 + "; ".join(a["reasons"]), tone, HIGH if abs(a["score"]) >= 3 else MEDIUM, "NSE")
            self.notify(sym, self.format_result(sym, kind, a), urgent=True)
            self.consider(sym)
        self.warmed_up.add("results")

    def format_result(self, sym, kind, a) -> str:
        icon = "🟢" if a["score"] > 0 else "🔴" if a["score"] < 0 else "🟡"
        rev, pat = a["revenue"], a["profit"]
        lines = [
            f"{icon} <b>⭐ {esc(sym)}</b> Q results · {kind} · quarter ending {a['quarter_end']}",
            f"<b>{a['verdict']}</b> (score {a['score']:+d} of ±4)",
            f"Revenue {crore(rev['cur'])}  YoY {pct(a['rev_yoy'])}  QoQ {pct(a['rev_qoq'])}",
            f"Net profit {crore(pat['cur'])}  YoY {pct(a['pat_yoy'])}  QoQ {pct(a['pat_qoq'])}",
        ]
        if a.get("eps") is not None:
            lines.append(f"EPS ₹{a['eps']:.2f}")
        if a["reasons"]:
            lines.append("Why: " + "; ".join(esc(x) for x in a["reasons"]))
        lines.append("<i>Compared with last year, not analyst estimates.</i>")
        return "\n".join(lines)

    # ================================================================== prices
    def poll_prices(self) -> None:
        t = now()
        today = t.strftime("%d-%b-%Y")
        self.momentum.day_start = t.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
        rows = []
        for idx in config.INDICES:
            try:
                r, stamp = self.nse.index_stocks(idx)
                self.ok("prices")
            except Exception as e:
                self.fail("prices", e)
                continue
            if stamp and today.lower() not in stamp.lower():
                continue  # holiday / stale data
            rows += r
        held = self.portfolio()
        seen = {r.get("symbol") for r in rows}
        for sym in sorted(held - seen):  # portfolio stocks outside the indices
            try:
                rows.append(self.nse.quote(sym))
            except Exception as e:
                log.warning("quote %s: %s", sym, e)

        for r in rows:
            sym = r.get("symbol")
            try:
                chg, price = float(r.get("pChange")), float(r.get("lastPrice"))
            except (TypeError, ValueError):
                continue
            self.day_change[sym] = chg
            self.last_price[sym] = price
            try:
                vol = float(r.get("totalTradedVolume") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            sig = self.momentum.update(sym, price, vol, t.timestamp())
            if sig and self.store.first_time(f"burst:{today}:{t.hour}:{sym}:{'+' if sig['move'] > 0 else '-'}"):
                vtxt = f", volume {sig['vol_ratio']:.1f}× normal pace" if sig["strong"] else ""
                self.store.log_event(sym, "burst", f"Price {sig['move']:+.1f}% in {sig['minutes']:.0f} min{vtxt}",
                                     "+" if sig["move"] > 0 else "-", MEDIUM, "NSE")
                if sym in held:
                    self.notify(sym, f"🚀 <b>⭐ {esc(sym)}</b> {sig['move']:+.1f}% in {sig['minutes']:.0f} min{vtxt} "
                                     f"· day {chg:+.1f}% · ₹{price:,.2f}\n" + "\n".join(self._reasons(sym)),
                                urgent=sig["strong"])
                self.consider(sym)
            if sym in held:
                crossed = [lv for lv in config.MOVE_LEVELS if abs(chg) >= lv]
                new = [lv for lv in crossed
                       if self.store.first_time(f"move:{today}:{sym}:{'+' if chg > 0 else '-'}{lv}")]
                if new:
                    self.notify(sym, f"{'🟢' if chg > 0 else '🔴'} <b>⭐ {esc(sym)}</b> {chg:+.2f}% at ₹{price:,.2f} "
                                     f"(crossed {max(new):g}%)\n" + "\n".join(self._reasons(sym)), urgent=True)
                try:
                    if price >= float(r.get("yearHigh")) > 0 and self.store.first_time(f"52wh:{today}:{sym}"):
                        for u in self.holders(sym):
                            u.queue(sym, f"📈 <b>{esc(sym)}</b> new 52-week high ₹{price:,.2f}", 45)
                except (TypeError, ValueError):
                    pass

    def _reasons(self, sym: str, hours: float = 24) -> list:
        return [f"   ↳ {esc(h[:140])}" for h, _, _ in self.store.recent_filings(sym, hours)]

    # ================================================================== pre-open
    def preopen_scan(self) -> None:
        """~9:06 IST: each person's opening gaps, plus at most 5 other big gaps that
        have news behind them (a gap with no news is usually noise)."""
        try:
            rows = self.nse.pre_open("ALL")
            self.ok("preopen")
        except Exception as e:
            self.fail("preopen", e)
            return
        gaps, others = {}, []
        for r in rows:
            try:
                chg = float(r["pChange"])
            except (TypeError, ValueError, KeyError):
                continue
            sym = r.get("symbol") or ""
            gaps[sym] = (chg, r.get("iep"))
            if abs(chg) >= config.PREOPEN_MIN_GAP_ALL and self.store.recent_filings(sym, 18):
                self.store.log_event(sym, "gap", f"Pre-open gap {chg:+.1f}%", "+" if chg > 0 else "-", MEDIUM, "NSE")
                others.append((chg, sym, r.get("iep")))

        def fmt(chg, sym, iep, star):
            return [f"{'🟢' if chg > 0 else '🔴'} {star}<b>{esc(sym)}</b> {chg:+.1f}%"
                    + (f" · open ~₹{float(iep):,.2f}" if iep not in (None, "", "-") else "")] + self._reasons(sym, 18)

        for u in self.users():
            mine = [(gaps[s][0], s, gaps[s][1]) for s in u.portfolio()
                    if s in gaps and abs(gaps[s][0]) >= u.cfg("gap")]
            out = ["🌅 <b>Pre-open</b> (indicative, market opens 9:15)", "<b>Your portfolio</b>"]
            for g in sorted(mine, key=lambda x: -abs(x[0])):
                out += fmt(*g, "⭐ ")
            if not mine:
                out.append("• no big gaps")
            theirs = [g for g in others if g[1] not in u.portfolio() and g[1] not in u.muted()]
            if theirs:
                out.append("<b>Other big gaps with news behind them</b>")
                for g in sorted(theirs, key=lambda x: -abs(x[0]))[:5]:
                    out += fmt(*g, "")
            u.send("\n".join(out))

    # ================================================================== insiders / deals
    def poll_insiders(self) -> None:
        t = now()
        try:
            rows = self.nse.insider_trades((t - timedelta(days=2)).date(), t.date())
            self.ok("insider")
        except Exception as e:
            self.fail("insider", e)
            return
        first_pass = "insider" not in self.warmed_up
        for r in rows:
            sym = (r.get("symbol") or "").upper()
            who = r.get("acqName") or r.get("personName") or ""
            cat = r.get("personCategory") or ""
            kind = (r.get("tdpTransactionType") or r.get("acqMode") or "").strip()
            key = f"pit:{sym}:{who}:{r.get('secAcq')}:{r.get('date') or r.get('acqfromDt')}:{kind}"
            if not self.store.first_time(key) or first_pass:
                continue
            try:
                value_cr = float(str(r.get("secVal") or 0).replace(",", "")) / 1e7
            except ValueError:
                value_cr = 0
            buy = re.search(r"buy|acqui", kind, re.I) and not re.search(
                r"pledge|revoc|invoc|gift|esop", kind + " " + str(r.get("acqMode")), re.I)
            sell = re.search(r"sell|dispos", kind, re.I)
            if not re.search(r"promoter", cat, re.I) or value_cr < config.INSIDER_MIN_CR or not (buy or sell):
                continue
            what = f"Promoter {'bought' if buy else 'sold'} ₹{value_cr:,.1f} cr ({who})"
            self.store.add_filing(sym, what, HIGH, "")
            self.store.log_event(sym, "insider", what, "+" if buy else "-", HIGH, "NSE")
            self.notify(sym, f"{'🟢' if buy else '🔴'} <b>⭐ {esc(sym)}</b> · {esc(what)} via "
                             f"{esc(r.get('acqMode') or 'market')}", urgent=True)
            if buy:
                self.store.track(sym, "insider", "promoter buy", 1)
                self.consider(sym)
        self.warmed_up.add("insider")

    def poll_deals(self) -> None:
        try:
            rows = self.nse.large_deals()
            self.ok("deals")
        except Exception as e:
            self.fail("deals", e)
            return
        first_pass = "deals" not in self.warmed_up
        for r in rows:
            sym = (r.get("symbol") or "").upper()
            try:
                qty = float(str(r.get("qty") or 0).replace(",", ""))
                px = float(str(r.get("watp") or 0).replace(",", ""))
            except ValueError:
                continue
            value_cr = qty * px / 1e7
            side = (r.get("buySell") or "").upper()
            key = f"deal:{r.get('date')}:{sym}:{r.get('clientName')}:{side}:{qty}"
            if not self.store.first_time(key) or first_pass or value_cr < config.DEAL_MIN_CR:
                continue
            what = f"{r['kind']} deal: {r.get('clientName')} {side} ₹{value_cr:,.0f} cr @ ₹{px:,.2f}"
            self.store.add_filing(sym, what, MEDIUM, "")
            self.store.log_event(sym, "deal", what, "+" if side == "BUY" else "-" if side == "SELL" else "?",
                                 MEDIUM, "NSE")
            self.notify(sym, f"🐋 <b>⭐ {esc(sym)}</b> · {esc(what)}")
            if side == "BUY":
                self.store.track(sym, "deal", "bulk/block buy", 1)
                self.consider(sym)
        self.warmed_up.add("deals")

    # ================================================================== news
    def poll_news(self) -> None:
        self._ai_cycle = 0
        first = "news" not in self.warmed_up
        for url, items in self.news.fetch():
            host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
            if isinstance(items, Exception):
                self.fail("news:" + host, items)
                continue
            self.ok("news:" + host)
            for it in items[:40]:
                title, src = it["title"], host
                if "news.google." in host:
                    title, src = split_source(title, host)
                if is_stale(title, rss_time(it.get("date", "")), max_age_h=48):
                    continue  # old article resurfacing in a feed, or about an old results quarter
                if not self.store.first_time("news:" + re.sub(r"\W+", "", title.lower())[:120]) or first:
                    continue
                self.handle_headline(title, src, it["link"])
        self.warmed_up.add("news")

    POLICY = re.compile(r"government|ministry|cabinet|\brbi\b|sebi|duty|tariff|\bban\b|policy|scheme|subsid|"
                        r"\btax|gst|import|export|price (hike|cut)|approv|crude|opec|monsoon|order|contract|"
                        r"acqui|merger|stake|probe|raid|downgrade|upgrade|recall|strike|shortage", re.I)

    def wants_ai(self, a, c, syms, mine) -> bool:
        if not self.ai.provider or a["already_moved"]:
            return False
        # Each AI read takes a few seconds; cap per news cycle so filings/prices never wait long.
        policy = bool(a["themes"] or self.POLICY.search(a.get("_title", "")))
        if self._ai_cycle >= (12 if policy else 6) and not mine:
            return False
        self._ai_cycle += 1
        spreading = any(self.store.news_sources(s, config.STORY_WINDOW_H) >= config.STORY_MIN_OUTLETS - 1
                        for s in syms)  # this headline may be the one that makes it a story
        return bool(mine or spreading or a["themes"] or (a["global"] and a["dramatic"])
                    or (syms and c["impact"] == HIGH) or self.POLICY.search(a.get("_title", "")))

    def handle_headline(self, title: str, src: str, link: str, _unused=None) -> None:
        a = analyse_headline(title, self.matcher, self.market_matcher)
        a["_title"] = title
        c = classify(title)
        t = a["tone"] if a["tone"] != "?" else c["tone"]
        held = self.portfolio()
        syms = a["stocks"] + a["others"]
        mine = [s for s in syms if s in held]

        # --- AI reasoning: who is affected, which way, and why (incl. second-order effects)
        ai_imp, ai = [], None
        if self.wants_ai(a, c, syms, mine):
            ai = self.ai.read(title, src, sorted(held))
            if ai:
                if ai["priced_in"]:
                    a["already_moved"] = True
                if ai["relevant"]:
                    # guard against made-up symbols: keep only real NSE symbols
                    ai_imp = [i for i in ai["impacts"] if i["confidence"] >= 0.5
                              and (i["symbol"] in self.names or i["symbol"] in held)]
        ai_by = {i["symbol"]: i for i in ai_imp}
        macro = bool(ai and ai["relevant"] and ai.get("importance") == "high" and ai_imp)
        if macro:
            self.macro_alert(title, src, link, ai, ai_imp)

        if a["already_moved"]:
            title_logged, label = title + " (already moving)", "⏱️ already moving"
        else:
            title_logged, label = title, (c["label"] if c["label"] != "Other" else "News")
        impact = c["impact"] if c["impact"] != "LOW" or t == "?" else MEDIUM
        stamp = f"<i>{esc(src)} · {now().strftime('%d %b %H:%M')}</i>" + (
            f'\n<a href="{esc(link)}">Read</a>' if link else "")

        for s in syms:
            self.store.log_event(s, "news", title_logged, t, impact, src, link)
            self.store.add_filing(s, "News: " + title[:180], impact, link)
            if impact == HIGH and t in "+-" and s not in ai_by and not a["already_moved"]:
                self.store.track(s, "news", c["label"], 1 if t == "+" else -1)
        for i in ai_imp:
            s, up = i["symbol"], i["direction"] == "up"
            strong = i["magnitude"] == "large" and i["confidence"] >= 0.7
            self.store.log_event(s, "ai", f"AI: {'↑' if up else '↓'} {i['magnitude']} ({i['confidence']:.0%}) — "
                                 f"{i['why']} [{title[:120]}]", "+" if up else "-", HIGH if strong else MEDIUM, src, link)
            self.store.add_filing(s, f"AI: {i['why']}", HIGH if strong else MEDIUM, link)
            if not a["already_moved"] and not i.get("moved"):
                self.store.track(s, "ai", f"AI {i['magnitude']}", 1 if up else -1)
        for name, _ in a["themes"]:
            self.store.log_event("", "theme:" + name, title, t, "MEDIUM", src, link)
        if a["global"]:
            self.store.log_event("", "global", title, t, "MEDIUM", src, link)

        # --- portfolios: direct mentions + stocks the AI says are affected
        for s in mine + [s for s in ai_by if s in held and s not in mine]:
            i = ai_by.get(s)
            tone_s = ("+" if i["direction"] == "up" else "-") if i else t
            ai_line = (f"\n🤖 {'↑' if i['direction'] == 'up' else '↓'} {i['magnitude']} impact likely "
                       f"({i['confidence']:.0%}, {i['order']}): {esc(i['why'])}") if i else ""
            urgent = not a["already_moved"] and (impact == HIGH or bool(
                i and i["magnitude"] in ("medium", "large") and i["confidence"] >= 0.7))
            self.notify(s, f"📰 {TONE[tone_s]} <b>⭐ {esc(s)}</b> · {esc(label)}\n{esc(title)}{ai_line}\n{stamp}",
                        urgent=urgent)
        for s in set(syms) | set(ai_by):
            self.consider(s)
        if not a["already_moved"]:
            for i in ai_imp:
                if not i.get("moved"):
                    self.breaking_check(i, title, src, link)
        for s in syms:
            self.story_check(s, title, src, ai_by.get(s), a["already_moved"])

        # --- sector / policy news: ping only people it affects; others get it in the digest
        if a["themes"] and config.NEWS_THEMES and not a["already_moved"] and not macro:
            name, exposed = a["themes"][0]
            if ai_by:
                who = ", ".join(f"{s} {'↑' if i['direction'] == 'up' else '↓'}" for s, i in ai_by.items())
                body = f"🤖 Likely impact: {esc(who)}\n" + "".join(
                    f"   • {esc(s)}: {esc(i['why'])}\n" for s, i in list(ai_by.items())[:3])
            else:
                body = f"Most exposed: {esc(', '.join(exposed[:6]))}\n"
            slot = int(time.time() // 3600)
            for u in self.users():
                hit = [s for s in set(exposed) | set(ai_by) if s in u.portfolio()]
                msg = (f"🧭 <b>Sector news · {esc(name)}</b>\n{esc(title)}\n" + body
                       + (f"Affects your: <b>{esc(', '.join(sorted(hit)))}</b>\n" if hit else "") + stamp)
                if hit and not u.quiet() and self.store.first_time(f"theme:{u.chat}:{name}:{slot}"):
                    u.send(msg)
                else:
                    u.queue("", msg, 30)
        elif a["global"] and a["dramatic"] and self.store.first_time(f"global:{int(time.time() // 3600)}"):
            self.broadcast(f"🌍 <b>Global cue</b>\n{esc(title)}\n{stamp}")

    def macro_alert(self, title: str, src: str, link: str, ai: dict, imps: list) -> None:
        """🏛️ Big market-moving news (budget, taxes, RBI, SEBI, government, global shocks): sent to everyone
        once per event, explained in plain words with who gains and who loses, even if no stock is named."""
        words = set(re.findall(r"[a-z0-9]+", ai.get("topic", ""))) or set(re.findall(r"[a-z0-9]{4,}", title.lower()))
        syms = {i["symbol"] for i in imps}
        cutoff = time.time() - 12 * 3600
        self._macro = [m for m in self._macro if m[0] > cutoff]
        for _t, w, ss in self._macro:   # the same event reported by another outlet
            if (w and len(w & words) / max(1, min(len(w), len(words))) >= 0.5) or len(ss & syms) >= max(2, len(syms) * 2 // 3):
                return False
        self._macro.append((time.time(), words, syms))
        ups = [i for i in imps if i["direction"] == "up"]
        downs = [i for i in imps if i["direction"] == "down"]
        day = now().strftime("%Y-%m-%d")
        topic = re.sub(r"\W+", "-", ai.get("topic") or title.lower())[:40]
        for u in self.users():
            mine = [i["symbol"] for i in imps if i["symbol"] in u.portfolio()]
            lines = [f"🏛️ <b>Big market news</b>" + (f" · affects your <b>{esc(', '.join(mine))}</b>" if mine else "")]
            lines.append(esc(ai.get("summary") or title))
            for label, group in (("🟢 Likely to gain", ups), ("🔴 Likely to lose", downs)):
                if group:
                    lines.append(f"\n<b>{label}</b>")
                    for i in group[:4]:
                        star = "⭐ " if i["symbol"] in u.portfolio() else ""
                        lines.append(f"{star}<b>{esc(i['symbol'])}</b> ({i['magnitude']}, {i['confidence']:.0%}): {esc(i['why'])}"
                                     + (" ⏱️ already moved" if i.get("moved") else ""))
            lines.append(f"<i>{esc(src)} · {now().strftime('%d %b %H:%M')}</i>")
            msg = "\n".join(lines)
            if self.store.has_seen(u.key("macro", day, topic)):
                continue
            if (u.quiet() and not mine) or u.count("macro", day) >= config.MACRO_MAX_PER_DAY:
                u.queue("", msg, 70)
                continue
            self.store.first_time(u.key("macro", day, topic))
            u.send(msg, buttons=[[(f"🔎 {i['symbol']}", f"stock:{i['symbol']}") for i in imps[:3]]])

    def breaking_check(self, i: dict, title: str, src: str, link: str) -> None:
        """⚡ One headline or filing that the AI judges a LARGE move with high confidence.
        Sent immediately, any time of day, to everyone who doesn't already hold the stock
        (holders get it as a portfolio alert)."""
        sym = i["symbol"]
        if i["magnitude"] != "large":
            return
        up = i["direction"] == "up"
        dc = self.day_change.get(sym)
        if dc is not None and (dc >= 3 if up else dc <= -3):
            return  # the market already reacted; not early any more
        day = now().strftime("%Y-%m-%d")
        for u in self.outsiders(sym):
            if i["confidence"] < u.cfg("breakconf"):
                continue
            if (self.store.has_seen(u.key("brk", day, sym)) or self.store.has_seen(u.key("opp", day, sym))
                    or self.store.has_seen(u.key("story", day, sym))):
                continue
            cap = u.cfg("maxbreak")
            if u.count("brk", day) >= cap:
                u.queue(sym, f"⚡ <b>{esc(sym)}</b> {'↑' if up else '↓'} {esc(i['why'])}", 80)
                continue
            self.store.first_time(u.key("brk", day, sym))
            n = u.count("brk", day)
            lines = [f"⚡ <b>BREAKING · {esc(sym)}</b> {'🟢 ↑' if up else '🔴 ↓'} large move likely "
                     f"({i['confidence']:.0%} confidence)",
                     f"🤖 {esc(i['why'])}",
                     f"📰 {esc(title[:300])}",
                     f"<i>{esc(src)} · {now().strftime('%H:%M:%S')}</i>"
                     + (f" · price today {dc:+.1f}%" if dc is not None else "")]
            if link:
                lines.append(f'<a href="{esc(link)}">Source</a>')
            lines.append(f"<i>Single source, not yet confirmed by other outlets. Breaking {n} of {cap} today. "
                         "📌 Tracked in 🏆 Track record.</i>")
            u.send("\n".join(lines), buttons=self.stock_buttons(sym))
            self.record_prediction(sym, "breaking", f"{i['confidence']:.0%}", 1 if up else -1, i["why"])

    def story_check(self, sym: str, title: str, src: str, ai_i, moved: bool) -> None:
        """📣 A stock is being covered by several outlets at once. Sent (with the AI's
        reading) to everyone who doesn't hold it, once per stock per day each."""
        people = [u for u in self.outsiders(sym) if not u.quiet()]
        if not people:
            return
        outlets = self.store.news_by_source(sym, config.STORY_WINDOW_H)
        day = now().strftime("%Y-%m-%d")
        people = [u for u in people if len(outlets) >= u.cfg("outlets")
                  and not self.store.has_seen(u.key("story", day, sym))
                  and not self.store.has_seen(u.key("opp", day, sym))
                  and not self.store.has_seen(u.key("brk", day, sym))
                  and not self.store.get(f"storyq:{day}:{u.chat}:{sym}")]
        if not people:
            return
        # One AI reading per stock per day, shared by everyone
        ck = (day, sym)
        if ck in self._story_ai:
            ai_i, ai_moved = self._story_ai[ck]
            moved = moved or ai_moved
        else:
            ai_moved = False
            if ai_i is None and self.ai.provider:  # the headline that tipped it over may not have had an AI read
                r = self.ai.read(title, src, sorted(self.portfolio()))
                ai_moved = bool(r and r["priced_in"])
                ai_i = next((i for i in (r or {}).get("impacts", []) if i["symbol"] == sym), None)
            self._story_ai[ck] = (ai_i, ai_moved)
            moved = moved or ai_moved
        if self.ai.provider and ai_i is None:
            return  # AI sees no price effect for this stock: stay quiet
        dc = self.day_change.get(sym)
        if ai_i and dc is not None and abs(dc) >= 7 and (dc > 0) == (ai_i["direction"] == "up"):
            moved = True
        for u in people:
            cap = u.cfg("maxstory")
            if u.count("story", day) >= cap:
                self.store.set(f"storyq:{day}:{u.chat}:{sym}", "1")
                u.queue(sym, f"📣 <b>{esc(sym)}</b> covered by {len(outlets)} outlets: {esc(title[:120])}", 60)
                continue
            self.store.first_time(u.key("story", day, sym))
            n = u.count("story", day)
            lines = [f"📣 <b>Story spreading · {esc(sym)}</b> · {len(outlets)} outlets in {config.STORY_WINDOW_H:g}h"]
            if ai_i:
                up = ai_i["direction"] == "up"
                lines.append(f"🤖 {'🟢 ↑' if up else '🔴 ↓'} {ai_i['magnitude']} impact likely "
                             f"({ai_i['confidence']:.0%}, {esc(ai_i['order'])}): {esc(ai_i['why'])}")
            if moved:
                lines.append("⏱️ The price has already moved a lot on this; you may be late.")
            elif dc is not None:
                lines.append(f"Price today so far: {dc:+.1f}%")
            lines += [f"• {esc(h[:140])} <i>({esc(o)})</i>" for o, h in outlets[:4]]
            lines.append(f"<i>Story alert {n} of {cap} today.</i>")
            u.send("\n".join(lines), buttons=self.stock_buttons(sym))
            if ai_i and not moved:
                self.record_prediction(sym, "story", ai_i["magnitude"], 1 if ai_i["direction"] == "up" else -1, ai_i["why"])

    # ================================================================== Telegram channels
    def channel_list(self) -> list:
        return [c for c in (self.store.get("channels") or "").split(",") if c]

    def poll_channels(self) -> None:
        """Public Telegram channels the owner added: posts mentioning someone's portfolio
        (directly, or per the AI's reading) are forwarded to those people."""
        self._ai_chan_cycle = 0
        for name in self.channel_list():
            try:
                posts = self.channels.fetch(name)
                self.ok("tg:" + name)
            except Exception as e:
                self.fail("tg:" + name, e)
                continue
            first = not self.store.get(f"chan_init:{name}")
            for p in posts:
                if self.store.first_time("tgpost:" + p["id"]) and not first:
                    self.handle_channel_post(name, p["text"], f"https://t.me/{p['id']}")
            self.store.set(f"chan_init:{name}", "1")

    def handle_channel_post(self, name: str, text: str, link: str) -> None:
        headline = text[:300]
        a = analyse_headline(headline, self.matcher, self.market_matcher)
        held = self.portfolio()
        syms = a["stocks"] + a["others"]
        mine = [s for s in syms if s in held]
        ai_by = {}
        # Channel posts can be chatty; only ask the AI when they could matter to someone's portfolio
        if self.ai.provider and held and self._ai_chan_cycle < 4 and (mine or self.POLICY.search(headline)):
            self._ai_chan_cycle += 1
            r = self.ai.read(headline, f"Telegram channel @{name}", sorted(held))
            if r and r["relevant"] and not r["priced_in"]:
                ai_by = {i["symbol"]: i for i in r["impacts"] if i["confidence"] >= 0.5 and i["symbol"] in held}
        c = classify(headline)
        t = a["tone"] if a["tone"] != "?" else c["tone"]
        for s in syms:  # kept for briefs/context, but channel posts never trigger market-wide alerts
            self.store.log_event(s, "tgnews", headline, t, c["impact"], f"@{name}", link)
        for s in mine + [s for s in ai_by if s not in mine]:
            i = ai_by.get(s)
            tone_s = ("+" if i["direction"] == "up" else "-") if i else t
            ai_line = (f"\n🤖 {'↑' if i['direction'] == 'up' else '↓'} {i['magnitude']} impact likely "
                       f"({i['confidence']:.0%}): {esc(i['why'])}") if i else ""
            urgent = bool(i and i["magnitude"] in ("medium", "large") and i["confidence"] >= 0.7)
            self.notify(s, f"📡 {TONE[tone_s]} <b>⭐ {esc(s)}</b> · from @{esc(name)}\n{esc(text[:500])}{ai_line}\n"
                           f'<a href="{esc(link)}">Open post</a>', urgent=urgent)

    def channels_view(self):
        chans = self.channel_list()
        lines = ["📡 <b>Telegram channels I read</b>",
                 "Posts that mention your stocks (or affect them, per the AI) are sent to you."]
        lines += [f"• @{esc(c)}" + (" ⚠️ not reachable" if self.failures.get("tg:" + c) else "") for c in chans] or ["• none yet"]
        rows = [[(f"❌ @{c}", f"rmch:{c}")] for c in chans] + [[("➕ Add channel", "prompt:channel")]]
        return "\n".join(lines), rows

    def add_channel(self, u, raw: str) -> None:
        name = clean_name(raw)
        if not name:
            return u.send("That doesn't look like a channel. Send its username, like <code>@channelname</code> "
                          "or a <code>t.me/channelname</code> link.")
        try:
            posts = self.channels.fetch(name)
        except Exception as e:
            return u.send(f"⚠️ Couldn't read @{esc(name)}: {esc(str(e)[:150])}\n"
                          "Only <b>public</b> channels can be read this way.")
        chans = self.channel_list()
        if name not in chans:
            chans.append(name)
            self.store.set("channels", ",".join(chans))
        for p in posts:  # don't re-send old posts
            self.store.first_time("tgpost:" + p["id"])
        self.store.set(f"chan_init:{name}", "1")
        u.send(f"✅ Now reading @{esc(name)} ({len(posts)} recent posts found). New posts about your "
               "family's stocks will be forwarded.", buttons=self.channels_view()[1])

    # ================================================================== track record
    # Rules, fixed in advance and shown to the user:
    #  * a prediction is written down the moment the bot makes it, with the price at that moment
    #  * it is judged at the close of the next trading session that starts after it was made
    #  * correct = moved >= 0.5% the predicted way; wrong = >= 0.5% the other way; flat = in between
    #  * every prediction counts; later news is noted but never excuses a miss
    FLAT_BAND = 0.5
    KIND_NAME = {"certainty": "✅ High certainty", "breaking": "⚡ Breaking", "opportunity": "🎯 Opportunity",
                 "story": "📣 Story"}

    @staticmethod
    def next_session(t: datetime) -> str:
        d = t.date()
        if not (is_weekday(t) and (t.hour, t.minute) < (9, 15)):
            d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d.isoformat()

    def nifty_now(self):
        lvl = self.nse.index_level.get("NIFTY 50")
        if not lvl or not market_open(now()):
            try:
                self.nse.index_stocks("NIFTY 50")
                lvl = self.nse.index_level.get("NIFTY 50")
            except Exception:
                return None
        return lvl[0] if lvl else None

    def price_now(self, sym: str):
        if market_open(now()) and sym in self.last_price:
            return self.last_price[sym]
        try:
            return float(self.nse.quote(sym)["lastPrice"])
        except Exception:
            return None

    def record_prediction(self, sym: str, kind: str, conviction: str, direction: int, reason: str) -> None:
        if not sym or direction not in (1, -1):
            return
        target = self.next_session(now())
        if not self.store.first_time(f"pred:{kind}:{sym}:{direction}:{target}"):
            return
        self.store.predict(sym, kind, conviction, direction, reason or "", self.price_now(sym), self.nifty_now(), target)

    def scan_certainty(self) -> None:
        """Every 15 minutes: whatever the ✅ High certainty list contains is recorded as a prediction,
        whether or not anyone tapped the button, so the track record isn't hand-picked."""
        for it in self.high_certainty(None):
            o = it["o"]
            d = o["drivers"].get(o["sign"])
            self.record_prediction(it["symbol"], "certainty", "very high" if it["very"] else "high", o["sign"],
                                   short(d, 150) if d else "")

    def evaluate_predictions(self) -> None:
        """After the close: judge every prediction whose target session was today."""
        t = now()
        today = t.date().isoformat()
        try:
            _rows, stamp = self.nse.index_stocks("NIFTY 50")
        except Exception as e:
            return self.fail("track", e)
        if t.strftime("%d-%b-%Y").lower() not in (stamp or "").lower():
            return  # no session today (holiday): targets roll to the next session
        nifty1 = (self.nse.index_level.get("NIFTY 50") or (None,))[0]
        quotes = 0
        for p in self.store.predictions("result IS NULL AND target_day <= ?", (today,)):
            if p["p0"] is None:
                self.store.prediction_set(p["id"], result="void", note="no price available when it was made",
                                          eval_ts=time.time())
                continue
            if quotes >= 80:
                break
            try:
                q = self.nse.quote(p["symbol"])
                quotes += 1
                time.sleep(0.4)
            except Exception:
                if p["target_day"] < (t.date() - timedelta(days=5)).isoformat():
                    self.store.prediction_set(p["id"], result="void", note="closing price unavailable",
                                              eval_ts=time.time())
                continue
            if t.strftime("%d-%b-%Y").lower() not in str(q.get("timestamp", "")).lower():
                continue  # stock didn't trade today (suspended?); try again next session
            p1 = float(q["lastPrice"])
            move = (p1 - p["p0"]) / p["p0"] * 100 * p["direction"]
            result = "correct" if move >= self.FLAT_BAND else "wrong" if move <= -self.FLAT_BAND else "flat"
            later = [e for e in self.store.events_for(p["symbol"], (time.time() - p["ts"]) / 3600)
                     if e["ts"] > p["ts"] and e["impact"] == HIGH
                     and {"+": 1, "-": -1}.get(e["tone"], 0) == -p["direction"]]
            note = "contrary news came later (still counted)" if later and result != "correct" else ""
            self.store.prediction_set(p["id"], p1=p1, nifty1=nifty1, result=result, note=note, eval_ts=time.time())

    def track_stats(self, rows: list) -> dict:
        ev = [p for p in rows if p["result"] in ("correct", "wrong", "flat")]
        n = len(ev)
        c = sum(p["result"] == "correct" for p in ev)
        w = sum(p["result"] == "wrong" for p in ev)
        moves, beat, nb = [], 0, 0
        for p in ev:
            r = (p["p1"] - p["p0"]) / p["p0"] * 100 * p["direction"]
            moves.append(r)
            if p["nifty0"] and p["nifty1"]:
                nr = (p["nifty1"] - p["nifty0"]) / p["nifty0"] * 100 * p["direction"]
                nb += 1
                beat += r > nr
        return {"n": n, "correct": c, "wrong": w, "flat": n - c - w,
                "avg": sum(moves) / n if n else 0.0, "beat": beat, "nb": nb}

    def track_view(self, mode: str = "summary"):
        allp = self.store.predictions()
        ok, chain = self.store.verify_predictions()
        if not allp:
            return ("🏆 <b>Track record</b>\nNo predictions recorded yet. Every ✅ High certainty, ⚡ Breaking, "
                    "🎯 Opportunity and 📣 Story call is written down when it's made and judged at the next "
                    "session's close.", [])
        # Overall: one call per stock, direction and session, so the same call made by several
        # alert types isn't counted more than once
        seen, unique = set(), []
        for p in allp:
            k = (p["symbol"], p["direction"], p["target_day"])
            if k not in seen:
                seen.add(k)
                unique.append(p)
        st = self.track_stats(unique)
        pending = sum(p["result"] is None for p in unique)
        void = sum(p["result"] == "void" for p in unique)
        since = datetime.fromtimestamp(allp[0]["ts"], IST).strftime("%d %b %Y")
        L = [f"🏆 <b>Track record</b> · every call since {since}"]
        if st["n"]:
            L.append(f"<b>{st['correct']} of {st['n']} correct ({st['correct'] / st['n']:.0%})</b> · "
                     f"❌ {st['wrong']} wrong · ➖ {st['flat']} flat")
            L.append(f"Average move in the predicted direction: {st['avg']:+.2f}%")
            if st["nb"]:
                L.append(f"Did better than the Nifty: {st['beat']} of {st['nb']} ({st['beat'] / st['nb']:.0%})")
        else:
            L.append("Nothing judged yet.")
        L.append(f"⏳ {pending} waiting for their closing price" + (f" · ⚪ {void} void (no price)" if void else ""))
        if mode == "summary":
            L.append("\n<b>By type</b>")
            groups = {}
            for p in allp:
                label = self.KIND_NAME.get(p["kind"], p["kind"]) + (
                    f" · {p['conviction']}" if p["kind"] == "certainty" else "")
                groups.setdefault(label, []).append(p)
            for label, rows in groups.items():
                g = self.track_stats(rows)
                if g["n"]:
                    L.append(f"{label}: {g['correct']}/{g['n']} ({g['correct'] / g['n']:.0%}) · avg {g['avg']:+.1f}%")
                else:
                    L.append(f"{label}: ⏳ {len(rows)} pending")
        show = [p for p in reversed(allp) if p["result"] in ("correct", "wrong", "flat")]
        if mode == "misses":
            show = [p for p in show if p["result"] != "correct"]
            L.append("\n<b>Every miss</b>")
        elif mode == "pending":
            show = [p for p in reversed(allp) if p["result"] is None]
            L.append("\n<b>Waiting to be judged</b>")
        else:
            L.append("\n<b>Latest results</b>")
        icon = {"correct": "✅", "wrong": "❌", "flat": "➖", None: "⏳"}
        for p in show[:15 if mode != "summary" else 8]:
            made = datetime.fromtimestamp(p["ts"], IST).strftime("%d %b %H:%M")
            arrow = "↑" if p["direction"] > 0 else "↓"
            line = f"{icon.get(p['result'], '⚪')} <b>{esc(p['symbol'])}</b> {arrow} · {made} · {self.KIND_NAME.get(p['kind'], p['kind'])}"
            if p["p1"] and p["p0"]:
                r = (p["p1"] - p["p0"]) / p["p0"] * 100
                line += f"\n   ₹{p['p0']:,.2f} → ₹{p['p1']:,.2f} ({r:+.2f}%)"
                if p["nifty0"] and p["nifty1"]:
                    line += f" · Nifty {(p['nifty1'] - p['nifty0']) / p['nifty0'] * 100:+.2f}%"
            else:
                line += f"\n   judged at the close on {p['target_day']}" + (f" · from ₹{p['p0']:,.2f}" if p["p0"] else "")
            if p["note"]:
                line += f"\n   <i>{esc(p['note'])}</i>"
            L.append(line)
        L.append(f"\n🔒 Record integrity: {'✅ all ' + str(chain) + ' entries untouched' if ok else '⚠️ the record was altered after entry ' + str(chain)}")
        L.append("<i>Rules: recorded when made · judged at the next session's close · correct = ≥0.5% the "
                 "predicted way, wrong = ≥0.5% the other way, flat in between · nothing is ever removed.</i>")
        rows = [[("❌ Misses", "track:misses"), ("⏳ Pending", "track:pending"), ("🏆 Summary", "track:summary")],
                [("📊 Signal stats (1h / 1 day)", "cmd:/learn")]]
        return L, rows

    # ================================================================== learning
    def poll_outcomes(self) -> None:
        """Record the price when a signal fired, 1 hour later and 1 trading day later."""
        now_ts = time.time()
        quotes = 0
        for oid, ts, sym, p0, p1h, p1d, t0 in self.store.outcomes_pending(40):
            if p0 and p1h and not p1d and now_ts - t0 < 18 * 3600:
                continue
            if p0 and not p1h and now_ts - t0 < 3600:
                continue
            price = self.last_price.get(sym)
            if price is None:
                if quotes >= 15:
                    continue
                try:
                    price = float(self.nse.quote(sym)["lastPrice"])
                    quotes += 1
                    time.sleep(0.8)
                except Exception:
                    continue
            if not p0:
                self.store.outcome_set(oid, "p0", price)
                self.store.outcome_set(oid, "t0", now_ts)
            elif not p1h:
                self.store.outcome_set(oid, "p1h", price)
            elif now_ts - t0 >= 18 * 3600:
                self.store.outcome_set(oid, "p1d", price)
        if now_ts - self._stats_at > 3600:
            self._stats_at = now_ts
            self.stats = {(r["kind"], r["label"]): r for r in self.store.outcome_stats()}

    def learned_adjust(self, events: list):
        """Nudge the score by how this kind of signal has actually played out so far."""
        best = None
        for e in events:
            key = (e["kind"], event_label(e))
            st = self.stats.get(key)
            if st and st["n1d"] >= 10:
                best = (key, st)
        if not best:
            return 0, None
        (kind, label), st = best
        hit = st["hit1d"] / st["n1d"]
        if hit >= 0.6:
            return 10, f"+10 '{label}' signals worked {hit:.0%} of the time so far (n={st['n1d']})"
        if hit < 0.45:
            return -10, f"-10 '{label}' signals worked only {hit:.0%} of the time so far (n={st['n1d']})"
        return 0, None

    # ================================================================== briefs & digests
    def last_close(self, t: datetime) -> datetime:
        d = t - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.replace(hour=15, minute=30, second=0, microsecond=0)

    def top_candidates(self, since_ts: float, limit: int = 8, user: User = None) -> list:
        """Stocks outside a person's portfolio ranked by evidence score."""
        user = user or self.owner
        mine, muted = user.portfolio(), user.muted()
        syms = {e["symbol"] for e in self.store.events_since(since_ts)
                if e["symbol"] and e["symbol"] not in mine and e["symbol"] not in muted}
        floor = user.cfg("digestscore")
        scored = []
        for s in syms:
            sc = score_events(self.store.events_for(s, 24), self.day_change.get(s))
            if sc["score"] >= floor:
                scored.append((sc["score"], s, sc))
        return sorted(scored, key=lambda x: (-x[0], x[1]))[:limit]

    def brief(self, since: datetime, title: str, user: User = None) -> list:
        user = user or self.owner
        ev = self.store.events_since(since.timestamp())
        mine = user.portfolio()
        out = [f"🗞️ <b>{title}</b> · since {since.strftime('%a %H:%M')}"]

        glob = [e for e in ev if e["kind"] == "global"][-5:]
        out.append("\n<b>Global cues</b>")
        out += [f"• {TONE.get(e['tone'], '🟡')} {esc(e['headline'][:150])}" for e in glob] or ["• nothing notable"]

        out.append("\n<b>Your portfolio</b>")
        if not mine:
            out.append("• empty. Tap ⭐ Portfolio → ➕ Add stocks.")
        per = {}
        for e in ev:
            if e["symbol"] in mine:
                per.setdefault(e["symbol"], []).append(e)
        looks = {s: self.outlook_of(s) for s in per}
        for s in sorted(per, key=lambda k: -abs(looks[k]["net"])):
            items, o = per[s], looks[s]
            out.append(f"<b>{esc(s)}</b> · {o['label'] or '🟡 Neutral'} ({o['pos']}+ / {o['neg']}−)")
            for e in items[-2:]:
                out.append(f"   {esc(e['headline'][:150])}")
        if mine and not per:
            out.append("• nothing new on your stocks")

        out.append("\n<b>Top candidates outside your portfolio</b>")
        cands = self.top_candidates(since.timestamp(), user=user)
        for sc, s, d in cands:
            last = self.store.events_for(s, 24)[-1]
            out.append(f"{'🟢' if d['direction'] > 0 else '🔴' if d['direction'] < 0 else '🟡'} <b>{esc(s)}</b> "
                       f"{sc:.0f}/100 · {esc(last['headline'][:120])}")
        if not cands:
            out.append("• none strong enough")

        themes = {}
        for e in ev:
            if e["kind"].startswith("theme:"):
                themes.setdefault(e["kind"][6:], []).append(e["headline"])
        out.append("\n<b>Sectors</b>")
        out += [f"• <b>{esc(k)}</b>: {esc(v[-1][:130])}" + (f" (+{len(v) - 1})" if len(v) > 1 else "")
                for k, v in sorted(themes.items(), key=lambda kv: -len(kv[1]))[:5]] or ["• quiet"]
        return out

    def send_digest(self, title: str = "Digest", user: User = None) -> None:
        for u in ([user] if user else self.users()):
            rows = self.store.take_queue(u.chat)
            if not rows:
                continue
            mine = u.portfolio()
            port = [r for r in rows if r[1] in mine]
            cands = [r for r in rows if r[1] and r[1] not in mine]
            rest = [r for r in rows if not r[1]]
            out = [f"📋 <b>{title}</b> · {len(rows)} items held back to avoid pinging you"]
            if port:
                out += ["\n<b>Your portfolio</b>"] + [r[2] for r in port[:15]]
            if cands:
                out += ["\n<b>Candidates (not strong enough to ping)</b>"] + [r[2] for r in cands[:8]]
            if rest:
                out += ["\n<b>Sector news</b>"] + [r[2].split("\n<i>")[0] for r in rest[:5]]
            u.send_long(out)

    def morning_digest(self) -> None:
        t = now()
        try:
            rows = self.nse.board_meetings(t.date(), (t + timedelta(days=14)).date())
        except Exception as e:
            self.fail("digest", e)
            rows = None
        for u in self.users():
            self.store.take_queue(u.chat)  # the brief covers everything held back overnight
            u.send_long(self.brief(self.last_close(t), "Pre-market brief", user=u))
            if rows is None:
                continue
            mine = u.portfolio()
            results, corp = [], []
            for r in rows:
                sym = (r.get("bm_symbol") or r.get("symbol") or "").upper()
                purpose = f"{r.get('bm_purpose', '')} {r.get('bm_desc', '')}"
                d = r.get("bm_date", "")
                m = re.search(r"bonus|split|sub-?division|buy-?back", purpose, re.I)
                if sym in mine and re.search(r"result", purpose, re.I):
                    results.append((d, sym))
                elif m and (sym in mine or sym in self.universe):
                    corp.append((d, sym, m.group(0).lower(), sym in mine))
                elif sym in mine and re.search(r"fund|qip|preferential", purpose, re.I):
                    corp.append((d, sym, "fund raising", True))
            results.sort(); corp.sort()
            text = ["📅 <b>Catalysts ahead</b>", "<b>Your results (next 14 days)</b>"]
            text += [f"• {esc(d)} — <b>{esc(s)}</b>" for d, s in results[:20]] or ["• none"]
            text.append("<b>Bonus / split / buyback / fund raising</b>")
            text += [f"• {esc(d)} — {'⭐ ' if m else ''}<b>{esc(s)}</b> {esc(w)}" for d, s, w, m in corp[:12]] or ["• none"]
            u.send_long(text)

    # ================================================================== settings & updates
    def set_setting(self, name: str, value: str, chat: str = None) -> str:
        chat = str(chat or config.TELEGRAM_CHAT_ID)
        name = name.lower()
        if name == "quiet":
            v = value.lower()
            if v != "off" and not re.fullmatch(r"\d{1,2}-\d{1,2}", v):
                return "Use quiet 23-7 or quiet off"
            self.store.set(f"cfg:{chat}:quiet", v)
            return f"Quiet hours: {v}"
        if name not in SETTINGS:
            return "Unknown setting. Open ⚙️ Settings to see the list."
        attr, typ, desc = SETTINGS[name]
        try:
            val = typ(float(value))
        except ValueError:
            return f"{name} needs a number."
        self.store.set(f"cfg:{chat}:{name}", val)
        return f"✅ {name} = {val:g} ({desc})"

    def self_update(self) -> None:
        """Pull the latest code from GitHub and restart (the service manager starts it again)."""
        import subprocess, sys
        root = str(config.ROOT)
        o = self.owner
        if not (config.ROOT / ".git").exists():
            o.send("Updates aren't connected yet. Run the one-time GitHub setup from the README.")
            return
        def git(*a):
            return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, timeout=120)
        before = git("rev-parse", "HEAD").stdout.strip()
        r = git("fetch", "origin", "main")
        if r.returncode != 0:
            o.send(f"⚠️ Update failed:\n<code>{esc((r.stdout + r.stderr).strip()[-600:])}</code>")
            return
        after = git("rev-parse", "origin/main").stdout.strip()
        if after == before:
            o.send("Already on the latest version.")
            return
        git("reset", "--hard", "origin/main")  # .env, portfolios and history are untracked, so they're kept
        changes = git("log", "--format=• %s", f"{before}..{after}").stdout.strip()[:800]
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", f"{root}/requirements.txt"],
                       capture_output=True, timeout=300)
        o.send("⬇️ Updated. Restarting in a few seconds…\n" + esc(changes))
        sys.exit(0)  # systemd (or run.sh on Termux) restarts the engine with the new code

    # ================================================================== buttons & menu
    MENU = [("menu", "Show the button panel"), ("sure", "Only the stocks where the evidence clearly points one way"),
            ("new", "High-certainty stocks you don't own yet"),
            ("portfolio", "Your stocks with their overall verdict"),
            ("track", "How many predictions were right and wrong, honestly"),
            ("top", "Strongest candidates right now"), ("brief", "News summary (pick hours)"),
            ("digest", "Get held-back items now"), ("ask", "Ask the AI about any headline"),
            ("settings", "Change your limits with ➕ / ➖ buttons"), ("learn", "How past signals played out"),
            ("status", "Health check")]
    PANEL = [["✅ High certainty", "🔍 Sure, not owned", "⭐ Portfolio"], ["🗞️ Brief", "🤖 Ask AI", "📥 Digest"],
             ["⚙️ Settings", "🏆 Track record", "☰ More"]]
    PANEL_MAP = {"🏆 Track record": "/track", "✅ High certainty": "/sure", "🔍 Sure, not owned": "/new", "🎯 Top": "/top", "🗞️ Brief": "/brief", "⭐ Portfolio": "/portfolio", "🤖 Ask AI": "/ask",
                 "⚙️ Settings": "/settings", "📥 Digest": "/digest", "📊 Learn": "/learn",
                 "🩺 Status": "/status", "☰ More": "/more"}
    STEPS = {  # setting -> (button label, step, min, max)
        "maxopp": ("🎯 Opportunities/day", 1, 0, 50), "score": ("🎯 Min evidence score", 5, 30, 95),
        "digestscore": ("📋 Digest min score", 5, 0, 90), "cooldown": ("⏱ Cooldown min", 15, 0, 240),
        "gap": ("🌅 Pre-open gap %", 0.5, 0.5, 10),
        "outlets": ("📣 Outlets for story", 1, 1, 6), "maxstory": ("📣 Stories/day", 1, 0, 50),
        "breakconf": ("⚡ Breaking confidence", 0.05, 0.5, 0.95), "maxbreak": ("⚡ Breaking/day", 1, 0, 50)}
    OWNER_ONLY = {"/update", "/setkey", "/users", "/channels", "/addchannel"}

    def stock_buttons(self, sym: str):
        return [[(f"➕ Add {sym} to portfolio", f"add:{sym}"), (f"🔇 Mute {sym}", f"mute:{sym}")]]

    def settings_view(self, u: User):
        rows = []
        for name, (label, *_r) in self.STEPS.items():
            rows.append([("➖", f"set:{name}:-"), (f"{label}: {u.cfg(name):g}", "noop"), ("➕", f"set:{name}:+")])
        s, e = u.quiet_range()
        quiet = "off" if s == e else f"{s}:00–{e}:00"
        rows.append([(f"🌙 Quiet hours: {quiet} (tap to switch)", "quiet:toggle")])
        return "⚙️ <b>Your settings</b>\nTap ➕ / ➖ to change. Applies immediately, only to you.", rows

    # ================================================================== stock lookup
    def resolve_stock(self, text: str) -> list:
        """'tcs', 'Tata Consultancy', 'indigo', 'LIC' -> candidate NSE symbols (best first)."""
        q = re.sub(r"[^a-z0-9& ]", " ", text.lower()).strip()
        q = re.sub(r"\s+", " ", q)
        if not q or len(q) < 2:
            return []
        up = q.upper().replace(" ", "")
        if up in self.names or up in self.portfolio():
            return [up]
        for sym, al in ALIASES.items():
            if q in al:
                return [sym]
        starts, contains = [], []
        for sym, name in self.names.items():
            n = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9& ]", " ", (name or "").lower())).strip()
            if not n:
                continue
            if n.startswith(q) or sym.lower().startswith(q.replace(" ", "")):
                starts.append(sym)
            elif len(q) >= 4 and f" {q}" in f" {n}":
                contains.append(sym)
        starts.sort(key=lambda s: (len(self.names.get(s, "")), s))
        return (starts + sorted(contains))[:8]

    # --- price, sector and peers -------------------------------------------------
    PEER_INDEX = "NIFTY TOTAL MARKET"   # ~750 stocks with NSE's industry tag: big enough to find peers

    def market_rows(self) -> dict:
        """symbol -> NSE quote row (with meta.industry, ffmc, 30-day and 1-year change), refreshed every 15 min."""
        if self._rows and time.time() - self._rows_at < 900:
            return self._rows
        if time.time() - self._rows_fail_at < 600:
            return self._rows
        try:
            rows, _ = self.nse.index_stocks(self.PEER_INDEX)
            if rows:
                self._rows, self._rows_at = {r["symbol"]: r for r in rows}, time.time()
        except Exception as e:
            log.warning("peer rows: %s", e)
            self._rows_fail_at = time.time()
        return self._rows

    @staticmethod
    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def price_info(self, sym: str) -> dict:
        """{'price','today','1w','1m','1y','high52','low52','pe','sector_pe','sector','industry'}:
        NSE first, Yahoo Finance for whatever NSE couldn't give (NSE often blocks cloud servers)."""
        out = {}
        row = self.market_rows().get(sym)
        if row:
            out.update(price=self._f(row.get("lastPrice")), today=self._f(row.get("pChange")),
                       **{"1m": self._f(row.get("perChange30d")), "1y": self._f(row.get("perChange365d"))},
                       high52=self._f(row.get("yearHigh")), low52=self._f(row.get("yearLow")),
                       industry=(row.get("meta") or {}).get("industry", ""))
        try:
            q = self.nse.quote(sym)
            for k, v in (("price", q.get("lastPrice")), ("today", q.get("pChange")), ("high52", q.get("yearHigh")),
                         ("low52", q.get("yearLow")), ("pe", q.get("pe")), ("sector_pe", q.get("sector_pe"))):
                if out.get(k) is None and self._f(v) is not None:
                    out[k] = self._f(v)
            for k in ("sector", "industry"):
                if not out.get(k) and q.get(k):
                    out[k] = q[k]
        except Exception as e:
            log.info("nse quote %s: %s", sym, e)
        try:
            r = returns(self.yahoo.history(sym))
            for k in ("today", "1w", "1m", "1y", "high52", "low52"):
                if out.get(k) is None and r.get(k) is not None:
                    out[k] = r[k]
            if out.get("price") is None:
                out["price"] = self.yahoo.history(sym)["price"]
        except Exception as e:
            log.info("yahoo history %s: %s", sym, e)
        if not out.get("sector"):
            try:
                pr = self.yahoo.profile(sym)
                out["sector"] = pr.get("sector", "")
                out.setdefault("industry", pr.get("industry", ""))
                if not out.get("industry"):
                    out["industry"] = pr.get("industry", "")
            except Exception as e:
                log.info("yahoo profile %s: %s", sym, e)
        if out.get("today") is None and sym in self.day_change:
            out["today"], out["price"] = self.day_change[sym], out.get("price") or self.last_price.get(sym)
        return out

    def nifty_today(self):
        for name in ("NIFTY 50", self.PEER_INDEX, "NIFTY MIDCAP 100"):
            ic = getattr(self.nse, "index_change", {})
            if name in ic:
                return ic[name]
        try:
            return returns(self.yahoo.history("NIFTY", "^NSEI")).get("today")
        except Exception:
            return None

    def peer_info(self, sym: str, info: dict) -> dict:
        """Same-industry companies and where this stock ranks among them.
        {'source','peers':[{symbol,name,size,today,1m,1y}], 'rank':{metric:(pos,total,value,avg)}}"""
        rows = self.market_rows()
        ind = (info.get("industry") or "").strip().lower()
        peers, source = [], ""
        if rows and ind:
            for s, r in rows.items():
                if ((r.get("meta") or {}).get("industry") or "").strip().lower() == ind:
                    peers.append({"symbol": s, "name": (r.get("meta") or {}).get("companyName") or self.names.get(s, ""),
                                  "size": self._f(r.get("ffmc")), "today": self._f(r.get("pChange")),
                                  "1m": self._f(r.get("perChange30d")), "1y": self._f(r.get("perChange365d"))})
            source = "NSE"
        if len(peers) < 2:  # NSE unavailable or industry not matched: Yahoo's list of comparable companies
            peers, source = [], ""
            try:
                for s in [sym] + [x for x in self.yahoo.similar(sym) if x != sym][:6]:
                    r = returns(self.yahoo.history(s))
                    peers.append({"symbol": s, "name": self.names.get(s, ""), "size": None,
                                  "today": r.get("today"), "1m": r.get("1m"), "1y": r.get("1y")})
                source = "Yahoo"
            except Exception as e:
                log.info("yahoo peers %s: %s", sym, e)
        if not any(p["symbol"] == sym for p in peers):
            if not peers:
                return {"source": "", "peers": [], "rank": {}}
            peers.append({"symbol": sym, "name": self.names.get(sym, ""), "size": None,
                          "today": info.get("today"), "1m": info.get("1m"), "1y": info.get("1y")})
        rank = {}
        for m in ("size", "today", "1m", "1y"):
            have = [p for p in peers if p[m] is not None]
            me = next((p for p in have if p["symbol"] == sym), None)
            if len(have) < 2 or not me:
                continue
            have.sort(key=lambda p: -p[m])
            others = [p[m] for p in have if p["symbol"] != sym]
            rank[m] = (have.index(me) + 1, len(have), me[m], sum(others) / len(others))
        peers.sort(key=lambda p: -(p["size"] or 0))
        return {"source": source, "peers": peers, "rank": rank}

    def stock_events(self, sym: str, hours: float = 168) -> list:
        """Stored events + fresh Google News headlines (cached 20 min), stale/old-period items dropped."""
        company = self.names.get(sym, "") or sym
        stored = [e for e in self.store.events_for(sym, hours) if not is_stale(e["headline"], e["ts"])]
        cached = self._news_cache.get(sym)
        if cached and time.time() - cached[0] < 1200:
            fetched = cached[1]
        else:
            fetched = []
            try:
                fetched = search_company_news(self.news.s, company, sym, days=7,
                                              aliases=[a for a in ALIASES.get(sym, ()) if len(a) > 3][:2])
            except Exception as e:
                log.warning("news search %s: %s", sym, e)
            self._news_cache[sym] = (time.time(), fetched)
        known = {e["headline"][:80].lower() for e in stored}
        events = list(stored)
        for f in fetched:
            if f["title"][:80].lower() in known or not f["ts"] or time.time() - f["ts"] > hours * 3600:
                continue
            c = classify(f["title"])
            t = tone(f["title"])
            t = t if t != "?" else c["tone"]
            events.append({"ts": f["ts"], "symbol": sym, "kind": "news", "headline": f["title"], "tone": t,
                           "impact": c["impact"] if c["impact"] != "LOW" or t == "?" else MEDIUM,
                           "source": f["source"], "url": f["link"]})
            known.add(f["title"][:80].lower())
        events.sort(key=lambda e: e["ts"])
        return events

    def stock_drivers(self, sym: str, industry: str = "") -> list:
        """Search queries for news that moves this stock without naming it (taxes, regulation,
        commodities, sector events). Written by the AI once a day per stock; themes as a fallback."""
        import json
        raw = self.store.get(f"drivers:{sym}")
        if raw:
            try:
                d = json.loads(raw)
                if time.time() - d["t"] < 86400 and d["q"]:
                    return d["q"]
            except (ValueError, KeyError, TypeError):
                pass
        qs = []
        if self.ai.provider:
            qs = self.ai.drivers(sym, self.names.get(sym, ""), industry)
            if qs:
                self.store.set(f"drivers:{sym}", json.dumps({"t": time.time(), "q": qs}))
        if not qs:
            qs = [THEME_QUERIES[name] for name, (_rx, stocks) in THEMES.items() if sym in stocks and name in THEME_QUERIES]
            if industry:
                qs.append(f"{industry} sector India")
        return qs[:6]

    def sector_events(self, sym: str, industry: str = "", hours: float = 72) -> list:
        """Indirect news: policy/tax/sector/commodity headlines that can move this stock without naming it.
        From the engine's own sector log plus a fresh Google News search on the stock's drivers."""
        out, seen = [], set()
        mine = {f"theme:{name}" for name, (_rx, stocks) in THEMES.items() if sym in stocks}
        for e in self.store.events_since(time.time() - hours * 3600):
            if e["kind"] in mine and not is_stale(e["headline"], e["ts"]):
                k = re.sub(r"\W+", "", e["headline"].lower())[:70]
                if k not in seen:
                    seen.add(k)
                    out.append({**e, "symbol": sym, "kind": "sector"})
        cached = self._sector_cache.get(sym)
        if cached and time.time() - cached[0] < 1200:
            fetched = cached[1]
        else:
            fetched = []
            for q in self.stock_drivers(sym, industry):
                try:
                    fetched += search_topic(self.news.s, q, days=max(1, int(hours // 24)), limit=5)
                except Exception as e:
                    log.info("topic search %r: %s", q, e)
            self._sector_cache[sym] = (time.time(), fetched)
        for f in fetched:
            k = re.sub(r"\W+", "", f["title"].lower())[:70]
            if k in seen or time.time() - f["ts"] > hours * 3600:
                continue
            seen.add(k)
            out.append({"ts": f["ts"], "symbol": sym, "kind": "sector", "headline": f["title"], "tone": "?",
                        "impact": MEDIUM, "source": f["source"], "url": f["link"]})
        out.sort(key=lambda e: -e["ts"])
        return out[:20]

    def stock_context(self, sym: str) -> dict:
        cached = self._ctx_cache.get(sym)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        info = self.price_info(sym)
        ctx = {"sym": sym, "company": self.names.get(sym, "") or sym, "info": info,
               "nifty": self.nifty_today(), "peers": self.peer_info(sym, info), "events": self.stock_events(sym),
               "sector": self.sector_events(sym, info.get("industry", ""))}
        self._ctx_cache[sym] = (time.time(), ctx)
        return ctx

    @staticmethod
    def _pct(x, digits=1):
        return "n/a" if x is None else f"{x:+.{digits}f}%"

    def news_items(self, ctx: dict, hours: float = 168) -> list:
        """The numbered list the AI reads: (tag, event). [C] = about the company, [S] = sector/policy/macro."""
        t0 = time.time() - hours * 3600
        comp = [("S" if e["kind"] == "ai" else "C", e) for e in reversed(ctx["events"]) if e["ts"] >= t0][:14]
        sect = [("S", e) for e in ctx.get("sector", []) if e["ts"] >= min(t0, time.time() - 72 * 3600)][:12]
        return comp + sect

    def facts_text(self, ctx: dict, news_hours: float = 168) -> str:
        """Plain-text facts handed to the AI (it is told to use nothing else)."""
        i, pr = ctx["info"], ctx["peers"]
        f = []
        if i.get("price"):
            f.append(f"Price: Rs {i['price']:,.2f}. Change today: {self._pct(i.get('today'), 2)}. "
                     f"Nifty 50 today: {self._pct(ctx['nifty'], 2)}.")
        f.append(f"Returns: 1 week {self._pct(i.get('1w'))}, 1 month {self._pct(i.get('1m'))}, 1 year {self._pct(i.get('1y'))}."
                 + (f" 52-week range Rs {i['low52']:,.0f} to Rs {i['high52']:,.0f}." if i.get("low52") and i.get("high52") else ""))
        if i.get("sector") or i.get("industry"):
            f.append(f"Sector: {i.get('sector') or 'n/a'}; industry: {i.get('industry') or 'n/a'}.")
        if i.get("pe") and i.get("sector_pe"):
            f.append(f"P/E {i['pe']:.1f} vs sector P/E {i['sector_pe']:.1f}.")
        names = {"size": "size (free-float market cap)", "today": "today's move", "1m": "1-month return", "1y": "1-year return"}
        for m, (pos, tot, val, avg) in pr["rank"].items():
            f.append(f"Rank among {tot} industry peers by {names[m]}: #{pos}"
                     + ("" if m == "size" else f" ({val:+.1f}% vs peer average {avg:+.1f}%)") + ".")
        if pr["peers"]:
            f.append("Peers: " + "; ".join(
                f"{p['symbol']} today {self._pct(p['today'])}, 1m {self._pct(p['1m'])}"
                for p in pr["peers"][:8] if p["symbol"] != ctx["sym"]) + ".")
        items = self.news_items(ctx, news_hours)
        if items:
            f.append("News (numbered; [C] = about the company, [S] = sector/policy/macro that may affect it):")
            for n, (tag, e) in enumerate(items, 1):
                when = datetime.fromtimestamp(e["ts"], IST).strftime("%d %b %Y %H:%M")
                f.append(f"{n}. [{tag}] [{when}] {short(e, 200)} ({e['source'] or e['kind']})")
        else:
            f.append("News: no news items found for this stock or its sector in this period.")
        return "\n".join(f)

    def peer_lines(self, ctx: dict) -> list:
        i, pr = ctx["info"], ctx["peers"]
        out = []
        sec = " · ".join(x for x in (i.get("sector"), i.get("industry")) if x)
        out.append(f"🏭 <b>Sector:</b> {esc(sec) if sec else 'not available'}")
        if pr["rank"]:
            label = {"size": "Size", "today": "Today", "1m": "1 month", "1y": "1 year"}
            tot = max(t for _p, t, _v, _a in pr["rank"].values())
            out.append(f"🏁 <b>Rank vs {tot - 1} peers</b> (1 = best)")
            for m, (pos, n, val, avg) in pr["rank"].items():
                medal = "🥇" if pos == 1 else ("🔻" if pos == n else "▫️")
                out.append(f"   {medal} {label[m]}: <b>#{pos} of {n}</b>"
                           + ("" if m == "size" else f" · {val:+.1f}% vs peers {avg:+.1f}%"))
            if i.get("pe") and i.get("sector_pe"):
                cheap = "cheaper" if i["pe"] < i["sector_pe"] else "pricier"
                out.append(f"   ▫️ P/E {i['pe']:.1f} vs sector {i['sector_pe']:.1f} ({cheap} than sector)")
            top = [p for p in pr["peers"] if p["symbol"] != ctx["sym"]][:5]
            if top:
                out.append("   Peers: " + " · ".join(f"{p['symbol']} {self._pct(p['today'])}" for p in top))
        else:
            out.append("🏁 Peer ranking not available right now (market data sources unreachable).")
        return out

    def stock_report(self, u: User, sym: str):
        """Everything about one stock: price and returns, sector and rank vs peers, the net news verdict
        (positives AND negatives), an AI analysis (always shown), and the latest news."""
        ctx = self.stock_context(sym)
        info, events, company = ctx["info"], ctx["events"], ctx["company"]
        cached = self._report_cache.get(sym)
        if cached and time.time() - cached[0] < 1200:
            analysis = cached[1]
        else:
            analysis = self.ai.analyse(sym, company, self.facts_text(ctx)) if self.ai.provider else None
            if analysis:
                self._report_cache[sym] = (time.time(), analysis)

        star = "⭐ " if sym in u.portfolio() else ""
        lines = [f"🔎 {star}<b>{esc(sym)}</b> · {esc(company)}"]
        if info.get("price"):
            lines.append(f"💰 ₹{info['price']:,.2f} · today <b>{self._pct(info.get('today'), 2)}</b>"
                         + (f" (Nifty {self._pct(ctx['nifty'], 2)})" if ctx["nifty"] is not None else ""))
            rets = [f"{k} {self._pct(info.get(k))}" for k in ("1w", "1m", "1y") if info.get(k) is not None]
            if rets:
                lines.append("   " + " · ".join(rets)
                             + (f" · 52w ₹{info['low52']:,.0f}–{info['high52']:,.0f}" if info.get("low52") and info.get("high52") else ""))
        else:
            lines.append("💰 Price not available right now.")
        lines += self.peer_lines(ctx)

        items = self.news_items(ctx)
        explained = []   # (tag, event, AI's reading) for the items the AI says matter for this stock
        if analysis:
            for n in analysis.get("news", []):
                if 1 <= n["ref"] <= len(items):
                    tag, e = items[n["ref"] - 1]
                    explained.append((tag, e, n))
        if explained:
            judged = [{"ts": e["ts"], "kind": "ainews", "headline": n["what"], "source": e["source"],
                       "tone": {"up": "+", "down": "-"}.get(n["direction"], "?"),
                       "impact": {3: "HIGH", 2: "MEDIUM"}.get(n["importance"], "LOW")} for _t, e, n in explained]
        else:   # no AI: keyword tones of news that names the company (sector items can't be judged by keywords)
            judged = events
        o = outlook([e for e in judged if time.time() - e["ts"] <= 24 * 3600])
        o7 = outlook(judged)
        lines.append("")
        if o["label"]:
            lines.append(f"📊 <b>News verdict (24h): {o['label']}</b> · {o['pos']} positive vs {o['neg']} negative")
        if o7["label"] and o7["label"] != o["label"]:
            lines.append(f"📊 News verdict (7 days): {o7['label']} · {o7['pos']} positive vs {o7['neg']} negative")
        if not o["label"] and not o7["label"]:
            lines.append("📊 No price-relevant news in the last 7 days.")

        if analysis:
            arrow = {"up": "🟢 ↑ leans positive", "down": "🔴 ↓ leans negative",
                     "mixed": "🟡 mixed"}.get(analysis["direction"], "⚪ no clear direction")
            lines.append(f"\n🤖 <b>AI analysis:</b> {arrow} ({analysis['confidence']:.0%} confident)")
            if analysis["summary"]:
                lines.append(f"   {esc(analysis['summary'])}")
            if analysis["vs_peers"]:
                lines.append(f"   🏁 {esc(analysis['vs_peers'])}")
            for p in analysis["positives"][:3]:
                lines.append(f"   ➕ {esc(p)}")
            for n in analysis["negatives"][:3]:
                lines.append(f"   ➖ {esc(n)}")
        elif not self.ai.provider:
            lines.append("\n🤖 AI analysis is off: the owner can add a free AI key (☰ More → 🔑 AI key).")
        else:
            lines.append(f"\n🤖 AI analysis unavailable right now ({'daily limit reached' if not self.ai.budget_left() else 'AI call failed'}). Tap 🔄 Refresh to retry.")

        if explained:
            lines.append("\n📰 <b>What's happening</b>")
            for tag, e, n in explained:
                dot = {"up": "🟢", "down": "🔴"}.get(n["direction"], "⚪")
                when = datetime.fromtimestamp(e["ts"], IST).strftime("%d %b")
                kind = "🏭 Sector/policy · " if tag == "S" else ""
                lines.append(f"{dot} {'🔥 ' if n['importance'] == 3 else ''}{kind}<b>{when}</b> · {esc(n['what'])}")
                if n["effect"]:
                    lines.append(f"      ↳ <i>For {esc(sym)}:</i> {esc(n['effect'])} <i>({esc(e['source'] or e['kind'])})</i>")
        elif analysis is not None and items:
            lines.append("\n📰 None of this week's news looks price-relevant for this stock.")
        elif items:
            lines.append("\n📰 <b>Latest headlines</b> <i>(AI off, so they aren't explained)</i>")
            for tag, e in items[:8]:
                when = datetime.fromtimestamp(e["ts"], IST).strftime("%d %b")
                lines.append(f"{'🏭' if tag == 'S' else TONE.get(e['tone'], '🟡')} {when} · {esc(short(e, 140))}")
        else:
            lines.append("\n📰 No news found about this stock or its sector in the last 7 days.")
        lines.append("<i>Verdict weighs every item by importance and recency. Not a guarantee.</i>")
        held = sym in u.portfolio()
        buttons = [[("❌ Remove from portfolio" if held else f"➕ Add {sym} to portfolio", f"rmq:{sym}" if held else f"add:{sym}"),
                    ("🔄 Refresh", f"stockr:{sym}")],
                   [("📉 Why is it falling?", f"move:{sym}:down"), ("📈 Why is it rising?", f"move:{sym}:up")],
                   [("📎 Sources", f"src:{sym}")]]
        return lines, buttons

    def sources_view(self, sym: str) -> list:
        ctx = self.stock_context(sym)
        lines = [f"📎 <b>Sources for {esc(sym)}</b> (newest first)"]
        for tag, e in self.news_items(ctx)[:20]:
            when = datetime.fromtimestamp(e["ts"], IST).strftime("%d %b %H:%M")
            link = f' <a href="{esc(e["url"])}">open</a>' if e.get("url") else ""
            lines.append(f"{'🏭' if tag == 'S' else '🏢'} {esc(short(e, 120))} <i>({esc(e['source'] or e['kind'])}, {when})</i>{link}")
        return lines

    def send_stock(self, u: User, sym: str, refresh: bool = False) -> None:
        if refresh:
            for c in (self._report_cache, self._ctx_cache, self._news_cache, self._sector_cache):
                c.pop(sym, None)
        self.tg.typing(u.chat)
        lines, buttons = self.stock_report(u, sym)
        self._send_lines(u, lines, buttons)

    def _send_lines(self, u: User, lines: list, buttons) -> None:
        text = "\n".join(lines)
        if len(text) > 3900:
            u.send_long(lines)
            u.send("⬆️", buttons=buttons)
        else:
            u.send(text, buttons=buttons)

    # --- "<stock> fall" / "<stock> rise": why did it move? -------------------------
    DOWN_WORDS = {"fall", "falls", "fell", "falling", "fallen", "down", "drop", "drops", "dropped", "dropping",
                  "crash", "crashed", "crashing", "decline", "declined", "declining", "red", "sink", "sinking", "sank",
                  "slump", "slumped", "tank", "tanked", "tanking", "plunge", "plunged", "dip", "dipped", "falling?"}
    UP_WORDS = {"rise", "rises", "rose", "rising", "risen", "up", "jump", "jumps", "jumped", "jumping", "rally",
                "rallied", "rallying", "gain", "gained", "gaining", "surge", "surged", "surging", "green", "soar",
                "soared", "soaring", "spike", "spiked", "climb", "climbed", "climbing", "zoom", "zoomed"}
    FILLER = {"why", "is", "did", "does", "has", "have", "today", "so", "much", "the", "stock", "share", "shares",
              "price", "what", "reason", "for", "behind", "it", "its", "was", "are", "going", "gone", "this", "week",
              "explain", "tell", "me", "about", "in", "of", "a"}

    def parse_move(self, text: str):
        """'indigo fall', 'why did tcs rise today', 'HAL up' -> ('indigo', 'down') etc., else None."""
        words = re.sub(r"[^a-z0-9& ]", " ", text.lower()).split()
        if not 2 <= len(words) <= 8:
            return None
        dirs = {"down" for w in words if w in self.DOWN_WORDS} | {"up" for w in words if w in self.UP_WORDS}
        if len(dirs) != 1:
            return None
        rest = [w for w in words if w not in self.DOWN_WORDS | self.UP_WORDS | self.FILLER]
        if not rest or len(rest) > 4:
            return None
        return " ".join(rest), dirs.pop()

    def explain_move(self, u: User, sym: str, direction: str) -> None:
        self.tg.typing(u.chat)
        ctx = self.stock_context(sym)
        info, company = ctx["info"], ctx["company"]
        pr = ctx["peers"]
        word = "fall" if direction == "down" else "rise"
        icon = "📉" if direction == "down" else "📈"
        lines = [f"{icon} <b>Why did {esc(sym)} {word}?</b> · {esc(company)}"]
        facts = [f"Today {self._pct(info.get('today'), 2)}"]
        for k in ("1w", "1m"):
            if info.get(k) is not None:
                facts.append(f"{k} {self._pct(info[k])}")
        if ctx["nifty"] is not None:
            facts.append(f"Nifty today {self._pct(ctx['nifty'], 2)}")
        if "today" in pr["rank"]:
            facts.append(f"peers today {pr['rank']['today'][3]:+.1f}% avg")
        lines.append("📏 " + " · ".join(facts))
        r = self.ai.explain_move(sym, company, "fell" if direction == "down" else "rose",
                                 self.facts_text(ctx, news_hours=96)) if self.ai.provider else None
        if r:
            if r["actual_move"] in ("up", "down", "flat") and not r["matches_user"]:
                lines.append(f"⚠️ <b>Note:</b> the data shows it actually went <b>{r['actual_move']}</b>, not {word}.")
            lines.append(f"\n🤖 <b>{esc(r['headline'])}</b>")
            tag = {"company": "🏢 Company", "sector": "🏭 Sector", "market": "🌐 Market"}
            for x in r["reasons"]:
                lines.append(f"{tag.get(x['type'], '❔ Unclear')}: {esc(x['reason'])}"
                             + (f"\n      <i>↳ {esc(x['evidence'])}</i>" if x["evidence"] else ""))
            lines.append(f"<i>AI confidence {r['confidence']:.0%}. Based only on price data, peers, and company + sector/policy news (📎 Sources).</i>")
        elif not self.ai.provider:
            lines.append("🤖 AI is off: the owner can add a free AI key (☰ More → 🔑 AI key).")
        else:
            lines.append("🤖 AI explanation unavailable right now (daily limit or AI error). Tap 📎 Sources for the news.")
        self._send_lines(u, lines, [[("🔎 Full report", f"stock:{sym}"), ("🔄 Re-check", f"move:{sym}:{direction}")],
                                    [("📎 Sources", f"src:{sym}")]])

    def high_certainty(self, u: User, hours: float = 24) -> list:
        """Stocks where the evidence clearly points one way: most signals agree, the combined
        weight is large, and either the evidence score or the AI's confidence is high.
        No count limit: it can be 0 stocks or 20."""
        muted = u.muted() if u else set()
        syms = {e["symbol"] for e in self.store.events_since(time.time() - hours * 3600)
                if e["symbol"] and e["symbol"] not in muted}
        out = []
        for s in syms:
            evs = self.store.events_for(s, hours)
            o = outlook(evs)
            if not o["sign"] or abs(o["ratio"]) < 0.75 or abs(o["net"]) < 3:
                continue
            sc = score_events(evs, self.day_change.get(s))
            if sc["direction"] not in (0, o["sign"]):
                continue
            if sc["score"] < 70 and o["ai_conf"] < 0.8:
                continue
            very = abs(o["net"]) >= 6 and abs(o["ratio"]) >= 0.85 and (sc["score"] >= 80 or o["ai_conf"] >= 0.85)
            out.append({"symbol": s, "o": o, "score": sc["score"], "very": very})
        return sorted(out, key=lambda x: (not x["very"], -abs(x["o"]["net"])))

    SCOPES = {"all": "all stocks", "others": "stocks you don't own", "mine": "your portfolio"}

    def scope_buttons(self, scope: str):
        names = {"all": "🌐 All", "others": "🔍 Not in my portfolio", "mine": "⭐ Only mine"}
        return [[(("• " if k == scope else "") + v, f"sure:{k}") for k, v in names.items()]]

    def certainty_view(self, u: User, scope: str = "all"):
        mine = u.portfolio()
        items = self.high_certainty(u)
        if scope == "others":
            items = [it for it in items if it["symbol"] not in mine]
        elif scope == "mine":
            items = [it for it in items if it["symbol"] in mine]
        where = self.SCOPES.get(scope, "all stocks")
        if not items:
            return ([f"✅ <b>High certainty · {where}</b>",
                     "Nothing right now. A stock appears here only when most of the last 24h's signals point "
                     "the same way and the evidence is strong."], self.scope_buttons(scope))
        lines = [f"✅ <b>High certainty · {where}</b> · {len(items)} stock{'s' if len(items) != 1 else ''} (last 24h)",
                 "<i>Most signals agree and the evidence is strong. Strong evidence, not a guarantee. "
                 "📌 Every call here is recorded and judged at the next close → 🏆 Track record.</i>"]
        for it in items:
            s, o = it["symbol"], it["o"]
            arrow = "🟢 ↑" if o["sign"] > 0 else "🔴 ↓"
            agree = round(100 * (o["pos"] if o["sign"] > 0 else o["neg"]) / max(1, o["pos"] + o["neg"]))
            dc = self.day_change.get(s)
            moved = dc is not None and dc * o["sign"] >= 5
            lines.append(f"\n{arrow} {'⭐ ' if s in mine else ''}<b>{esc(s)}</b> · "
                         f"{'Very high' if it['very'] else 'High'} conviction")
            lines.append(f"   {agree}% of signals agree · evidence {it['score']:.0f}/100"
                         + (f" · AI {o['ai_conf']:.0%}" if o["ai_conf"] else "")
                         + (f" · today {dc:+.1f}%" if dc is not None else ""))
            if o["drivers"].get(o["sign"]):
                lines.append(f"   {esc(short(o['drivers'][o['sign']], 110))}")
            if moved:
                lines.append("   ⏱️ Already moved a lot today; may be late.")
        add = [it["symbol"] for it in items if it["symbol"] not in mine][:6]
        rows = [[(f"➕ {s}", f"add:{s}") for s in add[i:i + 3]] for i in range(0, len(add), 3)]
        return lines, rows + self.scope_buttons(scope)

    def send_certainty(self, u: User, scope: str) -> None:
        lines, rows = self.certainty_view(u, scope)
        text = "\n".join(lines)
        if len(text) > 3800:
            u.send_long(lines)
            u.send("Filter or add:", buttons=rows)
        else:
            u.send(text, buttons=rows)

    def portfolio_view(self, u: User):
        """Your stocks ranked by how much is going on, with each one's overall verdict.
        Quiet stocks are collapsed into one line. Editing is behind ✏️ Edit."""
        mine = sorted(u.portfolio())
        if not mine:
            return ("⭐ <b>Your portfolio</b> is empty. Tap ➕ Add stocks.", [[("➕ Add stocks", "prompt:add")]])
        looks = {s: self.outlook_of(s) for s in mine}
        active = sorted([s for s in mine if looks[s]["label"]], key=lambda s: -abs(looks[s]["net"]))
        quiet = [s for s in mine if not looks[s]["label"]]
        lines = [f"⭐ <b>Your portfolio</b> · overall verdict from the last 24h"]
        for s in active:
            o = looks[s]
            dc = self.day_change.get(s)
            lines.append(f"<b>{esc(s)}</b> · {o['label']} ({o['pos']}+ / {o['neg']}−)"
                         + (f" · today {dc:+.1f}%" if dc is not None else ""))
            d = o["drivers"].get(o["sign"]) or o["drivers"].get(1) or o["drivers"].get(-1)
            if d:
                lines.append(f"   {esc(short(d, 100))}")
        if quiet:
            lines.append(f"\n😴 No news: {esc(', '.join(quiet))}")
        return "\n".join(lines), [[("✏️ Edit portfolio", "cmd:/editportfolio")]]

    def portfolio_edit_view(self, u: User):
        mine, muted = sorted(u.portfolio()), sorted(u.muted())
        text = (f"✏️ <b>Edit portfolio</b> ({len(mine)}): {esc(', '.join(mine)) or 'empty'}\n"
                f"🔇 Muted: {esc(', '.join(muted)) or 'none'}\n"
                f"<i>Also watching {len(self.universe)} index stocks and ~{len(self.market_matcher)} companies in the news.</i>")
        rows = [[(f"❌ {s}", f"rm:{s}") for s in mine[i:i + 3]] for i in range(0, len(mine), 3)]
        rows.append([("➕ Add stocks", "prompt:add")])
        rows += [[(f"🔊 Unmute {s}", f"unmute:{s}") for s in muted[i:i + 2]] for i in range(0, len(muted), 2)]
        return text, rows

    def more_view(self, u: User):
        rows = [[("🎯 Top candidates", "cmd:/top"), ("🩺 Status", "cmd:/status")],
                [("❓ What does each alert mean?", "cmd:/help")]]
        if u.is_owner:
            rows = [[("👥 Users", "cmd:/users"), ("📡 Channels", "cmd:/channels")],
                    [("🤖 AI status", "cmd:/ai")],
                    [("🔑 Set AI key", "prompt:setkey"), ("⬆️ Update now", "cmd:/update")]] + rows
        return "☰ <b>More</b>", rows

    def users_view(self):
        people = self.store.users("active")
        lines = [f"👥 <b>People using the bot</b> ({len(people)})"]
        rows = []
        for chat, name in people:
            u = User(self, chat, name)
            tag = " (you)" if u.is_owner else ""
            lines.append(f"• {esc(name)}{tag} · {len(u.portfolio())} stocks")
            if not u.is_owner:
                rows.append([(f"🚫 Remove {name[:30]}", f"kick:{chat}")])
        removed = self.store.users("removed")
        rows += [[(f"↩️ Allow {n[:30]} again", f"allow:{c}")] for c, n in removed]
        return "\n".join(lines), rows

    HELP = ("<b>What you'll receive</b>\n"
            "⭐ Anything about your portfolio stocks, with 🤖 AI reasoning\n"
            "⚡ Breaking: one report the AI judges a large, confident move\n"
            "🎯 Opportunity: several kinds of evidence agree\n"
            "📣 Story: several outlets covering a stock\n"
            "🧭 Sector news that affects your stocks\n"
            "🌅 9:06 pre-open gaps · ☀️ 8:30 brief · 📋 digests 12:30, 3:45, 8:30pm\n\n"
            "📊 Every alert about your stock shows its <b>overall</b> verdict from all of today's news, "
            "so one green or red item never misleads you. 🔄 tells you when that verdict flips.\n"
            "✅ <b>High certainty</b> lists only the stocks where the evidence clearly points one way.\n\n"
            "🔎 Type any stock's name or symbol (e.g. <code>indigo</code>) for its news, sector, rank vs peers and AI analysis.\n"
            "📉 Type <code>indigo fall</code> or <code>tcs rise</code> and the AI explains why it moved.\n\n"
            "Start by tapping ⭐ Portfolio → ✏️ Edit portfolio → ➕ Add stocks. "
            "Paste any headline as a message and the AI tells you what it means for stocks.")

    # ================================================================== commands
    def handle_commands(self) -> None:
        for ev in self.tg.commands():
            if isinstance(ev, str):
                ev = {"type": "text", "text": ev}
            chat = str(ev.get("chat") or config.TELEGRAM_CHAT_ID)
            status = self.store.user_status(chat)
            if status == "removed":
                continue
            u = User(self, chat, ev.get("name", ""))
            if status is None:  # first time this person talks to the bot: give them their own space
                self.store.user_add(chat, ev.get("name") or "someone")
                u.send("👋 <b>Welcome to Dalal Radar.</b>\n" + self.HELP, reply_keyboard=self.PANEL)
                if not u.is_owner:
                    self.owner.send(f"👤 {esc(ev.get('name') or 'Someone')} started using the bot.",
                                    buttons=[[("🚫 Remove", f"kick:{chat}")]])
                    self.tg.set_menu(self.MENU)
                if ev["type"] == "text" and ev["text"].lower().startswith("/start"):
                    continue
            try:
                if ev["type"] == "tap":
                    self.on_tap(u, ev)
                else:
                    self.on_text(u, ev["text"])
            except SystemExit:
                raise
            except Exception as e:  # a bad command must never stop the engine
                log.exception("command failed")
                u.send(f"⚠️ That didn't work: {esc(str(e)[:200])}")

    def on_tap(self, u: User, ev: dict) -> None:
        data, mid = ev["data"], ev["msg_id"]
        kind, _, rest = data.partition(":")
        toast = ""
        if kind == "cmd":
            self.on_text(u, rest)
        elif kind == "set":
            name, _, sign = rest.partition(":")
            _l, step, lo, hi = self.STEPS[name]
            new = round(min(hi, max(lo, u.cfg(name) + (step if sign == "+" else -step))), 2)
            self.set_setting(name, str(new), u.chat)
            self.tg.edit(mid, *self.settings_view(u), chat=u.chat)
            toast = f"{self.STEPS[name][0]}: {new:g}"
        elif kind == "quiet":
            s, e = u.quiet_range()
            self.set_setting("quiet", "23-7" if s == e else "off", u.chat)
            self.tg.edit(mid, *self.settings_view(u), chat=u.chat)
        elif kind in ("rm", "unmute"):
            if kind == "rm":
                self.store.watch_remove(rest, u.chat)
                self.rebuild_matcher()
            else:
                self.store.mute(rest, False, u.chat)
            self.tg.edit(mid, *self.portfolio_edit_view(u), chat=u.chat)
            toast = f"{'Removed' if kind == 'rm' else 'Unmuted'} {rest}"
        elif kind == "add":
            self.store.watch_add(rest, u.chat)
            self.rebuild_matcher()
            toast = f"⭐ {rest} added to your portfolio"
        elif kind == "mute":
            self.store.mute(rest, True, u.chat)
            toast = f"🔇 {rest} muted"
        elif kind == "sure":
            self.send_certainty(u, rest)
        elif kind == "stock":
            self.send_stock(u, rest)
        elif kind == "src":
            u.send_long(self.sources_view(rest))
        elif kind == "stockr":
            self.send_stock(u, rest, refresh=True)
        elif kind == "move":
            sym, _, d = rest.partition(":")
            self.explain_move(u, sym, d if d in ("up", "down") else "down")
        elif kind == "rmq":
            self.store.watch_remove(rest, u.chat)
            self.rebuild_matcher()
            toast = f"Removed {rest} from your portfolio"
        elif kind == "track":
            lines, rows = self.track_view(rest)
            if isinstance(lines, str):
                u.send(lines)
            else:
                u.send_long(lines)
        elif kind == "brief":
            u.send_long(self.brief(now() - timedelta(hours=float(rest)), f"News brief · last {rest}h", user=u))
        elif kind == "prompt":
            if rest in ("setkey", "channel") and not u.is_owner:
                return self.tg.answer(ev["id"], "Only the owner can do that.")
            self.awaiting[u.chat] = rest
            u.send({"add": "Type the NSE symbols you own, separated by spaces (e.g. <code>TCS HAL IRFC</code>).",
                    "ask": "Paste any news headline and I'll tell you which stocks it likely moves.",
                    "setkey": "Paste your Gemini (AQ.… / AIza…) or Groq (gsk_…) key.",
                    "channel": "Send the public channel's username (e.g. <code>@channelname</code>) or its t.me link."
                    }.get(rest, "Type it now."))
        elif kind == "rmch" and u.is_owner:
            self.store.set("channels", ",".join(c for c in self.channel_list() if c != rest))
            self.tg.edit(mid, *self.channels_view(), chat=u.chat)
            toast = f"Stopped reading @{rest}"
        elif kind in ("kick", "allow") and u.is_owner:
            self.store.user_set_status(rest, "removed" if kind == "kick" else "active")
            if mid:
                self.tg.edit(mid, *self.users_view(), chat=u.chat)
            toast = "Removed" if kind == "kick" else "Allowed again"
        self.tg.answer(ev["id"], toast)

    def on_text(self, u: User, text: str) -> None:
        text = self.PANEL_MAP.get(text, text)
        if not text.startswith("/"):
            waiting = self.awaiting.pop(u.chat, None)
            if waiting == "add":
                return self.on_text(u, "/add " + text)
            if waiting == "setkey":
                return self.on_text(u, "/setkey " + text)
            if waiting == "channel":
                return self.on_text(u, "/addchannel " + text)
            if waiting == "ask":
                return self.cmd_ask(u, text)
            mv = self.parse_move(text)  # "indigo fall", "why did tcs rise today"
            if mv and len(self.resolve_stock(text)) == 1:
                mv = None  # the move word is part of a company name ("adani green")
            if mv:
                cands = self.resolve_stock(mv[0])
                if len(cands) == 1:
                    return self.explain_move(u, cands[0], mv[1])
                if cands:
                    return u.send("Which one did you mean?", buttons=[
                        [(f"{c} · {(self.names.get(c) or '')[:28]}", f"move:{c}:{mv[1]}")] for c in cands])
            if len(text) <= 40 and len(text.split()) <= 5:  # looks like a stock name or symbol
                cands = self.resolve_stock(text)
                if len(cands) == 1:
                    return self.send_stock(u, cands[0])
                if cands:
                    return u.send(f"Which one did you mean?", buttons=[
                        [(f"{c} · {(self.names.get(c) or '')[:28]}", f"stock:{c}")] for c in cands])
            if len(text) >= 15:  # a pasted headline: ask the AI what it means
                return self.cmd_ask(u, text)
            return u.send("I couldn't find that stock. Type its NSE symbol (e.g. <code>TCS</code>) or company name, "
                          "or paste a news headline.", reply_keyboard=self.PANEL)
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = [p.upper().strip(",") for p in parts[1:]]
        if cmd in self.OWNER_ONLY and not u.is_owner:
            return u.send("Only the bot's owner can do that.")
        if cmd in ("/start", "/menu", "/help"):
            u.send(self.HELP, reply_keyboard=self.PANEL)
        elif cmd == "/more":
            u.send(*self.more_view(u))
        elif cmd == "/users":
            u.send(*self.users_view())
        elif cmd == "/channels":
            u.send(*self.channels_view())
        elif cmd == "/addchannel":
            if len(parts) < 2:
                return self._prompt(u, "channel")
            self.add_channel(u, parts[1])
        elif cmd in ("/add", "/watch"):
            if not args:
                return self._prompt(u, "add")
            for a in args:
                self.store.watch_add(a, u.chat)
            self.rebuild_matcher()
            u.send(*self.portfolio_edit_view(u))
        elif cmd in ("/remove", "/unwatch") and args:
            for a in args:
                self.store.watch_remove(a, u.chat)
            self.rebuild_matcher()
            u.send(*self.portfolio_edit_view(u))
        elif cmd == "/mute" and args:
            for a in args:
                self.store.mute(a, True, u.chat)
            u.send(f"🔇 Muted: <b>{esc(', '.join(args))}</b>")
        elif cmd == "/unmute" and args:
            for a in args:
                self.store.mute(a, False, u.chat)
            u.send(f"🔊 Unmuted: <b>{esc(', '.join(args))}</b>")
        elif cmd in ("/portfolio", "/list"):
            u.send(*self.portfolio_view(u))
        elif cmd == "/editportfolio":
            u.send(*self.portfolio_edit_view(u))
        elif cmd in ("/sure", "/certain", "/new"):
            self.send_certainty(u, "others" if cmd == "/new" else "all")
        elif cmd == "/top":
            c = self.top_candidates(time.time() - 24 * 3600, 8, user=u)
            if not c:
                return u.send("🎯 No candidate is strong enough right now.")
            u.send("🎯 <b>Top candidates now</b> (tap to add or mute)\n" + "\n".join(
                f"<b>{esc(s)}</b> {sc:.0f}/100 {ARROW[d['direction']]}" for sc, s, d in c),
                buttons=[[(f"➕ {s}", f"add:{s}"), (f"🔇 {s}", f"mute:{s}")] for _sc, s, _d in c[:6]])
        elif cmd == "/brief":
            if args and args[0].replace(".", "").isdigit():
                return u.send_long(self.brief(now() - timedelta(hours=float(args[0])), "News brief", user=u))
            u.send("🗞️ News summary for…", buttons=[[("Last 2h", "brief:2"), ("4h", "brief:4"),
                                                   ("12h", "brief:12"), ("24h", "brief:24")]])
        elif cmd == "/digest":
            if self.store.has_queue(u.chat):
                self.send_digest("Digest", user=u)
            else:
                u.send("📥 Nothing held back right now.")
        elif cmd == "/setkey":
            if len(parts) < 2:
                return self._prompt(u, "setkey")
            self.cmd_setkey(u, parts[1].strip())
        elif cmd == "/ask":
            if len(parts) < 2:
                return self._prompt(u, "ask")
            self.cmd_ask(u, text.split(None, 1)[1])
        elif cmd == "/ai":
            self.ai.budget_left()
            u.send(f"🤖 AI reasoning: {self.ai.provider or 'off'}\n"
                   f"Headlines read today (everyone): {self.ai.calls_today}/{self.ai.max_per_day}"
                   + (f"\nRecent errors: {self.ai.errors}\n<code>{esc(self.ai.last_error)}</code>" if self.ai.errors else ""))
        elif cmd == "/track":
            lines, rows = self.track_view("summary")
            if isinstance(lines, str):
                return u.send(lines)
            u.send_long(lines)
            u.send("More:", buttons=rows)
        elif cmd == "/learn":
            self.cmd_learn(u)
        elif cmd == "/settings":
            u.send(*self.settings_view(u))
        elif cmd == "/set" and len(parts) >= 3:
            u.send(self.set_setting(parts[1], parts[2], u.chat))
        elif cmd == "/update":
            self.self_update()
        elif cmd == "/status":
            day = now().strftime("%Y-%m-%d")
            lines = [f"✅ Running · {now().strftime('%d %b %H:%M:%S')} IST",
                     f"Your alerts today: ⚡ {u.count('brk', day)}/{u.cfg('maxbreak')} · "
                     f"🎯 {u.count('opp', day)}/{u.cfg('maxopp')} · 📣 {u.count('story', day)}/{u.cfg('maxstory')}"]
            if u.is_owner:
                bad = {k: v for k, v in self.failures.items() if v}
                lines.append(f"People using the bot: {len(self.store.users('active'))}")
                lines.append(f"Failing sources: {'none' if not bad else ''}")
                lines += [f"• {esc(k)}: {esc(self.last_error.get(k, ''))}" for k in bad]
            u.send("\n".join(lines))
        else:
            u.send(self.HELP, reply_keyboard=self.PANEL)

    def _prompt(self, u: User, what: str) -> None:
        self.on_tap(u, {"data": f"prompt:{what}", "msg_id": 0, "id": ""})

    def cmd_setkey(self, u: User, key: str) -> None:
        self.store.set("ai_key", key)
        self.ai.key = key
        prov = self.ai.provider
        if not prov:
            return u.send("That doesn't look like a Gemini (AIza… or AQ.…) or Groq (gsk_…) key.")
        test = self.ai.read("Government raises import duty on steel to 20%", "test", [])
        u.send((f"✅ AI reasoning on ({prov}), test read worked." if test is not None else
                f"⚠️ Key saved but the test call failed:\n<code>{esc(self.ai.last_error)}</code>")
               + "\nPlease delete your message that contains the key.")

    def cmd_ask(self, u: User, q: str) -> None:
        if not self.ai.provider:
            return u.send("AI is off. Ask the bot's owner to set an AI key.")
        r = self.ai.read(q, "you", sorted(u.portfolio()))
        if r is None:
            return u.send(f"⚠️ AI call failed: <code>{esc(self.ai.last_error)}</code>")
        if not r["impacts"]:
            return u.send("🤖 No clear effect on any listed stock" + (" (already priced in)." if r["priced_in"] else "."))
        mine = u.portfolio()
        out = ["🤖 <b>Likely impact</b>" + (" · already priced in" if r["priced_in"] else "")]
        for i in r["impacts"]:
            star = "⭐ " if i["symbol"] in mine else ""
            out.append(f"{'🟢 ↑' if i['direction'] == 'up' else '🔴 ↓'} {star}<b>{esc(i['symbol'])}</b> "
                       f"{i['magnitude']} ({i['confidence']:.0%}, {esc(i['order'])})\n   {esc(i['why'])}")
        out.append("<i>An AI reading of the headline, not a prediction. Check the price before acting.</i>")
        others = [i["symbol"] for i in r["impacts"] if i["symbol"] not in mine][:4]
        u.send("\n".join(out), buttons=[[(f"➕ {s}", f"add:{s}") for s in others]] if others else None)

    def cmd_learn(self, u: User) -> None:
        rows = self.store.outcome_stats()
        if not rows:
            return u.send("📊 Nothing measured yet. Each signal's price is checked 1 hour and 1 trading day "
                          "later; results appear after the first few days.")
        out = ["📊 <b>What happened after each kind of signal</b>",
               "(hit = moved the predicted way; avg = average move in that direction)"]
        for r in rows[:15]:
            h1d = (f"1d: hit {r['hit1d'] / r['n1d']:.0%}, avg {r['sum1d'] / r['n1d']:+.1f}% (n={r['n1d']})"
                   if r["n1d"] else "1d: pending")
            h1h = f"1h: avg {r['sum1h'] / r['n1h']:+.1f}% (n={r['n1h']})" if r["n1h"] else ""
            out.append(f"• <b>{esc(r['label'])}</b> [{esc(r['kind'])}] {h1d} {h1h}")
        u.send_long(out)

    # ================================================================== health
    def ok(self, job: str) -> None:
        if self.failures.get(job, 0) >= 5:
            self.owner.send(f"✅ {job} source is working again.")
        self.failures[job] = 0

    def fail(self, job: str, err: Exception) -> None:
        n = self.failures.get(job, 0) + 1
        self.failures[job] = n
        self.last_error[job] = f"{type(err).__name__}: {err}"[:160]
        log.warning("%s failed (%d): %s", job, n, err)
        if n == 5 and not job.startswith("news:"):  # one dead news feed isn't worth a ping
            self.owner.send(f"⚠️ <b>{job}</b> has failed 5 times in a row: {esc(str(err)[:200])}\n"
                            "Alerts from this source are paused until it recovers.")

    # ================================================================== loop
    def once_today(self, name: str, t: datetime) -> bool:
        return self.store.first_time(f"daily:{name}:{t.date()}")

    def run(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
        try:
            self.refresh_universe()
        except Exception as e:
            self.fail("universe", e)
        self.tg.set_menu(self.MENU)
        n = len(self.store.users("active"))
        # When the button panel changes, give everyone the new one once (their old panel stays otherwise)
        panel_id = "|".join("/".join(r) for r in self.PANEL)
        if self.store.get("panel_id") != panel_id:
            self.store.set("panel_id", panel_id)
            for u in self.users():
                if not u.is_owner:
                    u.send("🆕 New buttons: 🔍 Sure, not owned (high-certainty stocks you don't hold), "
                           "🏆 Track record, and ✅ High certainty. 🎯 Top is now under ☰ More.",
                           reply_keyboard=self.PANEL)
        self.owner.send(f"🛰️ Stock Radar online · {n} {'person' if n == 1 else 'people'} using it · "
                        f"your portfolio: {len(self.owner.portfolio())} stocks.", reply_keyboard=self.PANEL)
        nxt = dict.fromkeys(["filings", "results", "prices", "cmds", "news", "insider", "deals", "outcomes", "channels", "certainty", "evaluate"], 0)
        last_universe = last_prune = now().date()
        retry_universe = 0
        while True:
            t = now()
            mono = time.monotonic()
            fast = busy_hours(t)

            def due(job, every):
                if mono >= nxt[job]:
                    nxt[job] = mono + every
                    return True
                return False

            if due("filings", config.FILINGS_POLL_FAST if fast else config.FILINGS_POLL_SLOW):
                self.poll_filings()
            if due("news", config.NEWS_POLL):
                self.poll_news()
            if due("results", 30 if fast else 60):
                self.poll_results()
            if fast and due("insider", 120):
                self.poll_insiders()
            if fast and due("deals", 300):
                self.poll_deals()
            if market_open(t) and due("prices", config.PRICES_POLL):
                self.poll_prices()
            if market_open(t) and due("outcomes", 300):
                self.poll_outcomes()
            if due("certainty", 900):
                self.scan_certainty()
            if is_weekday(t) and "15:40" <= f"{t.hour:02d}:{t.minute:02d}" < "18:00" and due("evaluate", 600):
                self.evaluate_predictions()
            if self.channel_list() and due("channels", 60):
                self.poll_channels()
            if due("cmds", config.TELEGRAM_POLL):
                self.handle_commands()

            hm = f"{t.hour:02d}:{t.minute:02d}"
            if is_weekday(t):
                if "08:30" <= hm < "15:30" and self.once_today("digest", t):
                    self.morning_digest()
                if "09:06" <= hm < "09:15" and self.once_today("preopen", t):
                    self.preopen_scan()
                if hm == "09:14" and self.once_today("reset", t):
                    self.momentum.reset()
                    self.day_change.clear()
            for dt in config.DIGEST_TIMES:
                if dt <= hm < f"{int(dt[:2]) + 1:02d}{dt[2:]}" and self.once_today("dg" + dt, t):
                    self.send_digest(f"Digest {dt}")
            if (t.date() != last_universe and t.hour >= 8) or (not self.universe and mono >= retry_universe):
                retry_universe = mono + 600
                try:
                    self.refresh_universe()
                    if self.universe:
                        last_universe = t.date()
                        self.ok("universe")
                except Exception as e:
                    self.fail("universe", e)
            if t.date() != last_prune:
                self.store.prune(); last_prune = t.date()
                self._story_ai.clear()
            time.sleep(1)


if __name__ == "__main__":
    Radar().run()
