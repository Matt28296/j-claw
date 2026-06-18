"""Read-back / aggregation tool for action-risk evidence (roadmap item #6).

``permissions.observe()`` durably appends ``risk_classified`` events to the per-run
append-only session transcripts under ``<repo>/sessions/<mission_id>.jsonl`` (see
session_log.py). Those logs persist across runs but nothing read them back — so the
evidence couldn't actually inform enforcement thresholds. This module closes that gap:
it scans every transcript, extracts the ``risk_classified`` events, and aggregates them
by ``kind`` and risk so the eventual enforcement layer (permission modes, roadmap #1)
can set thresholds from data instead of guesses.

Key design choice — **re-classify, don't trust the logged risk.** ``classify_action`` is
pure and deterministic but the taxonomy evolves (e.g. ``shell`` was reclassified low→high).
A record's logged ``risk`` reflects the classifier *at log time*; for tuning we want the
*current* assessment. So aggregation re-derives risk via ``classify_action(kind, detail)``
and separately reports **drift** (records whose logged risk no longer matches current) so
the operator can see how much historical evidence predates a taxonomy change.

Pure/stdlib + reuses permissions.classify_action; no network, no mutation. Run as a CLI:
    python risk_evidence.py [--sessions-dir DIR] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterator

from permissions import RISK_LEVELS, classify_action, risk_rank
from session_log import SESSIONS_DIR

_EVENT = "risk_classified"


def iter_risk_events(sessions_dir: Path | None = None) -> Iterator[dict]:
    """Yield every ``risk_classified`` record across all session transcripts.

    Tolerant by design: skips unreadable files, non-JSON lines, and a truncated final
    record (session_log appends without fsync, so a hard kill can leave a partial line).
    """
    base = Path(sessions_dir) if sessions_dir is not None else SESSIONS_DIR
    if not base.exists():
        return
    for path in sorted(base.glob("*.jsonl")):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # malformed (e.g. truncated last line) — skip, don't abort
                    if isinstance(rec, dict) and rec.get("event") == _EVENT:
                        yield rec
        except OSError:
            continue


def aggregate(events: Iterator[dict]) -> dict:
    """Aggregate risk_classified records by kind and *current* risk.

    Risk is re-derived from ``classify_action(kind, detail)`` (current taxonomy), not the
    logged value. ``drift`` counts records whose logged risk differs from current.
    """
    by_kind: dict[str, int] = defaultdict(int)
    by_risk: dict[str, int] = defaultdict(int)
    by_kind_risk: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_mission: dict[str, int] = defaultdict(int)
    total = 0
    drift = 0

    for rec in events:
        kind = str(rec.get("kind", "") or "unknown")
        detail = str(rec.get("detail", "") or "")
        current_risk, _ = classify_action(kind, detail)
        logged_risk = rec.get("risk")
        total += 1
        by_kind[kind] += 1
        by_risk[current_risk] += 1
        by_kind_risk[kind][current_risk] += 1
        by_mission[str(rec.get("mission_id", "") or "unknown")] += 1
        if logged_risk is not None and logged_risk != current_risk:
            drift += 1

    return {
        "total": total,
        "missions": len(by_mission),
        "by_kind": dict(by_kind),
        "by_risk": dict(by_risk),
        "by_kind_risk": {k: dict(v) for k, v in by_kind_risk.items()},
        "by_mission": dict(by_mission),
        "drift": drift,
    }


def _risk_sort_key(risk: str) -> tuple[int, str]:
    # Most-severe first; unknown levels rank past the known ones (risk_rank handles that).
    return (-risk_rank(risk), risk)


def format_report(agg: dict) -> str:
    """Render a human-readable summary, kinds ordered by current risk severity then count."""
    total = agg["total"]
    if total == 0:
        return ("No risk_classified evidence found yet.\n"
                "Run a real build so permissions.observe() logs events, then re-run this tool.")

    lines: list[str] = []
    lines.append(f"Action-risk evidence — {total} event(s) across {agg['missions']} mission(s)")
    if agg["drift"]:
        lines.append(f"  ⚠ {agg['drift']} event(s) logged under a risk that the current taxonomy "
                     "no longer assigns (taxonomy drift).")
    lines.append("")

    # By current risk level (severe → mild).
    lines.append("By current risk:")
    for risk in sorted(agg["by_risk"], key=_risk_sort_key):
        lines.append(f"  {risk:<9} {agg['by_risk'][risk]:>6}")
    lines.append("")

    # By kind, ordered by the kind's worst current risk then frequency.
    def _kind_key(kind: str) -> tuple:
        risks = agg["by_kind_risk"].get(kind, {})
        worst = min((_risk_sort_key(r)[0] for r in risks), default=0)
        return (worst, -agg["by_kind"][kind], kind)

    lines.append("By kind:")
    for kind in sorted(agg["by_kind"], key=_kind_key):
        risks = agg["by_kind_risk"].get(kind, {})
        risk_str = ", ".join(
            f"{r}={risks[r]}" for r in sorted(risks, key=_risk_sort_key)
        )
        lines.append(f"  {kind:<10} {agg['by_kind'][kind]:>6}   ({risk_str})")

    return "\n".join(lines)


def build_report(sessions_dir: Path | None = None) -> dict:
    """Convenience: read + aggregate in one call."""
    return aggregate(iter_risk_events(sessions_dir))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate action-risk evidence from session logs.")
    parser.add_argument("--sessions-dir", type=Path, default=None,
                        help=f"Directory of *.jsonl transcripts (default: {SESSIONS_DIR}).")
    parser.add_argument("--json", action="store_true", help="Emit the raw aggregate as JSON.")
    args = parser.parse_args(argv)

    agg = build_report(args.sessions_dir)
    if args.json:
        print(json.dumps(agg, indent=2, sort_keys=True))
    else:
        print(format_report(agg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
