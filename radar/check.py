"""One-shot health check: hits every data source once and sends a Telegram test.
Run this first on your server:  python -m radar.check
"""
from . import config
from .bse import BSE
from .nse import NSE
from .telegram import Telegram


def step(name, fn):
    try:
        print(f"OK   {name}: {fn()}")
    except Exception as e:
        print(f"FAIL {name}: {type(e).__name__}: {e}")


def main():
    nse = NSE()
    step("NSE announcements", lambda: f"{len(nse.announcements())} items")
    step("NSE quarterly results", lambda: f"{len(nse.financial_results())} items")
    for idx in config.INDICES:
        step(f"NSE prices {idx}", lambda idx=idx: "{} stocks, data time {}".format(*map(
            lambda x: len(x) if isinstance(x, list) else x, nse.index_stocks(idx))))
    from datetime import date, timedelta
    step("NSE pre-open", lambda: f"{len(nse.pre_open('NIFTY'))} rows (empty outside 9:00-9:15 is normal)")
    step("NSE bulk/block deals", lambda: f"{len(nse.large_deals())} deals")
    step("NSE promoter/insider trades", lambda: f"{len(nse.insider_trades(date.today() - timedelta(days=3), date.today()))} rows")
    from .news import DEFAULT_FEEDS, NewsFeeds
    for url, items in NewsFeeds(config.NEWS_FEEDS or DEFAULT_FEEDS).fetch():
        if isinstance(items, Exception):
            print(f"FAIL news {url}: {items}")
        else:
            print(f"OK   news {url}: {len(items)} headlines")
    if config.USE_BSE:
        step("BSE announcements", lambda: f"{len(BSE().announcements())} items")
    tg = Telegram(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
    step("Telegram", lambda: "message sent" if tg.send("🛰️ Stock Radar test message") else
         "not sent (check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")


if __name__ == "__main__":
    main()
