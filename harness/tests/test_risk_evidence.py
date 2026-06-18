"""Tests for the action-risk evidence aggregator (roadmap item #6, read-back tool).

Verifies: only risk_classified events are counted (other lifecycle events ignored), a
truncated/malformed final line is tolerated, risk is re-derived from the CURRENT taxonomy
(not the logged value), and logged-vs-current drift is reported. All I/O is against a temp
sessions dir — the real <repo>/sessions/ is untouched.
"""
from __future__ import annotations
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_HARNESS = os.path.join(os.path.dirname(__file__), "..")
if _HARNESS not in sys.path:
    sys.path.insert(0, _HARNESS)

import risk_evidence


def _write_jsonl(path: Path, records: list, *, trailing_garbage: bool = False) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
        if trailing_garbage:
            fh.write('{"event": "risk_classified", "kind": "install"')  # truncated last line


def _rc(kind, risk, mission, detail=""):
    return {"event": "risk_classified", "kind": kind, "risk": risk,
            "mission_id": mission, "detail": detail, "reason": ""}


class TestRiskEvidence(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="risk_ev_"))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_missing_dir_yields_nothing(self):
        agg = risk_evidence.build_report(self._tmp / "does_not_exist")
        self.assertEqual(agg["total"], 0)
        self.assertIn("No risk_classified evidence", risk_evidence.format_report(agg))

    def test_aggregation_counts_filters_and_drift(self):
        # mission A: shell logged as "low" (stale taxonomy), install high, llm_cli medium,
        #            plus non-risk events that must be ignored. Truncated last line tolerated.
        _write_jsonl(self._tmp / "A.jsonl", [
            {"event": "mission_started", "mission_id": "A", "intent": "x"},
            _rc("shell", "low", "A", "bash render.sh"),      # current taxonomy → high ⇒ drift
            _rc("install", "high", "A", "npm install"),
            _rc("llm_cli", "medium", "A", "codex exec"),
            {"event": "task_done", "mission_id": "A", "task_id": "t1"},
        ], trailing_garbage=True)
        # mission B: a render (low) and another shell (already logged high → no drift).
        _write_jsonl(self._tmp / "B.jsonl", [
            _rc("render", "low", "B", "ffmpeg ..."),
            _rc("shell", "high", "B", "python render_scene.py"),
        ])

        agg = risk_evidence.build_report(self._tmp)

        self.assertEqual(agg["total"], 5)              # 5 risk_classified; lifecycle + garbage excluded
        self.assertEqual(agg["missions"], 2)
        self.assertEqual(agg["by_kind"], {"shell": 2, "install": 1, "llm_cli": 1, "render": 1})
        # Risk is the CURRENT classification: both shells → high (not the logged "low").
        self.assertEqual(agg["by_risk"]["high"], 3)    # 2 shell + 1 install
        self.assertEqual(agg["by_risk"]["medium"], 1)  # llm_cli
        self.assertEqual(agg["by_risk"]["low"], 1)     # render
        self.assertEqual(agg["by_kind_risk"]["shell"], {"high": 2})
        # Only the stale shell-logged-low record drifts.
        self.assertEqual(agg["drift"], 1)
        self.assertEqual(agg["by_mission"], {"A": 3, "B": 2})

    def test_format_report_orders_by_severity(self):
        _write_jsonl(self._tmp / "A.jsonl", [
            _rc("render", "low", "A"),
            _rc("shell", "high", "A", "bash x.sh"),
        ])
        report = risk_evidence.format_report(risk_evidence.build_report(self._tmp))
        self.assertIn("2 event(s)", report)
        # 'shell' (high) must be listed before 'render' (low) in the by-kind section.
        self.assertLess(report.index("shell"), report.index("render"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
