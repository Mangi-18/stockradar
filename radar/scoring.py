"""Opportunity score: how strong is the evidence that a stock OUTSIDE your
portfolio is about to move?

One headline is weak evidence. What separates the few real opportunities from
the hundreds of daily headlines is *agreement between independent signals*:

  - a hard fact from the exchange (order win, bonus, buyback, results) beats a headline
  - several different outlets covering it beats one
  - different KINDS of signals lining up (news + filing + promoter buying +
    a volume surge) beats any one of them alone
  - and it is only an opportunity if the price has NOT already moved

The score is 0-100. It ranks the strength of evidence; it is not a probability
and not a guarantee. Every point is listed in the alert so you can judge it.
"""
import time

KIND_BASE = {  # strongest single piece of evidence
    ("filing", "HIGH"): (40, "exchange filing"),
    ("results", "HIGH"): (40, "results far from last year"),
    ("insider", "HIGH"): (35, "promoter buying/selling"),
    ("news", "HIGH"): (30, "high-impact news"),
    ("deal", "MEDIUM"): (25, "bulk/block deal"),
    ("filing", "MEDIUM"): (20, "exchange filing"),
    ("gap", "MEDIUM"): (15, "pre-open gap"),
    ("news", "MEDIUM"): (15, "news"),
    ("burst", "MEDIUM"): (10, "price burst"),
}
SIGNAL_KINDS = {"filing", "results", "insider", "news", "deal", "burst", "gap"}
SIGN = {"+": 1, "-": -1, "?": 0}


def score_events(events: list, day_change: float = None, now: float = None) -> dict:
    """events: this stock's events from the last 24 h (dicts from Store.events_since).
    day_change: today's % change if known (to detect 'already moved')."""
    now = now or time.time()
    ev = [e for e in events if e["kind"] in SIGNAL_KINDS]
    if not ev:
        return {"score": 0, "direction": 0, "reasons": []}
    reasons = []

    base, why = 0, ""
    for e in ev:
        b, w = KIND_BASE.get((e["kind"], e["impact"]), (5, e["kind"]))
        if b > base:
            base, why = b, w
    reasons.append(f"+{base} {why}")
    score = base

    recent = [e for e in ev if now - e["ts"] <= 6 * 3600]
    sources = {e["source"] for e in recent if e["kind"] == "news" and e["source"]}
    if len(sources) > 1:
        bonus = min(30, 10 * (len(sources) - 1))
        score += bonus
        reasons.append(f"+{bonus} covered by {len(sources)} outlets")

    kinds = {e["kind"] for e in ev}
    if len(kinds) > 1:
        bonus = min(24, 12 * (len(kinds) - 1))
        score += bonus
        reasons.append(f"+{bonus} {len(kinds)} independent signal types agree ({', '.join(sorted(kinds))})")

    bursts = [e for e in ev if e["kind"] == "burst"]
    if any("volume" in e["headline"] for e in bursts):
        score += 15
        reasons.append("+15 price moving on heavy volume")

    tones = [SIGN.get(e["tone"], 0) for e in ev]
    direction = 1 if sum(tones) > 0 else -1 if sum(tones) < 0 else 0
    if any(t > 0 for t in tones) and any(t < 0 for t in tones):
        score -= 15
        reasons.append("-15 mixed signals (some positive, some negative)")
    if direction == 0:
        score -= 10
        reasons.append("-10 direction unclear")

    already = any("already moving" in e["headline"] for e in ev)
    if day_change is not None and direction and day_change * direction >= 7:
        already = True
    if already:
        score -= 25
        reasons.append("-25 price has already moved a lot")

    return {"score": max(0, min(100, score)), "direction": direction, "reasons": reasons}
