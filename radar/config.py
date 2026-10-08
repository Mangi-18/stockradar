"""Settings, read from a .env file next to the project (or real environment variables)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env_file(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Indices whose stocks form the core universe (NSE index names).
INDICES = [s.strip() for s in os.environ.get(
    "INDICES", "NIFTY 50,NIFTY MIDCAP 100").split(",") if s.strip()]

# YOUR PORTFOLIO (comma separated NSE symbols). Every relevant item about these
# reaches you. You can also add/remove from Telegram with /add and /remove.
EXTRA_WATCHLIST = [s.strip().upper() for s in (os.environ.get("PORTFOLIO") or os.environ.get(
    "EXTRA_WATCHLIST", "")).split(",") if s.strip()]

# Price move alert levels in percent (each level alerts once per day per direction).
MOVE_LEVELS = sorted(float(x) for x in os.environ.get(
    "MOVE_LEVELS", "5,10").split(",") if x.strip())


# Polling intervals in seconds.
FILINGS_POLL_FAST = _float("FILINGS_POLL_FAST", 15)   # 07:00-22:00 IST on weekdays
FILINGS_POLL_SLOW = _float("FILINGS_POLL_SLOW", 30)   # nights and weekends: still fast
TELEGRAM_POLL = _float("TELEGRAM_POLL", 5)

USE_BSE = _bool("USE_BSE", True)
DB_PATH = os.environ.get("DB_PATH", str(ROOT / "radar.db"))

# --- Early signals -------------------------------------------------------
PRICES_POLL = _float("PRICES_POLL", 30)               # faster polling for momentum bursts
MOMENTUM_MOVE = _float("MOMENTUM_MOVE", 1.5)          # % move within MOMENTUM_WINDOW that counts as a burst
MOMENTUM_WINDOW_MIN = _float("MOMENTUM_WINDOW_MIN", 5)
MOMENTUM_VOL_MULT = _float("MOMENTUM_VOL_MULT", 3)    # recent volume pace vs today's average
PREOPEN_MIN_GAP = _float("PREOPEN_MIN_GAP", 2)        # % gap for your universe in the pre-open alert
PREOPEN_MIN_GAP_ALL = _float("PREOPEN_MIN_GAP_ALL", 8)  # % gap for any other stock
INSIDER_MIN_CR = _float("INSIDER_MIN_CR", 0.5)        # promoter buy/sell value (₹ crore) worth an alert
DEAL_MIN_CR = _float("DEAL_MIN_CR", 10)               # bulk/block deal value (₹ crore) worth an alert
NEWS_POLL = _float("NEWS_POLL", 30)
NEWS_FEEDS = [s.strip() for s in os.environ.get("NEWS_FEEDS", "").split(",") if s.strip()]
NEWS_THEMES = _bool("NEWS_THEMES", True)              # sector/policy news (defence, crude, RBI...)

# --- Noise control ----------------------------------------------------------
OPP_MIN_SCORE = _float("OPP_MIN_SCORE", 70)          # 0-100: how strong the evidence must be to ping you
OPP_MAX_PER_DAY = int(_float("OPP_MAX_PER_DAY", 5))  # hard cap on opportunity alerts per day
OPP_DIGEST_SCORE = _float("OPP_DIGEST_SCORE", 40)    # weaker candidates go to the digest instead
STOCK_COOLDOWN_MIN = _float("STOCK_COOLDOWN_MIN", 60)  # max one routine ping per portfolio stock per hour
QUIET_START = int(_float("QUIET_START", 23))         # quiet hours (IST): only urgent portfolio alerts
QUIET_END = int(_float("QUIET_END", 7))
DIGEST_TIMES = [t.strip() for t in os.environ.get("DIGEST_TIMES", "12:30,15:45,20:30").split(",") if t.strip()]
