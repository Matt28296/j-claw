"""interpretation_risk.py — deterministic, $0 heuristic for Creative Director routing.

score_interpretation_risk(intent) returns a float in [0.0, 1.0] that quantifies how
likely the intent is to be misinterpreted by a cheaper model (Codex vs Sonnet). High-
risk intents are routed to Sonnet as the CD primary; low-risk intents keep the default
Codex-first path.

The score is PURELY deterministic (keyword matching), requires no LLM, and is fully
unit-testable. The threshold is tunable via INTERPRETATION_RISK_THRESHOLD env var.

Signal categories and caps (additive, each capped independently):
  - Ambiguity signals   (+0.15 each, cap 0.30): vague nouns, missing audience, missing
                        success criteria
  - Novelty signals     (+0.10 each, cap 0.30): unusual genre combos, novel/experimental
                        tags, unusual interaction models
  - Constraint-load     (+0.10 each, cap 0.40): auth/users, persistence/storage,
                        real-time, integrations, compliance
"""
from __future__ import annotations
import os
import re

# Tune via INTERPRETATION_RISK_THRESHOLD env var (float 0.0–1.0).
# Intents scoring at or above this threshold are routed to Sonnet as CD primary.
HIGH_RISK_THRESHOLD: float = float(os.getenv("INTERPRETATION_RISK_THRESHOLD", "0.55"))

# ── Ambiguity signals (+0.15 each, capped at 0.30) ───────────────────────────
# These indicate the intent is too vague for a less-capable model to interpret
# reliably without guessing at the unstated requirements.
_AMBIGUITY_SIGNALS: list[str] = [
    r"\bsomething\b",           # "build me something like..."
    r"\bkinda\b",               # "kinda like X but..."
    r"\bsort of\b",             # "sort of a game"
    r"\blike\s+\w+\s+but\b",   # "like X but Y" (comparing to reference without spec)
    r"\bfor\s+users\b(?!\s+who\b)",  # "for users" without a "who" qualifier
    r"\bsome\s+kind\s+of\b",   # "some kind of app"
    r"\bnot sure\b",            # "not sure what stack"
    r"\bmaybe\b",               # "maybe with auth"
    r"\bpossibly\b",            # "possibly real-time"
]

# Missing-success-criteria check: presence of "when X works" / "should X" / "must X"
# phrasing signals the intent HAS criteria. Absence adds ambiguity.
_SUCCESS_CRITERIA_PATTERNS: list[str] = [
    r"\bwhen\s+\w+\s+works\b",
    r"\bshould\s+\w+\b",
    r"\bmust\s+\w+\b",
    r"\bneed\s+to\s+\w+\b",
    r"\baccept[s]?\s+criteria\b",
    r"\bgoal\s+is\b",
    r"\bpass\s+when\b",
    r"\bsuccess\s+means\b",
]

# ── Novelty signals (+0.10 each, capped at 0.30) ──────────────────────────────
_NOVELTY_SIGNALS: list[str] = [
    r"\bcinematic\s+\w*\s*(?:productivity|tool|dashboard|editor)\b",  # cinematic + utility mash-up
    r"\bgame\s+\w*\s*(?:and|[+&])\s*\w*\s*tool\b",   # game + tool
    r"\bfilm\s+\w*\s*(?:and|[+&])\s*\w*\s*app\b",    # film + app
    r"\bnonstandard\b",
    r"\bunique\b",
    r"\bnovel\b",
    r"\bexperimental\b",
    r"\binnovative\b",
    r"\bvoice\s+(?:control|input|interface|ui|ux)\b",  # voice interaction
    r"\baugmented\s+reality\b|\bAR\s+\w",              # AR
    r"\bvirtual\s+reality\b|\bVR\s+\w",               # VR
    r"\bhaptic\b",                                      # haptic feedback
    r"\bmixed\s+reality\b|\bXR\b",                     # XR
]

# Unusual genre combination: two or more genre words from different categories
_GENRE_COMBOS: list[tuple[str, str]] = [
    ("cinematic", "productivity"),
    ("cinematic", "tool"),
    ("game", "tool"),
    ("film", "app"),
    ("film", "productivity"),
    ("game", "productivity"),
    ("art", "data"),
    ("creative", "enterprise"),
    ("narrative", "dashboard"),
]

# ── Constraint-load signals (+0.10 each, capped at 0.40) ─────────────────────
_CONSTRAINT_SIGNALS: list[str] = [
    r"\bauth(?:entication|orization|)?\b|\blogin\b|\busers\b|\baccounts\b",  # auth/users
    r"\bpersist(?:ence|ent|)?\b|\bdatabase\b|\bstorage\b|\bstore\b|\bsave\b",  # persistence
    r"\breal[\-\s]?time\b|\bwebsocket\b|\blive\s+update\b|\blive\s+feed\b",    # real-time
    r"\bintegration\b|\bapi\b|\bwebhook\b|\bthird[\-\s]party\b",              # integrations
    r"\bcompliance\b|\bGDPR\b|\bHIPAA\b|\baudit\b|\bregulat",                 # compliance
]


def _count_matches(patterns: list[str], text: str) -> int:
    """Count how many distinct patterns match in text (each pattern counts once)."""
    return sum(1 for p in patterns if re.search(p, text, re.IGNORECASE))


def _check_genre_combo(text: str) -> int:
    """Return 1 if any cross-genre combo is detected, else 0."""
    tl = text.lower()
    for a, b in _GENRE_COMBOS:
        if a in tl and b in tl:
            return 1
    return 0


def score_interpretation_risk(intent: str) -> float:
    """Score the interpretation risk of an intent string.

    Returns a float in [0.0, 1.0]. Higher scores indicate the intent is more
    likely to be misinterpreted by a less-capable model (Codex).

    Scoring is purely deterministic keyword/regex matching — no LLM, $0 cost.

    Categories:
      Ambiguity   (+0.15 per signal, cap 0.30): vague phrasing, missing audience,
                  missing success criteria
      Novelty     (+0.10 per signal, cap 0.30): genre mash-ups, novel/experimental
                  keywords, unusual interaction models
      Constraints (+0.10 per signal, cap 0.40): auth, persistence, real-time,
                  integrations, compliance

    Total is the SUM of capped category scores, capped to 1.0.
    """
    text = intent.strip()
    if not text:
        return 0.0

    # ── Ambiguity ─────────────────────────────────────────────────────────────
    ambiguity_hits = _count_matches(_AMBIGUITY_SIGNALS, text)
    # Missing success criteria adds one ambiguity hit
    has_criteria = _count_matches(_SUCCESS_CRITERIA_PATTERNS, text) > 0
    if not has_criteria:
        ambiguity_hits += 1
    ambiguity_score = min(ambiguity_hits * 0.15, 0.30)

    # ── Novelty ───────────────────────────────────────────────────────────────
    novelty_hits = _count_matches(_NOVELTY_SIGNALS, text)
    novelty_hits += _check_genre_combo(text)
    novelty_score = min(novelty_hits * 0.10, 0.30)

    # ── Constraint-load ───────────────────────────────────────────────────────
    constraint_hits = _count_matches(_CONSTRAINT_SIGNALS, text)
    constraint_score = min(constraint_hits * 0.10, 0.40)

    total = ambiguity_score + novelty_score + constraint_score
    return min(total, 1.0)
