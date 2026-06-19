"""
test_wave4_verdict.py — Wave 4 regression tests for the A1 (verdict & cost-scope) specialist.

Closes the gaps the Wave 3 adversarial review found in the verdict/cost path — every one
of these fails specifically on the FORMAT-5 *decomposing* build path the MOBA stress test takes,
and the existing suite only locks the happy path.

Covered:
  C1 — the cost ceiling is build-GLOBAL, not per-sub-project:
        * reset_costs() runs ONLY for the top-level run (depth 0), never per sub-project;
        * cumulative spend across 2 sub-projects trips the ceiling and HALTS the whole build
          (BuildCostCeilingExceeded propagates out of _handle_oversize, not swallowed).
  H1 — manual mode reports an HONEST verdict (a failed-and-exhausted task => FAIL, not True).
  H2 — a deadlocked/pending instance (tasks never ran) is NOT reported as PASS.
  H4 — a missing / ambiguous OpenClaw stamp is NOT rendered green (default not-clean).

Zero API spend. Run:
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python harness/test_wave4_verdict.py
"""
from __future__ import annotations
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Env shims so importing the harness modules never reaches for real keys.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("GOOGLE_API_KEY", "test-google-key")
os.environ.setdefault("OPENROUTER_API_KEY", "test-or-key")
os.environ.setdefault("ORCHESTRATOR_PROVIDER", "gemini")

sys.path.insert(0, str(Path(__file__).parent))

import main
import cost
from project import ProjectInstance, Task


def _make_task(tid: str, status: str = "pending", error: str = "") -> Task:
    t = Task(
        id=tid, type="backend", objective="o", files=[], dependencies=[],
        priority="normal", acceptance_criteria=[], verification="none",
    )
    t.status = status
    t.error_log = error
    return t


class _Usage:
    """Minimal stand-in for an Anthropic response.usage (priced by cost.call_cost)."""
    def __init__(self, i=0, o=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


# ── C1: build-global cost ceiling ───────────────────────────────────────────────

class TestC1CostCeilingIsBuildGlobal(unittest.TestCase):
    """C1 (BLOCKER): the cost ceiling must be per-BUILD, not per-sub-project. A
    FORMAT-5 decomposition must not get a fresh, re-armed budget per scene, and a
    tripped ceiling in any scene must halt the entire build."""

    def setUp(self):
        cost.reset_costs()
        self.addCleanup(cost.reset_costs)

    def test_reset_costs_runs_only_at_top_level(self):
        # run_project resets the accumulator only when depth == 0. A sub-project call
        # (depth > 0) must NOT zero the running total — otherwise each scene re-arms a
        # fresh ceiling and a 10-scene build can legally spend 10x the cap unattended.
        reset_calls = {"n": 0}

        def _spy_reset():
            reset_calls["n"] += 1

        # Stop after the reset decision so we don't run the whole pipeline.
        with patch.object(main, "reset_costs", side_effect=_spy_reset), \
             patch.object(main, "_run_project_inner", return_value=True), \
             patch("worker.reset_paid_budget"), \
             patch("orchestrator.reset_orchestrator_run"), \
             patch.object(main, "_start_dashboard"):
            out = Path(os.environ.get("TEMP", ".")) / "_w4_c1_top"
            main.run_project("intent", out, depth=0, auto_accept=True)
            self.assertEqual(reset_calls["n"], 1, "top-level run must reset the cost accumulator once")

            reset_calls["n"] = 0
            main.run_project("intent", out, depth=1, auto_accept=True)
            self.assertEqual(reset_calls["n"], 0, "a sub-project (depth>0) must NOT reset the budget")

    def test_cumulative_spend_across_two_subprojects_halts_the_build(self):
        # Two sub-projects each spend just under the ceiling; their CUMULATIVE spend
        # crosses it. Because the accumulator is build-global (not reset per scene),
        # the second sub-project's check_cost_ceiling() trips and the resulting
        # BuildCostCeilingExceeded must PROPAGATE out of _handle_oversize (halting the
        # whole build), not be swallowed by the broad `except Exception` that lets the
        # loop continue with a re-armed budget.
        ceiling = 10.0  # USD
        ran = []

        def _fake_run_project(goal, sp_dir, depth, manual=False, auto_accept=False, wiring=None):
            ran.append(goal)
            # Each scene spends $6 of real (sonnet) metered budget — NO reset between scenes.
            # 1M sonnet input = $3, 1M output = $15; use input-only to land at ~$6/scene.
            cost.record_usage(_Usage(i=2_000_000), "claude-sonnet-4-6", "worker")
            # Then the scene checks the ceiling at its batch boundary, as the scheduler does.
            cost.check_cost_ceiling()
            return True

        response = {
            "reason": "oversize",
            "sub_projects": [
                {"name": "scene_a", "goal": "build scene a", "depends_on": []},
                {"name": "scene_b", "goal": "build scene b", "depends_on": ["scene_a"]},
            ],
        }

        with patch.object(main, "run_project", side_effect=_fake_run_project), \
             patch("config.MAX_BUILD_COST_USD", ceiling), \
             patch("config.MAX_BUILD_TOKENS", 0), \
             patch("config.BUILD_COST_WARN_FRAC", 0.0), \
             patch.object(main, "write_parent_handoff", return_value=MagicMock()):
            base = Path(os.environ.get("TEMP", ".")) / "_w4_c1_build"
            with self.assertRaises(cost.BuildCostCeilingExceeded):
                main._handle_oversize(response, base, depth=0, auto_accept=True, intent="x")

        # The SECOND scene must have started (cumulative trip), and the build halted
        # there — a per-sub-project ceiling would have let both finish + a third begin.
        self.assertEqual(ran, ["build scene a", "build scene b"],
                         "ceiling must trip on cumulative spend at the 2nd scene, halting the build")

    def test_aggregate_cost_not_double_counted_across_subprojects(self):
        # Regression for the build-global double-count: _handle_oversize used to SUM
        # cost_summary() per sub-project, but the accumulator is build-global (never reset
        # between scenes), so each read already includes prior scenes' spend → a triangular
        # double-count in the reported aggregate (operator notification + dashboard).
        # SP1 spends $3 and SP2 spends $3 → TRUE cumulative $6; the old per-iteration sum
        # reported $9 (= $3 + $6). Also asserts decomposing-path output isolation: each
        # sub-project's declared file lands in its OWN sp_dir.
        import shutil
        import tempfile

        def _fake_run_project(goal, sp_dir, depth, manual=False, auto_accept=False, wiring=None):
            # Mimic the proven Scheduler husk behavior (file lands in this scene's output_dir)
            # and $3 of sonnet metered spend (1M input * $3/M). NO reset between scenes.
            (Path(sp_dir) / "out.txt").write_text(goal, encoding="utf-8")
            cost.record_usage(_Usage(i=1_000_000), "claude-sonnet-4-6", "worker")
            return True

        response = {
            "reason": "oversize",
            "sub_projects": [
                {"name": "scene_a", "goal": "build scene a", "depends_on": []},
                {"name": "scene_b", "goal": "build scene b", "depends_on": ["scene_a"]},
            ],
        }
        base = Path(tempfile.mkdtemp(prefix="_w4_c1_agg_"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        sw_mock = MagicMock()
        with patch.object(main, "run_project", side_effect=_fake_run_project), \
             patch("config.MAX_BUILD_COST_USD", 100.0), \
             patch.object(main, "sw", sw_mock), \
             patch.object(main, "notify_build_outcome"), \
             patch.object(main, "write_parent_handoff", return_value=base / "HANDOFF.md"):
            ok = main._handle_oversize(response, base, depth=0, auto_accept=True, intent="x")

        self.assertTrue(ok, "both sub-projects passed → aggregate should pass")
        # Output isolation on the decomposing path: each scene's file landed in its OWN dir.
        self.assertEqual((base / "scene_a" / "out.txt").read_text(encoding="utf-8"), "build scene a")
        self.assertEqual((base / "scene_b" / "out.txt").read_text(encoding="utf-8"), "build scene b")
        # The reported aggregate must equal the TRUE cumulative spend ($6), not the
        # triangular double-count ($9) the old per-iteration sum produced.
        on_cost_arg = sw_mock.on_cost.call_args[0][0]
        self.assertAlmostEqual(on_cost_arg["total_usd"], 6.0, places=4,
                               msg="aggregate cost must not double-count build-global spend")
        self.assertEqual(on_cost_arg["paid_calls"], 2,
                         "paid_calls must be the build-global count, not a per-scene sum")


# ── H1: honest manual verdict ───────────────────────────────────────────────────

class TestH1ManualVerdictHonest(unittest.TestCase):
    """H1 (BLOCKER): manual mode used to hard-return True. A --manual run with a
    failed-and-exhausted task must report FAIL, computed from the same inputs the
    automated branch feeds _build_disposition()."""

    def test_disposition_fails_on_failed_task(self):
        # The exact predicate the manual branch now returns.
        self.assertFalse(
            main._build_disposition(review_passed=True, dynamic_passed=True,
                                    failed_tasks=[_make_task("task-3", "failed")]),
            "a failed-and-exhausted task must fail the verdict",
        )
        self.assertTrue(
            main._build_disposition(review_passed=True, dynamic_passed=True, failed_tasks=[]),
            "clean inputs must pass",
        )

    def test_manual_branch_returns_fail_with_failed_task(self):
        # Drive the real manual branch of _run_project_inner with collaborators mocked.
        instance = ProjectInstance(Path("."))
        instance.spec = {"goal": "g"}
        instance.tasks = {"task-1": _make_task("task-1", "done"),
                          "task-2": _make_task("task-2", "failed", "boom")}
        # The branch calls instance.load_tasks(dag["tasks"]); we pre-seeded tasks above,
        # so neutralize the loader to keep our failed-task fixture intact.
        instance.load_tasks = lambda *a, **k: None

        with patch.object(main, "_start_dashboard"), \
             patch.object(main, "CreativeDirector") as _cd, \
             patch.object(main, "make_orchestrator") as _mo, \
             patch.object(main, "ProjectInstance", return_value=instance), \
             patch.object(main, "Scheduler") as _sched, \
             patch.object(main, "detect_ecosystem", return_value="unknown"), \
             patch.object(main, "run_final_review", return_value=True), \
             patch.object(main, "run_e2e_tests", return_value=(True, "")), \
             patch.object(main, "run_playwright_project_check", return_value=(True, "")), \
             patch.object(main, "check_completeness", return_value=(True, [])):
            _cd.return_value.interpret.return_value = {}
            _full_task = {
                "id": "task-1", "type": "backend", "objective": "o", "files": [],
                "dependencies": [], "priority": "normal", "acceptance_criteria": [],
                "verification": "none",
            }
            _mo.return_value.call.return_value = {"tasks": [_full_task]}
            _sched.return_value.run.return_value = None
            out = Path(os.environ.get("TEMP", ".")) / "_w4_h1_manual"
            out.mkdir(parents=True, exist_ok=True)
            passed = main._run_project_inner(
                "intent", out, depth=0, manual=True, auto_accept=True,
                wiring=None, phase={"current": "x"},
            )
        self.assertFalse(passed, "manual build with a failed+exhausted task must report FAIL")


# ── H2: deadlocked / pending instance ───────────────────────────────────────────

class TestH2PendingTasksFailVerdict(unittest.TestCase):
    """H2: a scheduler deadlock leaves tasks `pending` (never `failed`), so
    failed_tasks() is empty and the stalled build used to PASS. all_tasks_done()
    must feed the disposition so a not-done build is NOT a PASS."""

    def test_disposition_fails_when_not_all_done(self):
        self.assertFalse(
            main._build_disposition(review_passed=True, dynamic_passed=True,
                                    failed_tasks=[], all_done=False),
            "a deadlocked/pending build (not all tasks done) must not PASS",
        )
        self.assertTrue(
            main._build_disposition(review_passed=True, dynamic_passed=True,
                                    failed_tasks=[], all_done=True),
            "all-done clean build passes",
        )

    def test_pending_instance_reports_not_done(self):
        # A real instance with a stuck pending task: all_tasks_done() is False, which is
        # exactly the signal main now folds into the verdict.
        instance = ProjectInstance(Path("."))
        instance.tasks = {"task-1": _make_task("task-1", "done"),
                          "task-2": _make_task("task-2", "pending")}  # never ran (deadlock)
        self.assertFalse(instance.all_tasks_done())
        self.assertEqual(instance.failed_tasks(), [],
                         "deadlocked tasks are pending, not failed — failed_tasks() can't see them")
        # Therefore the verdict must rely on all_done, not failed_tasks, to fail this build.
        self.assertFalse(
            main._build_disposition(True, True, instance.failed_tasks(),
                                    instance.all_tasks_done()),
        )


# ── H4: missing / ambiguous stamp is not green ──────────────────────────────────

class TestH4StampDefaultNotGreen(unittest.TestCase):
    """H4: stamp-issue detection was exact-string and defaulted GREEN on any
    missing/timed-out/paraphrased stamp. Invert: require an explicit APPROVED marker
    to render green; treat missing/ambiguous as not-clean (has issues)."""

    def _write(self, text: str) -> Path:
        p = Path(os.environ.get("TEMP", ".")) / "_w4_h4_handoff.md"
        p.write_text(text, encoding="utf-8")
        return p

    def test_missing_stamp_is_not_clean(self):
        # Stamp timed out / CLI absent → nothing appended. Old code → green; now → issues.
        p = self._write("# HANDOFF\n\nBuild done. No verdict section was ever written.\n")
        self.assertTrue(main._handoff_has_stamp_issues(p),
                        "a missing stamp must NOT render a clean green check")

    def test_paraphrased_stamp_without_marker_is_not_clean(self):
        p = self._write("# HANDOFF\n\n## Claude Code Verdict\n\nLooks good to me overall.\n")
        self.assertTrue(main._handoff_has_stamp_issues(p),
                        "a paraphrased verdict lacking the APPROVED marker is ambiguous → not green")

    def test_explicit_issues_found_is_not_clean(self):
        p = self._write("# HANDOFF\n\n## Claude Code Verdict\n\nGaps remain.\nOPENCLAW: ISSUES FOUND\n")
        self.assertTrue(main._handoff_has_stamp_issues(p))

    def test_explicit_approved_is_clean(self):
        p = self._write("# HANDOFF\n\n## Claude Code Verdict\n\nMeets the goal.\nOPENCLAW: APPROVED\n")
        self.assertFalse(main._handoff_has_stamp_issues(p),
                         "only an explicit APPROVED marker renders green")

    def test_unreadable_handoff_is_not_clean(self):
        missing = Path(os.environ.get("TEMP", ".")) / "_w4_h4_does_not_exist.md"
        if missing.exists():
            missing.unlink()
        self.assertTrue(main._handoff_has_stamp_issues(missing),
                        "an unreadable/absent handoff cannot confirm a clean stamp → not green")


if __name__ == "__main__":
    unittest.main(verbosity=2)
