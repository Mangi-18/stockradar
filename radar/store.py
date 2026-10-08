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
        self.db.commit()

    # --- watchlist ---------------------------------------------------------
    def watch_add(self, symbol: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO watch VALUES (?)", (symbol.upper(),))
        self.db.commit()

    def watch_remove(self, symbol: str) -> None:
        self.db.execute("DELETE FROM watch WHERE symbol = ?", (symbol.upper(),))
        self.db.commit()

    def watchlist(self) -> list:
        return [r[0] for r in self.db.execute("SELECT symbol FROM watch ORDER BY symbol")]

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

    # --- digest queue: things worth knowing but not worth a ping right now ---
    def queue(self, symbol: str, text: str, score: float = 0) -> None:
        self.db.execute("INSERT INTO pending VALUES (?, ?, ?, ?)", (time.time(), symbol or "", text, score))
        self.db.commit()

    def take_queue(self) -> list:
        rows = self.db.execute("SELECT ts, symbol, text, score FROM pending ORDER BY score DESC, ts").fetchall()
        self.db.execute("DELETE FROM pending")
        self.db.commit()
        return rows

    # --- muted stocks -------------------------------------------------------
    def mute(self, symbol: str, on: bool = True) -> None:
        if on:
            self.db.execute("INSERT OR IGNORE INTO muted VALUES (?)", (symbol.upper(),))
        else:
            self.db.execute("DELETE FROM muted WHERE symbol = ?", (symbol.upper(),))
        self.db.commit()

    def muted(self) -> set:
        return {r[0] for r in self.db.execute("SELECT symbol FROM muted")}

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
