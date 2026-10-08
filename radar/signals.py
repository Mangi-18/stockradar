"""Early-momentum detector.

A stock that ends the day +8% usually doesn't get there in one jump. It
starts with a burst: price moving fast AND trading volume far above its
normal pace. Catching that burst at +2% is the point of this module.

State kept per stock: the last ~10 minutes of (time, price, cumulative volume).
"""
import time
from collections import deque


class Momentum:
    def __init__(self, window_s: float = 300, min_move_pct: float = 1.5, vol_mult: float = 3.0):
        self.window_s = window_s          # look-back window (5 min)
        self.min_move = min_move_pct      # price change within the window that counts as a burst
        self.vol_mult = vol_mult          # recent volume pace vs today's average pace
        self.hist = {}                    # symbol -> deque[(t, price, cum_volume)]
        self.day_start = None             # epoch seconds of 09:15 today (set by caller)

    def update(self, symbol: str, price: float, cum_volume: float, now: float = None):
        """Feed one quote. Returns a signal dict when a burst starts, else None."""
        now = now or time.time()
        h = self.hist.setdefault(symbol, deque())
        h.append((now, price, cum_volume))
        while h and now - h[0][0] > self.window_s * 2:
            h.popleft()
        # oldest point that is at least `window_s` old ... or the oldest we have, if >= 60% of window
        past = None
        for t, p, v in h:
            if now - t <= self.window_s:
                break
            past = (t, p, v)
        if past is None:
            if h and now - h[0][0] >= self.window_s * 0.6:
                past = h[0]
            else:
                return None
        t0, p0, v0 = past
        if p0 <= 0:
            return None
        move = (price - p0) / p0 * 100
        if abs(move) < self.min_move:
            return None

        vol_ratio = None
        if self.day_start and now > self.day_start + 600 and cum_volume and v0 is not None:
            recent_rate = max(cum_volume - v0, 0) / max(now - t0, 1)
            day_rate = cum_volume / max(now - self.day_start, 1)
            vol_ratio = recent_rate / day_rate if day_rate > 0 else None
        return {"symbol": symbol, "move": move, "minutes": (now - t0) / 60,
                "vol_ratio": vol_ratio, "strong": vol_ratio is not None and vol_ratio >= self.vol_mult}

    def reset(self):
        self.hist.clear()
