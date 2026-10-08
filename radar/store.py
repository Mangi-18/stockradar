"""Small SQLite store: what we've already seen/alerted, extra watchlist, recent filings."""
import sqlite3
import time


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts REAL);
            CREATE TABLE IF NOT EXISTS watch (symbol TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS filings (
                symbol TEXT, ts REAL, headline TEXT, impact TEXT, url TEXT);
            CREATE INDEX IF NOT EXISTS filings_sym ON filings(symbol, ts);
            CREATE TABLE IF NOT EXISTS events (
                ts REAL, symbol TEXT, kind TEXT, headline TEXT, tone TEXT,
                impact TEXT, source TEXT, url TEXT);
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
            CREATE INDEX IF NOT EXISTS events_sym ON events(symbol, ts);
            CREATE TABLE IF NOT EXISTS pending (ts REAL, symbol TEXT, text TEXT, score REAL);
            CREATE TABLE IF NOT EXISTS muted (symbol TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS users (chat TEXT PRIMARY KEY, name TEXT, joined REAL, status TEXT);
            CREATE TABLE IF NOT EXISTS uwatch (chat TEXT, symbol TEXT, PRIMARY KEY (chat, symbol));
            CREATE TABLE IF NOT EXISTS umuted (chat TEXT, symbol TEXT, PRIMARY KEY (chat, symbol));
            CREATE TABLE IF NOT EXISTS upending (chat TEXT, ts REAL, symbol TEXT, text TEXT, score REAL);
            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT, kind TEXT,
                label TEXT, direction INTEGER, p0 REAL, p1h REAL, p1d REAL, t0 REAL);
        """)
        self.db.commit()

    # --- dedup -------------------------------------------------------------
    def first_time(self, key: str) -> bool:
        """True the first time a key is seen, False afterwards (atomic)."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO seen(key, ts) VALUES (?, ?)", (key, time.time()))
        self.db.commit()
        return cur.rowcount == 1

    def prune(self, days: int = 10) -> None:
        cutoff = time.time() - days * 86400
        self.db.execute("DELETE FROM seen WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM filings WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM pending WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM upending WHERE ts < ?", (cutoff,))
        self.db.commit()

    # --- users ---------------------------------------------------------------
    def user_add(self, chat: str, name: str) -> bool:
        """Registers a new user. True if they are new."""
        cur = self.db.execute("INSERT OR IGNORE INTO users VALUES (?, ?, ?, 'active')", (str(chat), name, time.time()))
        self.db.commit()
        return cur.rowcount == 1

    def user_status(self, chat: str):
        row = self.db.execute("SELECT status FROM users WHERE chat = ?", (str(chat),)).fetchone()
        return row[0] if row else None

    def user_set_status(self, chat: str, status: str) -> None:
        self.db.execute("UPDATE users SET status = ? WHERE chat = ?", (status, str(chat)))
        self.db.commit()

    def users(self, status: str = "active") -> list:
        return [(c, n) for c, n in self.db.execute(
            "SELECT chat, name FROM users WHERE status = ? ORDER BY joined", (status,))]

    def migrate_single_user(self, owner: str) -> None:
        """Older versions had one portfolio/mute list/queue; they belong to the owner."""
        if self.get("migrated_multiuser"):
            return
        self.db.execute("INSERT OR IGNORE INTO uwatch SELECT ?, symbol FROM watch", (owner,))
        self.db.execute("INSERT OR IGNORE INTO umuted SELECT ?, symbol FROM muted", (owner,))
        self.db.execute("INSERT INTO upending SELECT ?, ts, symbol, text, score FROM pending", (owner,))
        self.db.execute("DELETE FROM pending")
        for k, v in self.db.execute("SELECT k, v FROM kv WHERE k LIKE 'cfg:%'").fetchall():
            if k.count(":") == 1:  # old global setting -> owner's setting
                self.db.execute("INSERT OR IGNORE INTO kv VALUES (?, ?)", (f"cfg:{owner}:{k[4:]}", v))
        self.db.commit()
        self.set("migrated_multiuser", "1")

    # --- per-user portfolio / mutes ---------------------------------------------
    def watch_add(self, symbol: str, chat: str = "") -> None:
        self.db.execute("INSERT OR IGNORE INTO uwatch VALUES (?, ?)", (str(chat), symbol.upper()))
        self.db.commit()

    def watch_remove(self, symbol: str, chat: str = "") -> None:
        self.db.execute("DELETE FROM uwatch WHERE chat = ? AND symbol = ?", (str(chat), symbol.upper()))
        self.db.commit()

    def watchlist(self, chat: str = "") -> list:
        return [r[0] for r in self.db.execute("SELECT symbol FROM uwatch WHERE chat = ? ORDER BY symbol", (str(chat),))]

    # --- key/value ---------------------------------------------------------
    def get(self, k: str, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return row[0] if row else default

    def set(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (k, str(v)))
        self.db.commit()

    # --- recent filings (used to explain price moves) ---------------------
    def add_filing(self, symbol: str, headline: str, impact: str, url: str) -> None:
        self.db.execute("INSERT INTO filings VALUES (?, ?, ?, ?, ?)",
                        (symbol, time.time(), headline, impact, url))
        self.db.commit()

    def recent_filings(self, symbol: str, hours: float = 24) -> list:
        cutoff = time.time() - hours * 3600
        return self.db.execute(
            "SELECT headline, impact, url FROM filings WHERE symbol = ? AND ts > ? "
            "ORDER BY ts DESC LIMIT 3", (symbol, cutoff)).fetchall()

    # --- event log (everything that happened; feeds the briefs and buzz) ---
    def log_event(self, symbol: str, kind: str, headline: str, tone: str = "?",
                  impact: str = "LOW", source: str = "", url: str = "", ts: float = None) -> None:
        self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (ts or time.time(), symbol or "", kind, headline[:300], tone, impact, source, url))
        self.db.commit()

    def events_since(self, ts: float) -> list:
        cur = self.db.execute(
            "SELECT ts, symbol, kind, headline, tone, impact, source, url FROM events "
            "WHERE ts >= ? ORDER BY ts", (ts,))
        cols = ["ts", "symbol", "kind", "headline", "tone", "impact", "source", "url"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def news_sources(self, symbol: str, hours: float = 2) -> int:
        """How many different outlets wrote about this stock recently (news 'buzz')."""
        cutoff = time.time() - hours * 3600
        return self.db.execute(
            "SELECT COUNT(DISTINCT source) FROM events WHERE symbol = ? AND kind = 'news' AND ts > ?",
            (symbol, cutoff)).fetchone()[0]

    def events_for(self, symbol: str, hours: float = 24) -> list:
        cutoff = time.time() - hours * 3600
        cur = self.db.execute(
            "SELECT ts, symbol, kind, headline, tone, impact, source, url FROM events "
            "WHERE symbol = ? AND ts > ? ORDER BY ts", (symbol, cutoff))
        cols = ["ts", "symbol", "kind", "headline", "tone", "impact", "source", "url"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # --- digest queue (per user): worth knowing but not worth a ping right now ---
    def queue(self, symbol: str, text: str, score: float = 0, chat: str = "") -> None:
        self.db.execute("INSERT INTO upending VALUES (?, ?, ?, ?, ?)", (str(chat), time.time(), symbol or "", text, score))
        self.db.commit()

    def take_queue(self, chat: str = "") -> list:
        rows = self.db.execute("SELECT ts, symbol, text, score FROM upending WHERE chat = ? "
                               "ORDER BY score DESC, ts", (str(chat),)).fetchall()
        self.db.execute("DELETE FROM upending WHERE chat = ?", (str(chat),))
        self.db.commit()
        return rows

    def has_queue(self, chat: str = "") -> bool:
        return self.db.execute("SELECT 1 FROM upending WHERE chat = ? LIMIT 1", (str(chat),)).fetchone() is not None

    def mute(self, symbol: str, on: bool = True, chat: str = "") -> None:
        if on:
            self.db.execute("INSERT OR IGNORE INTO umuted VALUES (?, ?)", (str(chat), symbol.upper()))
        else:
            self.db.execute("DELETE FROM umuted WHERE chat = ? AND symbol = ?", (str(chat), symbol.upper()))
        self.db.commit()

    def muted(self, chat: str = "") -> set:
        return {r[0] for r in self.db.execute("SELECT symbol FROM umuted WHERE chat = ?", (str(chat),))}

    def count_today(self, prefix: str, day: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM seen WHERE key LIKE ?", (f"{prefix}:{day}:%",)).fetchone()[0]

    # --- learning: what happened to the price after each signal ---------------
    def track(self, symbol: str, kind: str, label: str, direction: int) -> None:
        self.db.execute("INSERT INTO outcomes (ts, symbol, kind, label, direction) VALUES (?, ?, ?, ?, ?)",
                        (time.time(), symbol, kind, label, direction))
        self.db.commit()

    def outcomes_pending(self, limit: int = 30) -> list:
        """Rows still missing a price, oldest first (kept 5 days)."""
        cutoff = time.time() - 5 * 86400
        return self.db.execute(
            "SELECT id, ts, symbol, p0, p1h, p1d, t0 FROM outcomes WHERE ts > ? AND "
            "(p0 IS NULL OR p1h IS NULL OR p1d IS NULL) ORDER BY ts LIMIT ?", (cutoff, limit)).fetchall()

    def outcome_set(self, oid: int, field: str, value: float) -> None:
        assert field in ("p0", "p1h", "p1d", "t0")
        self.db.execute(f"UPDATE outcomes SET {field} = ? WHERE id = ?", (value, oid))
        self.db.commit()

    def outcome_stats(self, min_n: int = 1) -> list:
        """Per (kind, label): count, hit rate and average move IN THE PREDICTED DIRECTION."""
        rows = self.db.execute(
            "SELECT kind, label, direction, p0, p1h, p1d FROM outcomes WHERE p0 > 0 AND direction != 0").fetchall()
        agg = {}
        for kind, label, d, p0, p1h, p1d in rows:
            a = agg.setdefault((kind, label), {"n1h": 0, "hit1h": 0, "sum1h": 0.0, "n1d": 0, "hit1d": 0, "sum1d": 0.0})
            for p, tag in ((p1h, "1h"), (p1d, "1d")):
                if p:
                    move = (p - p0) / p0 * 100 * d
                    a["n" + tag] += 1
                    a["sum" + tag] += move
                    a["hit" + tag] += move > 0
        out = []
        for (kind, label), a in agg.items():
            if a["n1d"] >= min_n or a["n1h"] >= min_n:
                out.append({"kind": kind, "label": label, **a})
        return sorted(out, key=lambda x: -(x["n1d"] + x["n1h"]))

    def news_by_source(self, symbol: str, hours: float) -> list:
        """Latest headline per outlet for a stock in the last `hours` (newest first)."""
        cutoff = time.time() - hours * 3600
        rows = self.db.execute(
            "SELECT source, headline, MAX(ts) FROM events WHERE symbol = ? AND kind = 'news' AND ts > ? "
            "GROUP BY source ORDER BY MAX(ts) DESC", (symbol, cutoff)).fetchall()
        return [(src, h) for src, h, _ in rows]

    def has_seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone() is not None
