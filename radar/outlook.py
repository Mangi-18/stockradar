"""Overall outlook for a stock: all of the last 24 hours' signals, combined.

One headline can be positive while three others are negative. Showing each alert's
own colour on its own is misleading, so every stock gets one combined verdict:

  * each signal gets a weight by how much it usually matters
      exchange filing > results > promoter trade > AI read > deal > news > price burst
  * AI reads are weighted by their confidence and expected size of move
  * older signals count less (half the weight every 8 hours)
  * positives and negatives are netted; 'unclear' items dilute the verdict

The verdict is a summary of the evidence, not a prediction.
"""
import re
import time

IMPACT_W = {"HIGH": 3.0, "MEDIUM": 2.0, "LOW": 1.0}
MAG_W = {"small": 1.0, "medium": 2.0, "large": 3.0}
SIGN = {"+": 1, "-": -1}
HALF_LIFE_H = 8

_AI = re.compile(r"AI: ([↑↓]) (small|medium|large) \((\d+)%\)")
_RES = re.compile(r"\(([+-]\d)\)")


def weight(e: dict) -> float:
    k, imp, h = e["kind"], e.get("impact", "LOW"), e.get("headline", "")
    if k == "ai":
        m = _AI.search(h)
        if not m:
            return 1.0
        return MAG_W[m.group(2)] * int(m.group(3)) / 100 * 1.5
    if k == "filing":
        return IMPACT_W.get(imp, 1.0)
    if k == "results":
        m = _RES.search(h)
        return float(abs(int(m.group(1)))) if m else 2.0
    if k == "insider":
        return 3.0
    if k == "deal":
        return 2.0
    if k == "news":
        return {"HIGH": 2.0, "MEDIUM": 1.0}.get(imp, 0.5)
    if k == "tgnews":
        return 0.5 * IMPACT_W.get(imp, 1.0)
    if k == "burst":
        return 2.0 if "volume" in h else 1.0
    if k == "gap":
        return 1.0
    return 0.0


def outlook(events: list, now: float = None) -> dict:
    """events: one stock's events (dicts with ts, kind, headline, tone, impact, source)."""
    now = now or time.time()
    ai_heads = [e["headline"] for e in events if e["kind"] == "ai"]
    net = total = 0.0
    pos = neg = 0
    best = {1: (0.0, None), -1: (0.0, None)}
    ai_best = {1: 0.0, -1: 0.0}
    for e in events:
        w = weight(e)
        if w <= 0:
            continue
        # The AI already read this exact headline; count the better-informed reading only
        if e["kind"] == "news" and any(e["headline"][:80] in h for h in ai_heads):
            continue
        w *= 0.5 ** (max(0.0, now - e["ts"]) / 3600 / HALF_LIFE_H)
        s = SIGN.get(e.get("tone"), 0)
        if s == 0:
            total += 0.3 * w  # unclear items make the verdict less certain
            continue
        net += s * w
        total += w
        pos += s > 0
        neg += s < 0
        if w > best[s][0]:
            best[s] = (w, e)
        if e["kind"] == "ai":
            m = _AI.search(e["headline"])
            if m and m.group(2) in ("medium", "large"):
                ai_best[s] = max(ai_best[s], int(m.group(3)) / 100)
    ratio = net / total if total else 0.0
    sign = 0
    if total == 0:
        label = None
    elif abs(net) < 1.0 or abs(ratio) < 0.3:
        label = "🟡 Mixed" if pos and neg else "🟡 Neutral"
    else:
        sign = 1 if net > 0 else -1
        word = "positive" if sign > 0 else "negative"
        dot = "🟢" if sign > 0 else "🔴"
        if abs(net) >= 6 and abs(ratio) >= 0.6:
            label = f"{dot}{dot} Strongly {word}"
        elif abs(net) >= 2.5:
            label = f"{dot} {word.capitalize()}"
        else:
            label = f"{dot} Mildly {word}"
    return {"label": label, "sign": sign, "net": net, "total": total, "ratio": ratio,
            "pos": pos, "neg": neg, "drivers": {s: e for s, (_w, e) in best.items() if e},
            "ai_conf": ai_best.get(sign, 0.0) if sign else 0.0}


def short(e: dict, n: int = 90) -> str:
    h = e["headline"]
    if e["kind"] == "ai":
        h = re.sub(r"^AI: [↑↓] \w+ \(\d+%\) — ", "", h).split(" [")[0]
    return h[:n] + ("…" if len(h) > n else "")
