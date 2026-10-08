"""Decide how price-moving a filing is likely to be, from its headline/category text.

This is rule-based on purpose: it is instant, free, and you can read exactly
why something was flagged. Order matters: the first matching rule wins.
"""
import re

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"

# (regex, label, impact, tone)   tone: "+" usually positive, "-" usually negative, "?" depends
RULES = [
    # Noise first, so it never gets promoted by a later keyword.
    (r"trading window|loss of share cert|duplicate share|newspaper (publication|advertisement)"
     r"|certificate under reg|reg(ulation)?\.? ?74\(5\)|compliance certificate"
     r"|change in (registered office|address)|closure of trading", "Routine", LOW, "?"),

    # Board meeting intimations: announced days ahead, so they are EARLY signals.
    (r"board meeting.*(bonus|stock split|sub-?division|buy-?back)|(bonus|split|buy-?back).*board meeting",
     "Upcoming bonus/split/buyback", HIGH, "+"),
    (r"board meeting.*(fund ?rais|qip|preferential|rights issue)", "Upcoming fund raising", MEDIUM, "?"),
    (r"(intimation|prior intimation|to consider).*(results?)|board meeting.*intimation",
     "Results date announced", MEDIUM, "?"),

    (r"financial results?|quarterly results?|unaudited.*results?|audited.*results?",
     "Quarterly results", HIGH, "?"),
    (r"insolvency|nclt|default|fraud|forensic audit|resignation of (statutory )?auditor",
     "Red flag", HIGH, "-"),
    (r"\b(search|raid|seizure)\b|enforcement directorate|show cause|sebi order|penalt",
     "Regulatory action", HIGH, "-"),
    (r"usfda|us fda|warning letter|import alert|form 483|eir\b",
     "USFDA update", HIGH, "?"),
    (r"bonus|stock split|sub-?division", "Bonus / split", HIGH, "+"),
    (r"buy-?back", "Buyback", HIGH, "+"),
    (r"(bags?|receiv|receipt|secur|award|win)\w*.*\b(order|contract)|letter of (award|intent)"
     r"|\bloa\b|work order|purchase order", "Order win", HIGH, "+"),
    (r"acqui|merger|amalgamation|demerger|scheme of arrangement|open offer|delist",
     "M&A / restructuring", HIGH, "?"),
    (r"\bqip\b|preferential (issue|allotment)|rights issue|fund ?rais|warrants",
     "Fund raising", MEDIUM, "?"),
    (r"credit rating|rating (upgrade|downgrade|reaffirm)|\bcrisil\b|\bicra\b|\bcare ratings\b",
     "Credit rating", MEDIUM, "?"),
    (r"resign|cessation|appointment of (md|ceo|cfo|managing director|chief)",
     "Top management change", MEDIUM, "?"),
    (r"approval|approved|licen[cs]e|patent", "Approval / licence", MEDIUM, "+"),
    (r"(fire|accident|shutdown|strike|lockout)", "Operational disruption", MEDIUM, "-"),
    (r"board meeting.*(result|dividend|bonus|split|buyback|fund)", "Board meeting", MEDIUM, "?"),
    (r"dividend", "Dividend", MEDIUM, "+"),
    (r"business update|operational update|sales (number|volume)|production|disbursement",
     "Business update", MEDIUM, "?"),
    (r"joint venture|\bjv\b|\bmou\b|partnership|collaborat|agreement", "Partnership / MoU", MEDIUM, "+"),
    (r"investor presentation|analyst|earnings call|con-?call|investor meet", "Investor meet", LOW, "?"),
]
_COMPILED = [(re.compile(p, re.I), label, impact, tone) for p, label, impact, tone in RULES]


def classify(headline: str, category: str = "") -> dict:
    text = f"{category} {headline}"
    for rx, label, impact, tone in _COMPILED:
        if rx.search(text):
            return {"label": label, "impact": impact, "tone": tone}
    return {"label": "Other", "impact": LOW, "tone": "?"}
