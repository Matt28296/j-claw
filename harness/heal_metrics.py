#!/usr/bin/env python3
"""Lightweight convergence/oscillation metrics for the self-healing loop.

Pure functions (no I/O, no harness imports) so they are trivially testable.
Used by main.py to decide whether consecutive heal cycles are actually
converging or merely churning / regressing on the same set of issues.
"""
from __future__ import annotations
import re

_WORD = re.compile(r"[a-z0-9]+")


def _normalize(issue: str) -> frozenset[str]:
    """Reduce an issue string to a bag of lowercase alphanumeric tokens.

    Drops markup, punctuation, and ordering so that cosmetic rewordings of
    the *same* underlying problem still compare as highly similar.
    """
    return frozenset(_WORD.findall(issue.lower()))


def issue_set_similarity(prev: list[str], curr: list[str]) -> float:
    """Jaccard similarity (0.0–1.0) between two issue lists.

    Each issue is reduced to a token set; we union the tokens per cycle and
    compute |A ∩ B| / |A ∪ B|. Two empty lists are treated as identical (1.0)
    and one empty / one non-empty as fully disjoint (0.0).
    """
    a: set[str] = set()
    for issue in prev:
        a |= _normalize(issue)
    b: set[str] = set()
    for issue in curr:
        b |= _normalize(issue)

    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 1.0


def classify_trend(
    prev: list[str],
    curr: list[str],
    overlap_threshold: float = 0.6,
) -> str:
    """Classify the cycle-over-cycle trend.

    Returns one of:
      "regressing"  — issue count went UP (fixes made things worse)
      "stalled"     — count did not decrease, or the same issues recur
                       (similarity >= overlap_threshold)
      "converging"  — fewer issues and meaningfully different set
    """
    if len(curr) > len(prev):
        return "regressing"
    sim = issue_set_similarity(prev, curr)
    if len(curr) >= len(prev) or sim >= overlap_threshold:
        return "stalled"
    return "converging"


if __name__ == "__main__":
    # Inline self-test. Run with: PYTHONUTF8=1 python harness/heal_metrics.py
    # identical sets -> high similarity, stalled trend
    a = ["Player class name mismatch in GameScene", "Missing collision handler"]
    b = ["player class-name mismatch in game scene", "missing collision handler!"]
    sim_ab = issue_set_similarity(a, b)
    assert sim_ab >= 0.6, sim_ab
    assert classify_trend(a, b) == "stalled", classify_trend(a, b)

    # regression: more issues than before
    c = a + ["Reintroduced disallowed framework jQuery", "New import error"]
    assert classify_trend(a, c) == "regressing", classify_trend(a, c)

    # genuine progress: fewer, different issues
    d = ["Typo in README heading"]
    assert classify_trend(a, d) == "converging", classify_trend(a, d)

    # empty-set edge cases
    assert issue_set_similarity([], []) == 1.0
    assert issue_set_similarity([], a) == 0.0

    print("heal_metrics self-test passed ✓")
