"""Offline tests: no network. Run with  python -m pytest -q  (or python tests/test_radar.py)."""
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["QUIET_START"] = "0"   # no quiet hours in tests
os.environ["QUIET_END"] = "0"

from radar import main as M  # noqa: E402
from radar.bse import symbol_from_nsurl  # noqa: E402
from radar.classify import classify  # noqa: E402
from radar.results import analyse  # noqa: E402

XBRL = b"""<?xml version="1.0"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
  xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin"
  xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
 <xbrli:context id="OneD"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2026-07-01</xbrli:startDate><xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:context id="TwoD"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:context id="ThreeD"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2025-07-01</xbrli:startDate><xbrli:endDate>2025-09-30</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:context id="Seg"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>
  <xbrli:segment><xbrldi:explicitMember dimension="a">b</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
  <xbrli:period><xbrli:startDate>2026-07-01</xbrli:startDate><xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>
 <in-bse-fin:RevenueFromOperations contextRef="OneD" decimals="-5">12000000000</in-bse-fin:RevenueFromOperations>
 <in-bse-fin:RevenueFromOperations contextRef="TwoD" decimals="-5">11000000000</in-bse-fin:RevenueFromOperations>
 <in-bse-fin:RevenueFromOperations contextRef="ThreeD" decimals="-5">9500000000</in-bse-fin:RevenueFromOperations>
 <in-bse-fin:RevenueFromOperations contextRef="Seg" decimals="-5">1</in-bse-fin:RevenueFromOperations>
 <in-bse-fin:ProfitLossForPeriod contextRef="OneD" decimals="-5">1800000000</in-bse-fin:ProfitLossForPeriod>
 <in-bse-fin:ProfitLossForPeriod contextRef="TwoD" decimals="-5">1500000000</in-bse-fin:ProfitLossForPeriod>
 <in-bse-fin:ProfitLossForPeriod contextRef="ThreeD" decimals="-5">1200000000</in-bse-fin:ProfitLossForPeriod>
 <in-bse-fin:BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations contextRef="OneD" decimals="2">12.5</in-bse-fin:BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations>
</xbrli:xbrl>"""


def test_classify():
    assert classify("Outcome of Board Meeting - Financial Results for quarter ended Sep 2026")["label"] == "Quarterly results"
    assert classify("Company bags order worth Rs 450 crore from NHAI")["label"] == "Order win"
    assert classify("Receipt of Letter of Award from NTPC")["label"] == "Order win"
    assert classify("Closure of Trading Window")["impact"] == "LOW"
    assert classify("Board approves Bonus issue 1:1")["label"] == "Bonus / split"
    assert classify("Receipt of USFDA warning letter")["label"] == "USFDA update"
    assert classify("Proposal for Buyback of equity shares")["label"] == "Buyback"


def test_results():
    a = analyse(XBRL)
    assert a["ok"] and a["quarter_end"] == "2026-09-30"
    assert round(a["rev_yoy"], 1) == 26.3 and round(a["pat_yoy"], 1) == 50.0
    assert round(a["pat_qoq"], 1) == 20.0
    assert a["score"] == 4 and a["verdict"] == "Very strong", a
    assert a["eps"] == 12.5


def test_bse_symbol():
    assert symbol_from_nsurl("https://www.bseindia.com/stock-share-price/reliance-industries-ltd/reliance/500325/") == "RELIANCE"


from radar.news import analyse_headline, build_market_matcher, build_matcher, parse_rss, split_source  # noqa: E402
from radar.scoring import score_events  # noqa: E402
from radar.signals import Momentum  # noqa: E402


class FakeNSE:
    def __init__(self, rows=(), stamp=""):
        self.rows, self.stamp = list(rows), stamp

    def index_stocks(self, idx):
        return self.rows, self.stamp

    def quote(self, sym):
        raise RuntimeError("no network")


def make_radar(portfolio=()):
    M.config.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")  # fresh state per test
    r = M.Radar()
    r.sent = []
    r.tg.send = lambda m: r.sent.append(m) or True
    r.tg.send_long = lambda lines: r.sent.append("\n".join(lines))
    for p in portfolio:
        r.store.watch_add(p)
    r.rebuild_matcher()
    return r


def ev(kind, impact, tone="+", source="X", headline="h", ago=0):
    import time
    return {"ts": time.time() - ago, "symbol": "S", "kind": kind, "headline": headline,
            "tone": tone, "impact": impact, "source": source, "url": ""}


def test_score_single_headline_is_not_enough():
    assert score_events([ev("news", "HIGH")])["score"] < 70


def test_score_confluence_crosses_threshold():
    s = score_events([ev("filing", "HIGH", source="NSE"), ev("news", "HIGH", source="ET"),
                      ev("news", "MEDIUM", source="Mint"), ev("burst", "MEDIUM", headline="Price +2% in 5 min, volume 4x")])
    assert s["score"] >= 70 and s["direction"] == 1, s


def test_score_already_moved_penalty():
    evs = [ev("filing", "HIGH"), ev("news", "HIGH", source="ET"), ev("news", "HIGH", source="BS")]
    assert score_events(evs, day_change=9)["score"] < score_events(evs, day_change=1)["score"]


def test_portfolio_price_alerts_and_others_silent():
    r = make_radar(["ABC"])
    today = M.now().strftime("%d-%b-%Y")
    r.nse = FakeNSE([{"symbol": "ABC", "pChange": 11.2, "lastPrice": 110, "yearHigh": 105},
                     {"symbol": "XYZ", "pChange": -6.0, "lastPrice": 94, "yearHigh": 200}], f"{today} 11:30:00")
    r.poll_prices()
    joined = "\n".join(r.sent)
    assert "ABC" in joined and "crossed 10%" in joined and "XYZ" not in joined
    n = len(r.sent); r.poll_prices(); assert len(r.sent) == n  # no repeats
    assert r.day_change["XYZ"] == -6.0


def test_prices_skip_stale():
    r = make_radar(["OLD"])
    r.nse = FakeNSE([{"symbol": "OLD", "pChange": 9, "lastPrice": 1, "yearHigh": 9}], "01-Jan-2020 15:30:00")
    r.poll_prices()
    assert not r.sent


def test_filings_portfolio_vs_market():
    r = make_radar(["TCS"])
    r.handle_filing({"id": "n1", "symbol": "TCS", "company": "TCS", "headline": "Financial Results for Q2",
                     "category": "Financial Result Updates", "url": "http://x", "exchange": "NSE"})
    r.handle_filing({"id": "b1", "symbol": "TCS", "company": "TCS", "headline": "Results - Financial Results",
                     "category": "Result", "url": "", "exchange": "BSE"})  # duplicate on BSE
    r.handle_filing({"id": "n2", "symbol": "TINYCO", "company": "Tiny", "headline": "Bags order worth Rs 300 cr",
                     "category": "", "url": "", "exchange": "NSE"})
    assert len(r.sent) == 1 and "TCS" in r.sent[0]  # one filing alone outside the portfolio: silent


def test_opportunity_needs_agreement_and_respects_cap():
    r = make_radar()
    r.market_matcher = build_market_matcher({"GPIL": "Godawari Power and Ispat", "ZENTEC": "Zen Technologies"})
    r.handle_filing({"id": "f1", "symbol": "GPIL", "company": "", "headline": "Bags order worth Rs 900 crore from NTPC",
                     "category": "", "url": "", "exchange": "NSE"})
    assert not r.sent
    r.handle_headline("Godawari Power and Ispat bags Rs 900 crore order", "Moneycontrol", "")
    r.handle_headline("Godawari Power and Ispat wins big NTPC contract", "ET", "")
    assert not [m for m in r.sent if "Opportunity" in m]  # filing + 2 outlets = 62: digest, not a ping
    r.handle_headline("NTPC awards Godawari Power and Ispat a large order", "Business Standard", "")
    opp = [m for m in r.sent if "Opportunity" in m]
    assert len(opp) == 1 and "GPIL" in opp[0] and "/100" in opp[0], r.sent
    r.handle_headline("Godawari Power and Ispat order boosts outlook", "Mint", "")
    assert len([m for m in r.sent if "Opportunity" in m]) == 1  # once per stock per day
    M.config.OPP_MAX_PER_DAY, old = 1, M.config.OPP_MAX_PER_DAY
    try:
        r.handle_filing({"id": "f2", "symbol": "ZENTEC", "company": "", "headline": "Receipt of order worth Rs 300 crore",
                         "category": "", "url": "", "exchange": "NSE"})
        r.handle_headline("Zen Technologies bags anti-drone order", "ET", "")
        r.handle_headline("Zen Technologies wins defence contract", "BS", "")
        r.handle_headline("Zen Technologies receives anti-drone contract", "Mint", "")
        assert len([m for m in r.sent if "Opportunity" in m]) == 1  # cap reached: ZENTEC goes to digest
        assert any("ZENTEC" in q[2] for q in r.store.take_queue())
    finally:
        M.config.OPP_MAX_PER_DAY = old


def test_portfolio_news_cooldown_and_digest():
    r = make_radar(["HAL"])
    r.names["HAL"] = "Hindustan Aeronautics"; r.rebuild_matcher()
    r.handle_headline("HAL shares in focus ahead of defence expo", "ET", "")
    r.handle_headline("Analysts review HAL outlook", "Mint", "")       # routine: held back by cooldown
    r.handle_headline("HAL bags Rs 62,000 crore order", "BS", "")      # HIGH: goes through anyway
    assert len(r.sent) == 2 and "62,000" in r.sent[1]
    r.send_digest()
    assert "Analysts review HAL" in r.sent[-1]


def test_sector_news_only_pings_if_it_hits_portfolio():
    r = make_radar(["ONGC"])
    r.handle_headline("Government hikes safeguard duty on steel imports", "PIB", "")
    assert not r.sent
    r.handle_headline("Crude oil jumps as OPEC extends cuts", "Reuters", "")
    assert len(r.sent) == 1 and "Affects your" in r.sent[0] and "ONGC" in r.sent[0]


def test_preopen_portfolio_and_newsbacked_gaps():
    r = make_radar(["TCS"])
    r.store.add_filing("SMALLX", "Bags order", "HIGH", "")

    class N:
        def pre_open(self, key):
            return [{"symbol": "TCS", "pChange": 3.1, "iep": 3100}, {"symbol": "NOISE", "pChange": 12.5},
                    {"symbol": "SMALLX", "pChange": 12.0, "iep": 55}]
    r.nse = N(); r.preopen_scan()
    assert "TCS" in r.sent[-1] and "SMALLX" in r.sent[-1] and "NOISE" not in r.sent[-1]


def test_insider_and_deal_feed_scoring():
    r = make_radar()
    r.warmed_up |= {"insider", "deals"}

    class N:
        def insider_trades(self, a, b):
            return [{"symbol": "SMALLX", "acqName": "Promoter Co", "personCategory": "Promoter Group",
                     "tdpTransactionType": "Buy", "acqMode": "Market Purchase", "secAcq": "50000",
                     "secVal": "20000000", "date": "07-Oct-2026"}]

        def large_deals(self):
            return [{"kind": "Block", "symbol": "SMALLX", "clientName": "Big Fund", "buySell": "BUY",
                     "qty": "1,000,000", "watp": "300", "date": "08-Oct-2026"}]
    r.nse = N(); r.poll_insiders(); r.poll_deals()
    kinds = {e["kind"] for e in r.store.events_for("SMALLX")}
    assert kinds == {"insider", "deal"}
    assert score_events(r.store.events_for("SMALLX"))["score"] >= 40


def test_brief_and_global():
    r = make_radar(["TCS"])
    r.names["TCS"] = "Tata Consultancy Services"; r.rebuild_matcher()
    r.handle_headline("GIFT Nifty indicates flat start; Wall Street ends higher", "ET", "")
    assert not r.sent
    r.handle_headline("Wall Street crash: Dow plunges 1,200 points", "Mint", "")
    assert "Global cue" in r.sent[-1]
    r.handle_headline("TCS wins $2 bn deal from European bank", "BS", "")
    from datetime import timedelta
    text = "\n".join(r.brief(M.now() - timedelta(hours=1), "Pre-market brief"))
    assert "GIFT Nifty" in text and "TCS" in text and "Your portfolio" in text


def test_classify_early():
    assert classify("Board Meeting to consider Bonus issue and Stock Split")["label"] == "Upcoming bonus/split/buyback"
    assert classify("Board Meeting Intimation for Financial Results")["label"] == "Results date announced"
    assert classify("Outcome of Board Meeting - Financial Results for Sep 2026")["label"] == "Quarterly results"


def test_news_matching():
    m = build_matcher({"HAL": "Hindustan Aeronautics Limited", "ITC": "ITC Limited",
                       "TATAMOTORS": "Tata Motors Limited", "INFY": "Infosys Limited"})
    a = analyse_headline("HAL bags Rs 62,000 crore order for 156 helicopters", m)
    assert a["stocks"] == ["HAL"] and a["tone"] == "+" and a["themes"] == [] or "HAL" in a["stocks"]
    assert analyse_headline("Tata Motors shares jump 6% after JLR update", m)["already_moved"]
    assert not analyse_headline("Tata Motors to raise prices from November", m)["already_moved"]
    assert analyse_headline("Infosys wins $1.5 bn deal", m)["stocks"] == ["INFY"]
    assert analyse_headline("Brands built with italic fonts", m)["stocks"] == []  # 'itc' inside a word
    th = analyse_headline("Government hikes safeguard duty on steel imports to 15%", m)["themes"]
    assert th and th[0][0] == "Steel / metals"


def test_rss_parse():
    xml = b"""<rss><channel><item><title>RBI keeps repo rate unchanged</title><link>http://a</link></item>
    <item><title> Second  </title><guid>g2</guid></item></channel></rss>"""
    items = parse_rss(xml)
    assert items[0]["title"] == "RBI keeps repo rate unchanged" and items[1]["id"] == "g2"


def test_momentum_burst():
    mo = Momentum(window_s=300, min_move_pct=1.5, vol_mult=3)
    t0 = 1_000_000.0
    mo.day_start = t0 - 3600            # an hour into the session
    vol = 360_000.0                     # 100 shares/s average pace so far
    sig = None
    for i in range(11):                 # 5 minutes of 30 s quotes, price +2%, volume 5x pace
        vol += 500 * 30
        sig = mo.update("ABC", 100 + 0.2 * i, vol, t0 + 30 * i)
    assert sig and sig["move"] > 1.5 and sig["strong"], sig
    quiet = Momentum()
    quiet.day_start = t0 - 3600
    for i in range(11):
        s2 = quiet.update("XYZ", 100 + 0.01 * i, 1000 + i, t0 + 30 * i)
    assert s2 is None


def test_market_matcher():
    mm = build_market_matcher({"SENCOGOLD": "Senco Gold Limited", "INDIANB": "Indian Bank",
                               "GPIL": "Godawari Power & Ispat limited"})
    syms = [s for s, _ in mm]
    assert "SENCOGOLD" in syms and "INDIANB" not in syms  # 'Indian Bank' is too generic
    a = analyse_headline("Senco Gold posts record H1 revenue", [], mm)
    assert a["others"] == ["SENCOGOLD"]
    assert split_source("Senco Gold posts record revenue - Moneycontrol", "g") == ("Senco Gold posts record revenue", "Moneycontrol")


def test_settings_commands_persist():
    r = make_radar()
    assert "maxopp = 15" in r.set_setting("maxopp", "15")
    assert M.config.OPP_MAX_PER_DAY == 15
    r.set_setting("quiet", "off")
    assert M.config.QUIET_START == M.config.QUIET_END
    r2 = M.Radar.__new__(M.Radar); r2.store = r.store; M.config.OPP_MAX_PER_DAY = 5
    r2.apply_saved_settings()
    assert M.config.OPP_MAX_PER_DAY == 15  # survives a restart
    assert "Unknown" in r.set_setting("nonsense", "1")
    M.config.OPP_MAX_PER_DAY = 5; M.config.QUIET_START = M.config.QUIET_END = 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
