"""Read a quarterly-results XBRL filing and turn it into numbers + a transparent score.

XBRL is the machine-readable version of the results PDF. Every number ("fact")
points to a "context" that says which period it belongs to, so we can find
this quarter, the same quarter last year (YoY) and the previous quarter (QoQ)
without any AI.
"""
import xml.etree.ElementTree as ET
from datetime import date, datetime

REVENUE_TAGS = ["RevenueFromOperations", "InterestEarned", "Revenue", "TotalRevenueFromOperations",
                "Income", "TotalIncome"]
PROFIT_TAGS = ["ProfitLossForPeriod", "ProfitLossForThePeriod", "NetProfitLossForThePeriod",
               "ProfitLossFromContinuingOperations", "ProfitLossForPeriodFromContinuingOperations"]
EPS_TAGS = ["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "BasicEarningsLossPerShareFromContinuingOperations", "BasicEarningsPerShare"]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _date(s: str):
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def parse_xbrl(xml_bytes: bytes) -> dict:
    """Return {"periods": {ctx_id: (start, end)}, "facts": {name: {ctx_id: value}}}.
    Contexts that carry dimensions (segment breakdowns) are skipped."""
    root = ET.fromstring(xml_bytes)
    periods = {}
    for ctx in root.iter():
        if _local(ctx.tag) != "context":
            continue
        if any(_local(e.tag) in ("segment", "scenario") for e in ctx.iter()):
            continue
        start = end = None
        for e in ctx.iter():
            n = _local(e.tag)
            if n == "startDate":
                start = _date(e.text)
            elif n == "endDate":
                end = _date(e.text)
        if start and end:
            periods[ctx.get("id")] = (start, end)

    facts = {}
    for el in root.iter():
        ref = el.get("contextRef")
        if ref not in periods or el.text is None:
            continue
        try:
            val = float(el.text.strip())
        except ValueError:
            continue
        facts.setdefault(_local(el.tag), {})[ref] = val
    return {"periods": periods, "facts": facts}


def _quarter_contexts(periods: dict):
    """Pick current quarter, same quarter last year, previous quarter."""
    q = {cid: (s, e) for cid, (s, e) in periods.items() if 75 <= (e - s).days <= 100}
    if not q:
        return None, None, None
    cur = max(q, key=lambda c: q[c][1])
    cur_end = q[cur][1]

    def near(target: date, tol: int = 25):
        best = [c for c in q if abs((q[c][1] - target).days) <= tol and c != cur]
        return min(best, key=lambda c: abs((q[c][1] - target).days)) if best else None

    yoy = near(date(cur_end.year - 1, cur_end.month, min(cur_end.day, 28)), 30)
    qoq = near(date.fromordinal(cur_end.toordinal() - 91))
    return cur, yoy, qoq


def _pick(facts: dict, tags: list, ctx):
    if ctx is None:
        return None
    for t in tags:
        if t in facts and ctx in facts[t]:
            return facts[t][ctx]
    return None


def _growth(new, old):
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old) * 100


def analyse(xml_bytes: bytes) -> dict:
    parsed = parse_xbrl(xml_bytes)
    facts, periods = parsed["facts"], parsed["periods"]
    cur, yoy, qoq = _quarter_contexts(periods)
    if cur is None:
        return {"ok": False, "reason": "No quarterly period found in filing"}

    rev = {k: _pick(facts, REVENUE_TAGS, c) for k, c in (("cur", cur), ("yoy", yoy), ("qoq", qoq))}
    pat = {k: _pick(facts, PROFIT_TAGS, c) for k, c in (("cur", cur), ("yoy", yoy), ("qoq", qoq))}
    eps = _pick(facts, EPS_TAGS, cur)

    out = {
        "ok": rev["cur"] is not None or pat["cur"] is not None,
        "quarter_end": periods[cur][1].isoformat(),
        "revenue": rev, "profit": pat, "eps": eps,
        "rev_yoy": _growth(rev["cur"], rev["yoy"]), "rev_qoq": _growth(rev["cur"], rev["qoq"]),
        "pat_yoy": _growth(pat["cur"], pat["yoy"]), "pat_qoq": _growth(pat["cur"], pat["qoq"]),
    }
    out.update(score(out))
    return out


def score(r: dict) -> dict:
    """Heuristic score from -4 to +4 with the reasons spelled out.
    It compares against LAST YEAR, not against analyst estimates, and it does
    not predict the price. A big score means 'unusually strong/weak quarter'."""
    pts, why = 0, []
    p_now, p_then = r["profit"]["cur"], r["profit"]["yoy"]
    g = r["pat_yoy"]
    if p_now is not None and p_then is not None:
        if p_then <= 0 < p_now:
            pts += 2; why.append("turned profitable vs loss last year")
        elif p_now <= 0 < p_then:
            pts -= 2; why.append("swung to a loss vs profit last year")
        elif p_now < 0 and p_then < 0:
            if p_now < p_then:
                pts -= 1; why.append("loss widened YoY")
            else:
                why.append("loss narrowed YoY")
        elif g is not None:
            if g >= 30: pts += 2; why.append(f"profit up {g:.0f}% YoY")
            elif g >= 10: pts += 1; why.append(f"profit up {g:.0f}% YoY")
            elif g <= -30: pts -= 2; why.append(f"profit down {abs(g):.0f}% YoY")
            elif g <= -10: pts -= 1; why.append(f"profit down {abs(g):.0f}% YoY")
            else: why.append(f"profit flat ({g:+.0f}% YoY)")

    rg = r["rev_yoy"]
    if rg is not None:
        if rg >= 20: pts += 1; why.append(f"revenue up {rg:.0f}% YoY")
        elif rg <= -10: pts -= 1; why.append(f"revenue down {abs(rg):.0f}% YoY")
        else: why.append(f"revenue {rg:+.0f}% YoY")

    rev, pat = r["revenue"], r["profit"]
    if all(v not in (None, 0) for v in (rev["cur"], rev["yoy"])) and None not in (pat["cur"], pat["yoy"]):
        m_now, m_then = pat["cur"] / rev["cur"] * 100, pat["yoy"] / rev["yoy"] * 100
        d = m_now - m_then
        if d >= 2: pts += 1; why.append(f"net margin up {d:.1f} pts to {m_now:.1f}%")
        elif d <= -2: pts -= 1; why.append(f"net margin down {abs(d):.1f} pts to {m_now:.1f}%")

    pts = max(-4, min(4, pts))
    label = ("Very strong" if pts >= 3 else "Strong" if pts >= 1 else "Mixed" if pts == 0
             else "Weak" if pts >= -2 else "Very weak")
    return {"score": pts, "verdict": label, "reasons": why}


def crore(v):
    """XBRL values are in rupees; Indian results are read in crore (1 crore = 10^7)."""
    if v is None:
        return "n/a"
    return f"₹{v / 1e7:,.1f} cr"
