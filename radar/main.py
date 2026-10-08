"""Stock Radar engine.

Two lists, two rules:
  * YOUR PORTFOLIO: every relevant filing, news item, deal or price burst reaches you
    (routine items are rate-limited per stock; urgent ones always go through).
  * THE REST OF THE MARKET (~2,000 stocks): watched silently. A stock pings you only
    when independent signals agree strongly enough (opportunity score >= 70/100),
    and never more than OPP_MAX_PER_DAY times a day. Weaker candidates go into a
    digest 3 times a day and the 8:30am brief.

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
from .classify import HIGH, MEDIUM, classify
from .news import (DEFAULT_FEEDS, NewsFeeds, analyse_headline, build_market_matcher,
                   build_matcher, split_source)
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


def quiet_hours(t: datetime) -> bool:
    s, e = config.QUIET_START, config.QUIET_END
    return (t.hour >= s or t.hour < e) if s > e else (s <= t.hour < e)


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


class Radar:
    def __init__(self):
        self.store = Store(config.DB_PATH)
        self.tg = Telegram(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
        self.nse = NSE()
        self.bse = BSE() if config.USE_BSE else None
        self.universe = set()        # index stocks we scan for prices (not alerted by default)
        self.failures = {}
        self.last_error = {}
        self.last_price = {}         # symbol -> latest price seen (for outcome tracking)
        self.stats, self._stats_at = {}, 0.0
        self._ai_cycle = 0
        self.result_tries = {}
        self.warmed_up = set()
        self.names = {}              # symbol -> company name
        self.matcher = []            # portfolio + index stocks (aliases, symbols)
        self.market_matcher = []     # every other NSE company, by full name
        self.day_change = {}         # symbol -> today's % change (to spot 'already moved')
        self.momentum = Momentum(config.MOMENTUM_WINDOW_MIN * 60, config.MOMENTUM_MOVE,
                                 config.MOMENTUM_VOL_MULT)
        self.news = NewsFeeds(config.NEWS_FEEDS or DEFAULT_FEEDS)
        if self.store.get("initialized"):
            self.warmed_up = {"nse", "bse", "results", "insider", "deals"}
        self.apply_saved_settings()
        self.ai = AIReader(self.store.get("ai_key") or config.AI_KEY, config.AI_MAX_PER_DAY)
        self.ai.model = config.AI_MODEL

    # ================================================================== lists
    def portfolio(self) -> set:
        return set(config.EXTRA_WATCHLIST) | set(self.store.watchlist())

    def watched(self) -> set:  # kept for older callers
        return self.portfolio()

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
    def notify(self, sym: str, text: str, urgent: bool = False) -> None:
        """Portfolio alerts. Urgent ones always go out. Routine ones respect quiet
        hours and a per-stock cooldown; held-back items go into the next digest."""
        if sym in self.store.muted():
            return
        t = now()
        if not urgent:
            cooling = False
            if config.STOCK_COOLDOWN_MIN > 0:  # 0 = no cooldown, every item goes out
                slot = int(time.time() // (config.STOCK_COOLDOWN_MIN * 60))
                cooling = not self.store.first_time(f"cool:{sym}:{slot}")
            if quiet_hours(t) or cooling:
                self.store.queue(sym, text, 50)
                return
        self.tg.send(text)

    def consider(self, sym: str) -> None:
        """A stock outside your portfolio just produced a signal. Score all its
        evidence from the last 24 h and decide: ping, digest, or stay silent."""
        if not sym or sym in self.portfolio() or sym in self.store.muted():
            return
        evs = self.store.events_for(sym, 24)
        s = score_events(evs, self.day_change.get(sym))
        adj, why = self.learned_adjust(evs)
        if adj:
            s["score"] = max(0, min(100, s["score"] + adj))
            s["reasons"].append(why)
        day = now().strftime("%Y-%m-%d")
        if s["score"] >= config.OPP_MIN_SCORE and not quiet_hours(now()):
            used = self.store.count_today("opp", day)
            if used < config.OPP_MAX_PER_DAY and self.store.first_time(f"opp:{day}:{sym}"):
                self.tg.send(self.format_opportunity(sym, s, used + 1))
                return
        if s["score"] >= config.OPP_DIGEST_SCORE and self.store.first_time(f"oppq:{day}:{sym}"):
            self.store.queue(sym, self.format_candidate(sym, s), s["score"])

    def format_opportunity(self, sym, s, n) -> str:
        ev = self.store.events_for(sym, 24)[-3:]
        lines = [f"🎯 <b>Opportunity · {esc(sym)}</b> {ARROW[s['direction']]}",
                 f"Evidence score <b>{s['score']:.0f}/100</b> · alert {n} of {config.OPP_MAX_PER_DAY} today",
                 *[f"   {esc(r)}" for r in s["reasons"]],
                 "<b>Latest evidence</b>",
                 *[f"• {TONE.get(e['tone'], '🟡')} {esc(e['headline'][:150])} <i>({esc(e['source'] or e['kind'])})</i>"
                   for e in reversed(ev)],
                 "<i>Score = strength of evidence, not a guarantee. /add to track it, /mute to silence it.</i>"]
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
        if sym in self.portfolio():
            msg = (f"{TONE[c['tone']]} <b>⭐ {esc(sym)}</b> · {esc(c['label'])}\n{esc(it['headline'][:600])}\n"
                   f"<i>{it['exchange']} filing · {now().strftime('%H:%M:%S')}</i>")
            if it["url"]:
                msg += f'\n<a href="{esc(it["url"])}">Open filing</a>'
            if c["label"] == "Quarterly results":
                msg += "\n⏳ Numbers follow as soon as NSE publishes them."
            self.notify(sym, msg, urgent=c["impact"] == HIGH)
        elif c["label"] != "Quarterly results":  # others' results are judged on the numbers
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
            if a["score"]:
                self.store.track(sym, "results", a["verdict"], 1 if a["score"] > 0 else -1)
            if sym in self.portfolio():
                self.notify(sym, self.format_result(sym, kind, a), urgent=True)
            else:
                tone = "+" if a["score"] > 0 else "-" if a["score"] < 0 else "?"
                self.store.log_event(sym, "results", f"Q results {a['verdict']} ({a['score']:+d}): "
                                     + "; ".join(a["reasons"]), tone,
                                     HIGH if abs(a["score"]) >= 3 else MEDIUM, "NSE")
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
        seen = {r.get("symbol") for r in rows}
        for sym in sorted(self.portfolio() - seen):  # portfolio stocks outside the indices
            try:
                rows.append(self.nse.quote(sym))
            except Exception as e:
                log.warning("quote %s: %s", sym, e)

        mine = self.portfolio()
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
                if sym in mine:
                    self.notify(sym, f"🚀 <b>⭐ {esc(sym)}</b> {sig['move']:+.1f}% in {sig['minutes']:.0f} min{vtxt} "
                                     f"· day {chg:+.1f}% · ₹{price:,.2f}\n" + "\n".join(self._reasons(sym)),
                                urgent=sig["strong"])
                else:
                    self.store.log_event(sym, "burst", f"Price {sig['move']:+.1f}% in {sig['minutes']:.0f} min{vtxt}",
                                         "+" if sig["move"] > 0 else "-", MEDIUM, "NSE")
                    self.consider(sym)
            if sym in mine:
                crossed = [lv for lv in config.MOVE_LEVELS if abs(chg) >= lv]
                new = [lv for lv in crossed
                       if self.store.first_time(f"move:{today}:{sym}:{'+' if chg > 0 else '-'}{lv}")]
                if new:
                    self.notify(sym, f"{'🟢' if chg > 0 else '🔴'} <b>⭐ {esc(sym)}</b> {chg:+.2f}% at ₹{price:,.2f} "
                                     f"(crossed {max(new):g}%)\n" + "\n".join(self._reasons(sym)), urgent=True)
                try:
                    if price >= float(r.get("yearHigh")) > 0 and self.store.first_time(f"52wh:{today}:{sym}"):
                        self.store.queue(sym, f"📈 <b>{esc(sym)}</b> new 52-week high ₹{price:,.2f}", 45)
                except (TypeError, ValueError):
                    pass

    def _reasons(self, sym: str, hours: float = 24) -> list:
        return [f"   ↳ {esc(h[:140])}" for h, _, _ in self.store.recent_filings(sym, hours)]

    # ================================================================== pre-open
    def preopen_scan(self) -> None:
        """~9:06 IST: your stocks' opening gaps, plus at most 5 other big gaps that
        have news behind them (a gap with no news is usually noise)."""
        try:
            rows = self.nse.pre_open("ALL")
            self.ok("preopen")
        except Exception as e:
            self.fail("preopen", e)
            return
        mine_set, mine, others = self.portfolio(), [], []
        for r in rows:
            try:
                chg = float(r["pChange"])
            except (TypeError, ValueError, KeyError):
                continue
            sym = r.get("symbol") or ""
            if sym in mine_set and abs(chg) >= config.PREOPEN_MIN_GAP:
                mine.append((chg, sym, r.get("iep")))
            elif abs(chg) >= config.PREOPEN_MIN_GAP_ALL and self.store.recent_filings(sym, 18):
                self.store.log_event(sym, "gap", f"Pre-open gap {chg:+.1f}%", "+" if chg > 0 else "-", MEDIUM, "NSE")
                others.append((chg, sym, r.get("iep")))
        out = ["🌅 <b>Pre-open</b> (indicative, market opens 9:15)"]

        def fmt(chg, sym, iep, star):
            return [f"{'🟢' if chg > 0 else '🔴'} {star}<b>{esc(sym)}</b> {chg:+.1f}%"
                    + (f" · open ~₹{float(iep):,.2f}" if iep not in (None, "", "-") else "")] + self._reasons(sym, 18)

        out.append("<b>Your portfolio</b>")
        for g in sorted(mine, key=lambda x: -abs(x[0])):
            out += fmt(*g, "⭐ ")
        if not mine:
            out.append("• no big gaps")
        if others:
            out.append("<b>Other big gaps with news behind them</b>")
            for g in sorted(others, key=lambda x: -abs(x[0]))[:5]:
                out += fmt(*g, "")
        self.tg.send("\n".join(out))

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
        mine = self.portfolio()
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
            if sym in mine:
                self.notify(sym, f"{'🟢' if buy else '🔴'} <b>⭐ {esc(sym)}</b> · {esc(what)} via "
                                 f"{esc(r.get('acqMode') or 'market')}", urgent=True)
            elif buy:
                self.store.log_event(sym, "insider", what, "+", HIGH, "NSE")
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
        mine = self.portfolio()
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
            if sym in mine:
                self.notify(sym, f"🐋 <b>⭐ {esc(sym)}</b> · {esc(what)}")
            elif side == "BUY":
                self.store.log_event(sym, "deal", what, "+", MEDIUM, "NSE")
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
        if self._ai_cycle >= 6 and not mine:
            return False
        self._ai_cycle += 1
        return bool(mine or a["themes"] or (a["global"] and a["dramatic"])
                    or (syms and c["impact"] == HIGH) or self.POLICY.search(a.get("_title", "")))

    def handle_headline(self, title: str, src: str, link: str, _unused=None) -> None:
        a = analyse_headline(title, self.matcher, self.market_matcher)
        a["_title"] = title
        c = classify(title)
        t = a["tone"] if a["tone"] != "?" else c["tone"]
        mine_set = self.portfolio()
        syms = a["stocks"] + a["others"]
        mine = [s for s in syms if s in mine_set]

        # --- AI reasoning: who is affected, which way, and why (incl. second-order effects)
        ai_imp = []
        if self.wants_ai(a, c, syms, mine):
            ai = self.ai.read(title, src, sorted(mine_set))
            if ai:
                if ai["priced_in"]:
                    a["already_moved"] = True
                if ai["relevant"]:
                    # guard against made-up symbols: keep only real NSE symbols
                    ai_imp = [i for i in ai["impacts"] if i["confidence"] >= 0.5
                              and (i["symbol"] in self.names or i["symbol"] in mine_set)]
        ai_by = {i["symbol"]: i for i in ai_imp}

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
            if not a["already_moved"]:
                self.store.track(s, "ai", f"AI {i['magnitude']}", 1 if up else -1)
        for name, _ in a["themes"]:
            self.store.log_event("", "theme:" + name, title, t, "MEDIUM", src, link)
        if a["global"]:
            self.store.log_event("", "global", title, t, "MEDIUM", src, link)

        # --- your portfolio: direct mentions + stocks the AI says are affected
        for s in mine + [s for s in ai_by if s in mine_set and s not in mine]:
            i = ai_by.get(s)
            tone_s = ("+" if i["direction"] == "up" else "-") if i else t
            ai_line = (f"\n🤖 {'↑' if i['direction'] == 'up' else '↓'} {i['magnitude']} impact likely "
                       f"({i['confidence']:.0%}, {i['order']}): {esc(i['why'])}") if i else ""
            urgent = not a["already_moved"] and (impact == HIGH or bool(
                i and i["magnitude"] in ("medium", "large") and i["confidence"] >= 0.7))
            self.notify(s, f"📰 {TONE[tone_s]} <b>⭐ {esc(s)}</b> · {esc(label)}\n{esc(title)}{ai_line}\n{stamp}",
                        urgent=urgent)
        for s in set(syms) | set(ai_by):
            if s not in mine_set:
                self.consider(s)

        # --- sector / policy news
        if a["themes"] and config.NEWS_THEMES and not a["already_moved"]:
            name, exposed = a["themes"][0]
            hit = [s for s in set(exposed) | set(ai_by) if s in mine_set]
            if ai_by:
                who = ", ".join(f"{s} {'↑' if i['direction'] == 'up' else '↓'}" for s, i in ai_by.items())
                body = f"🤖 Likely impact: {esc(who)}\n" + "".join(
                    f"   • {esc(s)}: {esc(i['why'])}\n" for s, i in list(ai_by.items())[:3])
            else:
                body = f"Most exposed: {esc(', '.join(exposed[:6]))}\n"
            msg = (f"🧭 <b>Sector news · {esc(name)}</b>\n{esc(title)}\n" + body
                   + (f"Affects your: <b>{esc(', '.join(hit))}</b>\n" if hit else "") + stamp)
            if hit and not quiet_hours(now()) and self.store.first_time(f"theme:{name}:{int(time.time() // 3600)}"):
                self.tg.send(msg)
            else:
                self.store.queue("", msg, 30)
        elif a["global"] and a["dramatic"] and self.store.first_time(f"global:{int(time.time() // 3600)}"):
            self.tg.send(f"🌍 <b>Global cue</b>\n{esc(title)}\n{stamp}")

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

    def top_candidates(self, since_ts: float, limit: int = 8) -> list:
        """Stocks outside the portfolio ranked by opportunity score."""
        mine = self.portfolio()
        syms = {e["symbol"] for e in self.store.events_since(since_ts) if e["symbol"] and e["symbol"] not in mine}
        scored = []
        for s in syms:
            sc = score_events(self.store.events_for(s, 24), self.day_change.get(s))
            if sc["score"] >= config.OPP_DIGEST_SCORE:
                scored.append((sc["score"], s, sc))
        return sorted(scored, key=lambda x: (-x[0], x[1]))[:limit]

    def brief(self, since: datetime, title: str) -> list:
        ev = self.store.events_since(since.timestamp())
        mine = self.portfolio()
        out = [f"🗞️ <b>{title}</b> · since {since.strftime('%a %H:%M')}"]

        glob = [e for e in ev if e["kind"] == "global"][-5:]
        out.append("\n<b>Global cues</b>")
        out += [f"• {TONE.get(e['tone'], '🟡')} {esc(e['headline'][:150])}" for e in glob] or ["• nothing notable"]

        out.append("\n<b>Your portfolio</b>")
        if not mine:
            out.append("• empty. Send /add SYMBOL for each stock you own.")
        per = {}
        for e in ev:
            if e["symbol"] in mine:
                per.setdefault(e["symbol"], []).append(e)
        for s in sorted(per, key=lambda k: -len(per[k])):
            items = per[s]
            net = sum({"+": 1, "-": -1}.get(e["tone"], 0) for e in items)
            out.append(f"{'🟢' if net > 0 else '🔴' if net < 0 else '🟡'} <b>{esc(s)}</b> · {len(items)} items")
            for e in items[-2:]:
                out.append(f"   {esc(e['headline'][:150])}")
        if mine and not per:
            out.append("• nothing new on your stocks")

        out.append("\n<b>Top candidates outside your portfolio</b>")
        cands = self.top_candidates(since.timestamp())
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

    def send_digest(self, title: str = "Digest") -> None:
        rows = self.store.take_queue()
        if not rows:
            return
        mine = self.portfolio()
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
        self.tg.send_long(out)

    def morning_digest(self) -> None:
        t = now()
        self.store.take_queue()  # the brief below covers everything held back overnight
        self.tg.send_long(self.brief(self.last_close(t), "Pre-market brief"))
        try:
            rows = self.nse.board_meetings(t.date(), (t + timedelta(days=14)).date())
        except Exception as e:
            self.fail("digest", e)
            return
        mine = self.portfolio()
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
        self.tg.send_long(text)

    # ================================================================== settings & updates
    SETTINGS = {  # Telegram name -> (config attribute, type, what it means)
        "maxopp": ("OPP_MAX_PER_DAY", int, "max opportunity alerts per day"),
        "score": ("OPP_MIN_SCORE", float, "evidence score (0-100) needed to ping you"),
        "digestscore": ("OPP_DIGEST_SCORE", float, "score needed to appear in digests"),
        "cooldown": ("STOCK_COOLDOWN_MIN", float, "minutes between routine pings per portfolio stock (0 = off)"),
        "burst": ("MOMENTUM_MOVE", float, "% move in 5 min that counts as early momentum"),
        "gap": ("PREOPEN_MIN_GAP", float, "% pre-open gap for your stocks"),
    }

    def apply_saved_settings(self) -> None:
        for name, (attr, typ, _) in self.SETTINGS.items():
            v = self.store.get("cfg:" + name)
            if v is not None:
                setattr(config, attr, typ(float(v)))
        q = self.store.get("cfg:quiet")
        if q:
            a, b = (0, 0) if q == "off" else map(int, q.split("-"))
            config.QUIET_START, config.QUIET_END = a, b

    def settings_text(self) -> str:
        lines = ["⚙️ <b>Settings</b> (change with /set NAME VALUE)"]
        for name, (attr, _, desc) in self.SETTINGS.items():
            lines.append(f"<b>{name}</b> = {getattr(config, attr):g} · {desc}")
        quiet = "off" if config.QUIET_START == config.QUIET_END else f"{config.QUIET_START}-{config.QUIET_END}"
        lines.append(f"<b>quiet</b> = {quiet} · quiet hours, e.g. /set quiet 23-7 or /set quiet off")
        return "\n".join(lines)

    def set_setting(self, name: str, value: str) -> str:
        name = name.lower()
        if name == "quiet":
            v = value.lower()
            if v != "off" and not re.fullmatch(r"\d{1,2}-\d{1,2}", v):
                return "Use /set quiet 23-7 or /set quiet off"
            self.store.set("cfg:quiet", v)
            self.apply_saved_settings()
            return f"Quiet hours: {v}"
        if name not in self.SETTINGS:
            return "Unknown setting. Send /settings to see the list."
        attr, typ, desc = self.SETTINGS[name]
        try:
            val = typ(float(value))
        except ValueError:
            return f"{name} needs a number."
        self.store.set("cfg:" + name, val)
        setattr(config, attr, val)
        return f"✅ {name} = {val:g} ({desc})"

    def self_update(self) -> None:
        """Pull the latest code from GitHub and restart (the service manager starts it again)."""
        import subprocess, sys
        root = str(config.ROOT)
        if not (config.ROOT / ".git").exists():
            self.tg.send("Updates aren't connected yet. Run the one-time GitHub setup from the README, then /update works.")
            return
        def git(*a):
            return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, timeout=120)
        before = git("rev-parse", "HEAD").stdout.strip()
        r = git("fetch", "origin", "main")
        if r.returncode != 0:
            self.tg.send(f"⚠️ Update failed:\n<code>{esc((r.stdout + r.stderr).strip()[-600:])}</code>")
            return
        after = git("rev-parse", "origin/main").stdout.strip()
        if after == before:
            self.tg.send("Already on the latest version.")
            return
        git("reset", "--hard", "origin/main")  # your .env, portfolio and history are untracked, so they're kept
        log = git("log", "--format=• %s", f"{before}..{after}").stdout.strip()[:800]
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", f"{root}/requirements.txt"],
                       capture_output=True, timeout=300)
        self.tg.send("⬇️ Updated. Restarting in a few seconds…\n" + esc(log))
        sys.exit(0)  # systemd (or run.sh on Termux) restarts the engine with the new code

    # ================================================================== commands
    def handle_commands(self) -> None:
        for text in self.tg.commands():
            parts = text.split()
            cmd = parts[0].lower().split("@")[0]
            args = [p.upper().strip(",") for p in parts[1:]]
            if cmd in ("/add", "/watch") and args:
                for a in args:
                    self.store.watch_add(a)
                self.rebuild_matcher()
                self.tg.send(f"Added to portfolio: <b>{esc(', '.join(args))}</b>")
            elif cmd in ("/remove", "/unwatch") and args:
                for a in args:
                    self.store.watch_remove(a)
                self.rebuild_matcher()
                self.tg.send(f"Removed: <b>{esc(', '.join(args))}</b>")
            elif cmd == "/mute" and args:
                for a in args:
                    self.store.mute(a)
                self.tg.send(f"Muted: <b>{esc(', '.join(args))}</b>")
            elif cmd == "/unmute" and args:
                for a in args:
                    self.store.mute(a, False)
                self.tg.send(f"Unmuted: <b>{esc(', '.join(args))}</b>")
            elif cmd == "/list":
                self.tg.send(f"⭐ Portfolio: {esc(', '.join(sorted(self.portfolio())) or 'empty, use /add')}\n"
                             f"🔇 Muted: {esc(', '.join(sorted(self.store.muted())) or 'none')}\n"
                             f"Scanning {len(self.universe)} index stocks + ~{len(self.market_matcher)} companies in the news")
            elif cmd == "/top":
                c = self.top_candidates(time.time() - 24 * 3600, 10)
                self.tg.send("🎯 <b>Top candidates now</b>\n" + ("\n".join(
                    f"<b>{esc(s)}</b> {sc:.0f}/100 {ARROW[d['direction']]}" for sc, s, d in c) or "None strong enough."))
            elif cmd == "/brief":
                hours = float(args[0]) if args and args[0].replace(".", "").isdigit() else 12
                self.tg.send_long(self.brief(now() - timedelta(hours=hours), "News brief"))
            elif cmd == "/digest":
                self.send_digest("Digest") if self.store.db.execute("SELECT 1 FROM pending LIMIT 1").fetchone() \
                    else self.tg.send("Nothing held back right now.")
            elif cmd == "/setkey" and len(parts) >= 2:
                key = parts[1].strip()
                self.store.set("ai_key", key)
                self.ai.key = key
                prov = self.ai.provider
                if not prov:
                    self.tg.send("That doesn't look like a Gemini (AIza… or AQ.…) or Groq (gsk_…) key.")
                else:
                    test = self.ai.read("Government raises import duty on steel to 20%", "test", [])
                    self.tg.send((f"✅ AI reasoning on ({prov}), test read worked."
                                  if test is not None else
                                  f"⚠️ Key saved but the test call failed:\n<code>{esc(self.ai.last_error)}</code>")
                                 + "\nPlease delete your message that contains the key.")
            elif cmd == "/ai":
                self.ai.budget_left()
                self.tg.send(f"🤖 AI reasoning: {self.ai.provider or 'off (send /setkey YOUR_KEY)'}\n"
                             f"Headlines read today: {self.ai.calls_today}/{self.ai.max_per_day}"
                             + (f"\nRecent errors: {self.ai.errors}\n<code>{esc(self.ai.last_error)}</code>" if self.ai.errors else ""))
            elif cmd == "/learn":
                rows = self.store.outcome_stats()
                if not rows:
                    self.tg.send("📊 Nothing measured yet. Each signal's price is checked 1 hour and 1 trading day "
                                 "later; results appear after the first few days.")
                else:
                    out = ["📊 <b>What happened after each kind of signal</b>",
                           "(hit = moved the predicted way; avg = average move in that direction)"]
                    for r in rows[:15]:
                        h1d = f"1d: hit {r['hit1d'] / r['n1d']:.0%}, avg {r['sum1d'] / r['n1d']:+.1f}% (n={r['n1d']})" if r["n1d"] else "1d: pending"
                        h1h = f"1h: avg {r['sum1h'] / r['n1h']:+.1f}% (n={r['n1h']})" if r["n1h"] else ""
                        out.append(f"• <b>{esc(r['label'])}</b> [{esc(r['kind'])}] {h1d} {h1h}")
                    self.tg.send_long(out)
            elif cmd == "/settings":
                self.tg.send(self.settings_text())
            elif cmd == "/set" and len(parts) >= 3:
                self.tg.send(self.set_setting(parts[1], parts[2]))
            elif cmd == "/update":
                self.self_update()
            elif cmd == "/status":
                bad = {k: v for k, v in self.failures.items() if v}
                why = "\n".join(f"• {esc(k)}: {esc(self.last_error.get(k, ''))}" for k in bad)
                day = now().strftime("%Y-%m-%d")
                self.tg.send(f"✅ Running · {now().strftime('%d %b %H:%M:%S')} IST\n"
                             f"Opportunity alerts today: {self.store.count_today('opp', day)}/{config.OPP_MAX_PER_DAY}\n"
                             f"Failing sources: {esc(bad) if bad else 'none'}" + (f"\n{why}" if why else ""))
            else:
                self.tg.send("<b>Commands</b>\n/add SYM … — add to portfolio\n/remove SYM\n/list\n"
                             "/mute SYM — never alert this stock\n/unmute SYM\n"
                             "/top — strongest candidates right now\n/brief [hours] — news summary\n"
                             "/digest — send held-back items now\n/settings — see limits\n/set NAME VALUE — change a limit (e.g. /set maxopp 15)\n"
                             "/update — install the latest version\n/setkey KEY — turn on AI reasoning\n/ai — AI status\n"
                             "/learn — how past signals actually played out\n/status")

    # ================================================================== health
    def ok(self, job: str) -> None:
        if self.failures.get(job, 0) >= 5:
            self.tg.send(f"✅ {job} source is working again.")
        self.failures[job] = 0

    def fail(self, job: str, err: Exception) -> None:
        n = self.failures.get(job, 0) + 1
        self.failures[job] = n
        self.last_error[job] = f"{type(err).__name__}: {err}"[:160]
        log.warning("%s failed (%d): %s", job, n, err)
        if n == 5 and not job.startswith("news:"):  # one dead news feed isn't worth a ping
            self.tg.send(f"⚠️ <b>{job}</b> has failed 5 times in a row: {esc(str(err)[:200])}\n"
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
        p = self.portfolio()
        self.tg.send(f"🛰️ Stock Radar online · portfolio: {len(p)} stocks"
                     + ("" if p else " (send /add SYMBOL for each stock you own)")
                     + f" · max {config.OPP_MAX_PER_DAY} opportunity alerts/day. Send /help.")
        nxt = dict.fromkeys(["filings", "results", "prices", "cmds", "news", "insider", "deals", "outcomes"], 0)
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
            # Refresh the index list daily, and retry every 10 min if it failed (e.g. NSE hiccup at startup).
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
            time.sleep(1)


if __name__ == "__main__":
    Radar().run()
