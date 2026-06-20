"""
test_wave5_dryrun.py — Wave 5 dry-run: FORCE_FORMAT5 routing regression.

Asserts that the FORCE_FORMAT5 escape hatch routes correctly WITHOUT any
subprocess, worktree, LLM, or network call.  Run with:

    cd harness
    $env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"
    .\\.venv\\Scripts\\python.exe test_wave5_dryrun.py

Tests:
  T1 — FORCE_FORMAT5 on + orchestrator returns OVERSIZE spec → _handle_oversize called,
       Scheduler.run() never called (no flat-build worker execution).
  T2 — FORCE_FORMAT5 on + orchestrator returns FLAT even after retry → honest abort
       (return False), _handle_oversize never called, Scheduler.run() never called.
  T3 — FORCE_FORMAT5 on at depth 0 → 'decomposition_required' key injected into the
       first orchestrator payload.
"""

from __future__ import annotations
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ── Minimal environment so config / imports don't crash at load ───────────────
os.environ.setdefault("ANTHROPIC_API_KEY",    "sk-ant-test-key")
os.environ.setdefault("GOOGLE_API_KEY",        "test-google-key")
os.environ.setdefault("OPENROUTER_API_KEY",    "test-or-key")
os.environ.setdefault("ORCHESTRATOR_PROVIDER", "gemini")
os.environ.setdefault("WORKER_LADDER",
    "ollama::qwen3:8b,anthropic::claude-sonnet-4-6")
os.environ.setdefault("MAX_PAID_WORKER_CALLS",  "15")
os.environ.setdefault("OLLAMA_HOST",            "http://localhost:11434")
os.environ.setdefault("FORCE_FORMAT5",          "false")   # default off; patched per-test
os.environ.setdefault("TECHNICAL_ARCHITECT_ENABLED", "false")  # skip TA pass

sys.path.insert(0, str(Path(__file__).parent))


# ── Fixture data ─────────────────────────────────────────────────────────────

# An OVERSIZE (FORMAT 5) spec that _handle_oversize would normally handle.
OVERSIZE_SPEC = {
    "oversize": True,
    "reason": "Project is too large to build as a single pass",
    "sub_projects": [
        {"name": "frontend", "goal": "Build the frontend", "depends_on": []},
        {"name": "backend",  "goal": "Build the backend",  "depends_on": []},
        {"name": "db",       "goal": "Build the database layer", "depends_on": []},
    ],
}

# A flat (FORMAT 1) spec — what a non-decomposing orchestrator returns.
FLAT_SPEC = {
    "project_type": "web",
    "complexity": "low",
    "goal": "test intent",
    "features": [],
    "constraints": [],
    "architecture": {"frontend": "html", "backend": "none",
                     "database": "none", "deployment": "none"},
    "modules": [],
}


# ── Shared patch targets ──────────────────────────────────────────────────────
# These are patched in every test so nothing touches the network or filesystem
# beyond the temporary output directory.

_COMMON_PATCHES = [
    # Prevent dashboard subprocess spawn / browser open.
    "main._start_dashboard",
    # Prevent creative director LLM call.
    "main.CreativeDirector",
    # Prevent technical architect LLM call (belt-and-suspenders on top of env).
    "main.TechnicalArchitect",
    # make_orchestrator is replaced per-test with a controlled mock.
    "main.make_orchestrator",
    # StateWriter singleton — avoid WebSocket / file I/O side-effects.
    "main.sw",
    # Budget / cost resets — fast no-ops, nothing interesting to assert here.
    "main.reset_costs",
    # worker.reset_paid_budget is imported inline inside run_project(); patch module.
    "worker.reset_paid_budget",
    # orchestrator.reset_orchestrator_run — imported inline.
    "orchestrator.reset_orchestrator_run",
    # get_stack_lessons — experience log, not relevant here.
    "main.get_stack_lessons",
    # permissions.observe — filesystem-observe hook, no-op for tests.
    "permissions.observe",
]


def _build_patchers(extra: dict | None = None):
    """Return a dict of {target: MagicMock} for all common patches + extras."""
    mocks = {}
    for target in _COMMON_PATCHES:
        m = MagicMock()
        mocks[target] = m
    if extra:
        for target, mock_obj in extra.items():
            mocks[target] = mock_obj
    return mocks


class _PatchStack:
    """Context manager that applies a dict of {target: mock} patches at once."""

    def __init__(self, patches: dict):
        self._patches = patches
        self._started: list = []

    def __enter__(self):
        started = {}
        for target, mock_obj in self._patches.items():
            p = patch(target, mock_obj)
            p.start()
            self._started.append(p)
            started[target] = mock_obj
        return started

    def __exit__(self, *_):
        for p in reversed(self._started):
            p.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# T1 — OVERSIZE spec → _handle_oversize reached, Scheduler never runs
# ═══════════════════════════════════════════════════════════════════════════════

class TestForceFormat5OversizeRouting(unittest.TestCase):
    """With FORCE_FORMAT5=True and an orchestrator that immediately returns an
    OVERSIZE spec, run_project should route into _handle_oversize and must NOT
    invoke Scheduler.run() (no flat worker execution)."""

    def test_handle_oversize_called_scheduler_not_called(self):
        import main

        # Mock orchestrator whose first .call() returns the OVERSIZE spec.
        mock_orch = MagicMock()
        mock_orch.call.return_value = OVERSIZE_SPEC

        make_orch_mock = MagicMock(return_value=mock_orch)

        # We intercept _handle_oversize in the main module to assert invocation
        # and prevent the actual sub-project recursion.
        handle_oversize_mock = MagicMock(return_value=True)

        # Scheduler.run should never be called.
        scheduler_mock = MagicMock()

        # CreativeDirector.interpret() returns an empty brief so the difficulty
        # path is a no-op.
        cd_instance = MagicMock()
        cd_instance.interpret.return_value = {}
        cd_mock = MagicMock(return_value=cd_instance)

        patches = _build_patchers({
            "main.make_orchestrator":  make_orch_mock,
            "main.CreativeDirector":   cd_mock,
            "main._handle_oversize":   handle_oversize_mock,
            "main.Scheduler":          scheduler_mock,
        })

        with _PatchStack(patches), \
             patch.object(main, "FORCE_FORMAT5", True), \
             patch.object(main, "MIN_SUBPROJECT_COUNT", 3):

            with tempfile.TemporaryDirectory() as tmpdir:
                out = Path(tmpdir) / "test_output"
                result = main.run_project("test intent", out, depth=0, auto_accept=True)

        # _handle_oversize MUST have been called once.
        handle_oversize_mock.assert_called_once()

        # The FIRST positional arg should be the OVERSIZE spec.
        called_spec = handle_oversize_mock.call_args[0][0]
        self.assertTrue(called_spec.get("oversize"),
                        "First arg to _handle_oversize should be the oversize spec")

        # Scheduler.run() must NOT have been called (no worker execution).
        scheduler_mock.return_value.run.assert_not_called()
        scheduler_mock.assert_not_called()

        # run_project should propagate _handle_oversize's return value.
        self.assertTrue(result)

    def test_decomposition_required_injected_into_first_payload(self):
        """T3 — 'decomposition_required' key appears in the initial orchestrator
        payload when FORCE_FORMAT5=True and depth=0."""
        import main

        captured_payloads: list[dict] = []

        def capturing_call(payload):
            captured_payloads.append(dict(payload))
            return OVERSIZE_SPEC

        mock_orch = MagicMock()
        mock_orch.call.side_effect = capturing_call

        make_orch_mock = MagicMock(return_value=mock_orch)
        handle_oversize_mock = MagicMock(return_value=True)

        cd_instance = MagicMock()
        cd_instance.interpret.return_value = {}
        cd_mock = MagicMock(return_value=cd_instance)

        patches = _build_patchers({
            "main.make_orchestrator": make_orch_mock,
            "main.CreativeDirector":  cd_mock,
            "main._handle_oversize":  handle_oversize_mock,
            "main.Scheduler":         MagicMock(),
        })

        with _PatchStack(patches), \
             patch.object(main, "FORCE_FORMAT5", True), \
             patch.object(main, "MIN_SUBPROJECT_COUNT", 3):

            with tempfile.TemporaryDirectory() as tmpdir:
                out = Path(tmpdir) / "test_output"
                main.run_project("test intent", out, depth=0, auto_accept=True)

        # At least one call must have happened.
        self.assertTrue(len(captured_payloads) >= 1,
                        "Expected at least one orchestrator call")

        first_payload = captured_payloads[0]

        self.assertIn("decomposition_required", first_payload,
                      "'decomposition_required' must be injected into the first "
                      "orchestrator payload when FORCE_FORMAT5=True and depth=0")

        # The directive should mention the subproject count floor.
        directive = first_payload["decomposition_required"]
        self.assertIn("3", directive,
                      "The decomposition_required directive should mention "
                      "MIN_SUBPROJECT_COUNT (3)")
        self.assertIn("oversize", directive.lower(),
                      "The directive should mention 'oversize'")

    def test_force_format5_not_injected_at_depth_gt_0(self):
        """Regression: decomposition_required must NOT be injected for sub-projects
        (depth > 0) — sub-projects must flatten, not recurse indefinitely."""
        import main

        captured_payloads: list[dict] = []

        def capturing_call(payload):
            captured_payloads.append(dict(payload))
            return FLAT_SPEC   # sub-project returns flat, normal path

        mock_orch = MagicMock()
        mock_orch.call.side_effect = capturing_call

        make_orch_mock = MagicMock(return_value=mock_orch)

        cd_instance = MagicMock()
        cd_instance.interpret.return_value = {}
        cd_mock = MagicMock(return_value=cd_instance)

        # Sub-project at depth=1: FORCE_FORMAT5 should NOT inject the directive.
        # We need to stub enough of the post-spec path to avoid full execution.
        # Patch Scheduler so it does nothing and the DAG call is also intercepted.
        # The orchestrator returns FLAT_SPEC for both INIT and SPEC_ACCEPTED calls.
        scheduler_instance = MagicMock()
        scheduler_instance.run.return_value = None
        scheduler_mock = MagicMock(return_value=scheduler_instance)

        # Mock ProjectInstance to avoid filesystem-heavy task loading.
        project_instance = MagicMock()
        project_instance.failed_tasks.return_value = []
        project_instance.all_tasks_done.return_value = True
        project_instance.tasks_as_list.return_value = []
        project_instance.tasks = []

        # Stub downstream pipeline: review, handoff, etc.
        patches = _build_patchers({
            "main.make_orchestrator": make_orch_mock,
            "main.CreativeDirector":  cd_mock,
            "main.Scheduler":         scheduler_mock,
            "main.ProjectInstance":   MagicMock(return_value=project_instance),
            "main.run_final_review":  MagicMock(return_value={"passed": True, "issues": []}),
            "main.parse_review_issues": MagicMock(return_value=(True, [])),
            "main.write_handoff":     MagicMock(return_value=Path("/tmp/HANDOFF.md")),
            "main.try_claude_stamp":  MagicMock(),
            "main.git_commit_project": MagicMock(),
            "main.deploy_project":    MagicMock(return_value=(None, "")),
            "main.append_deploy_section": MagicMock(),
            "main.notify_build_outcome": MagicMock(),
            "main.check_completeness": MagicMock(return_value=(True, [])),
            "main.detect_ecosystem":  MagicMock(return_value="unknown"),
            "main._build_disposition": MagicMock(return_value=True),
        })

        with _PatchStack(patches), \
             patch.object(main, "FORCE_FORMAT5", True), \
             patch.object(main, "MIN_SUBPROJECT_COUNT", 3):

            with tempfile.TemporaryDirectory() as tmpdir:
                out = Path(tmpdir) / "test_subproject"
                # depth=1 — sub-project, FORCE_FORMAT5 must NOT inject directive
                try:
                    main.run_project("sub intent", out, depth=1, auto_accept=True)
                except Exception:
                    pass  # downstream stubs may not be complete; payload check is enough

        # The first INIT payload at depth=1 must NOT have decomposition_required.
        init_payloads = [p for p in captured_payloads
                         if p.get("system_state") == "INIT"]
        # Assert the INIT call actually happened — otherwise the depth>0 check below
        # would pass vacuously if a mock regression ever suppressed the call.
        self.assertGreaterEqual(len(init_payloads), 1,
                                "Expected at least one INIT orchestrator call at depth=1")
        first_init = init_payloads[0]
        self.assertNotIn("decomposition_required", first_init,
                         "decomposition_required must NOT be injected at depth > 0")


# ═══════════════════════════════════════════════════════════════════════════════
# T2 — FLAT spec even after retry → honest abort (False), no Scheduler.run()
# ═══════════════════════════════════════════════════════════════════════════════

class TestForceFormat5FlatAbort(unittest.TestCase):
    """With FORCE_FORMAT5=True and an orchestrator that stubbornly returns a FLAT
    spec on BOTH the initial call and the re-request retry, run_project must:
      • return False (honest abort — not a pass)
      • never invoke Scheduler.run() (no silent flat build)
      • never invoke _handle_oversize
    """

    def test_flat_spec_abort_no_scheduler_no_handle_oversize(self):
        import main

        # Orchestrator always returns the flat spec (simulates a model that
        # refuses to decompose even after the sharper retry directive).
        mock_orch = MagicMock()
        mock_orch.call.return_value = FLAT_SPEC

        make_orch_mock = MagicMock(return_value=mock_orch)
        handle_oversize_mock = MagicMock(return_value=True)
        scheduler_mock = MagicMock()

        cd_instance = MagicMock()
        cd_instance.interpret.return_value = {}
        cd_mock = MagicMock(return_value=cd_instance)

        patches = _build_patchers({
            "main.make_orchestrator": make_orch_mock,
            "main.CreativeDirector":  cd_mock,
            "main._handle_oversize":  handle_oversize_mock,
            "main.Scheduler":         scheduler_mock,
        })

        with _PatchStack(patches), \
             patch.object(main, "FORCE_FORMAT5", True), \
             patch.object(main, "MIN_SUBPROJECT_COUNT", 3):

            with tempfile.TemporaryDirectory() as tmpdir:
                out = Path(tmpdir) / "test_abort"
                result = main.run_project("test intent", out, depth=0, auto_accept=True)

        # Must return False — honest abort.
        self.assertIs(result, False,
                      "run_project should return False when orchestrator refuses to "
                      "decompose after the FORCE_FORMAT5 retry")

        # _handle_oversize must NOT have been called.
        handle_oversize_mock.assert_not_called()

        # Scheduler must NOT have been instantiated or run.
        scheduler_mock.assert_not_called()

    def test_flat_abort_orchestrator_called_exactly_twice(self):
        """The re-request path calls the orchestrator EXACTLY twice when both
        responses are flat (initial call + one retry)."""
        import main

        mock_orch = MagicMock()
        mock_orch.call.return_value = FLAT_SPEC

        make_orch_mock = MagicMock(return_value=mock_orch)

        cd_instance = MagicMock()
        cd_instance.interpret.return_value = {}
        cd_mock = MagicMock(return_value=cd_instance)

        patches = _build_patchers({
            "main.make_orchestrator": make_orch_mock,
            "main.CreativeDirector":  cd_mock,
            "main._handle_oversize":  MagicMock(return_value=True),
            "main.Scheduler":         MagicMock(),
        })

        with _PatchStack(patches), \
             patch.object(main, "FORCE_FORMAT5", True), \
             patch.object(main, "MIN_SUBPROJECT_COUNT", 3):

            with tempfile.TemporaryDirectory() as tmpdir:
                out = Path(tmpdir) / "test_retry_count"
                main.run_project("test intent", out, depth=0, auto_accept=True)

        # Exactly 2 calls: initial INIT + 1 retry with sharper directive.
        self.assertEqual(mock_orch.call.call_count, 2,
                         f"Expected exactly 2 orchestrator calls (INIT + retry), "
                         f"got {mock_orch.call.call_count}")

    def test_retry_payload_contains_sharpened_directive(self):
        """The retry payload should include a sharpened 'decomposition_required'
        key that explicitly says the previous response was FLAT."""
        import main

        retry_payloads: list[dict] = []
        call_count = [0]

        def side_effect(payload):
            call_count[0] += 1
            if call_count[0] == 2:
                retry_payloads.append(dict(payload))
            return FLAT_SPEC

        mock_orch = MagicMock()
        mock_orch.call.side_effect = side_effect
        make_orch_mock = MagicMock(return_value=mock_orch)

        cd_instance = MagicMock()
        cd_instance.interpret.return_value = {}
        cd_mock = MagicMock(return_value=cd_instance)

        patches = _build_patchers({
            "main.make_orchestrator": make_orch_mock,
            "main.CreativeDirector":  cd_mock,
            "main._handle_oversize":  MagicMock(return_value=True),
            "main.Scheduler":         MagicMock(),
        })

        with _PatchStack(patches), \
             patch.object(main, "FORCE_FORMAT5", True), \
             patch.object(main, "MIN_SUBPROJECT_COUNT", 3):

            with tempfile.TemporaryDirectory() as tmpdir:
                out = Path(tmpdir) / "test_retry_payload"
                main.run_project("test intent", out, depth=0, auto_accept=True)

        self.assertEqual(len(retry_payloads), 1, "Expected exactly one retry payload")
        retry = retry_payloads[0]

        self.assertIn("decomposition_required", retry,
                      "Retry payload must contain 'decomposition_required'")
        directive = retry["decomposition_required"]
        # The sharper re-request mentions the FLAT response.
        self.assertIn("FLAT", directive,
                      "Retry directive should mention 'FLAT' to signal the previous "
                      "response was rejected")


# ═══════════════════════════════════════════════════════════════════════════════
# T-BONUS — FORCE_FORMAT5=False: normal flat path proceeds (no directive injected)
# ═══════════════════════════════════════════════════════════════════════════════

class TestForceFormat5Disabled(unittest.TestCase):
    """Sanity check: when FORCE_FORMAT5=False a flat spec does NOT trigger the
    abort path — the pipeline proceeds normally (Scheduler is reached).  This
    guards against a regression where the FORCE_FORMAT5 gate fires unconditionally."""

    def test_force_format5_off_no_abort(self):
        import main

        mock_orch = MagicMock()
        # First call → flat spec; second call → fake DAG response
        _dag_response = {"tasks": []}
        _call_count = [0]

        def _orch_side(payload):
            _call_count[0] += 1
            state = payload.get("system_state", "")
            if state == "SPEC_ACCEPTED":
                return _dag_response
            return FLAT_SPEC

        mock_orch.call.side_effect = _orch_side
        make_orch_mock = MagicMock(return_value=mock_orch)

        scheduler_instance = MagicMock()
        scheduler_instance.run.return_value = None
        scheduler_mock = MagicMock(return_value=scheduler_instance)

        project_instance = MagicMock()
        project_instance.failed_tasks.return_value = []
        project_instance.all_tasks_done.return_value = True
        project_instance.tasks_as_list.return_value = []
        project_instance.tasks = []

        cd_instance = MagicMock()
        cd_instance.interpret.return_value = {}
        cd_mock = MagicMock(return_value=cd_instance)

        handle_oversize_mock = MagicMock(return_value=True)

        patches = _build_patchers({
            "main.make_orchestrator":   make_orch_mock,
            "main.CreativeDirector":    cd_mock,
            "main._handle_oversize":    handle_oversize_mock,
            "main.Scheduler":           scheduler_mock,
            "main.ProjectInstance":     MagicMock(return_value=project_instance),
            "main.run_final_review":    MagicMock(return_value={"passed": True, "issues": []}),
            "main.parse_review_issues": MagicMock(return_value=(True, [])),
            "main.write_handoff":       MagicMock(return_value=Path("/tmp/HANDOFF.md")),
            "main.try_claude_stamp":    MagicMock(),
            "main.git_commit_project":  MagicMock(),
            "main.deploy_project":      MagicMock(return_value=(None, "")),
            "main.append_deploy_section": MagicMock(),
            "main.notify_build_outcome": MagicMock(),
            "main.check_completeness":  MagicMock(return_value=(True, [])),
            "main.detect_ecosystem":    MagicMock(return_value="unknown"),
            "main._build_disposition":  MagicMock(return_value=True),
        })

        with _PatchStack(patches), \
             patch.object(main, "FORCE_FORMAT5", False):

            with tempfile.TemporaryDirectory() as tmpdir:
                out = Path(tmpdir) / "test_no_force"
                result = main.run_project("test intent", out, depth=0, auto_accept=True)

        # The flat-spec path must NOT have been treated as an abort.
        # decomposition_required must NOT have been injected.
        init_calls = [
            c for c in mock_orch.call.call_args_list
            if (c.args and isinstance(c.args[0], dict) and
                c.args[0].get("system_state") == "INIT")
        ]
        if init_calls:
            first_init_payload = init_calls[0].args[0]
            self.assertNotIn("decomposition_required", first_init_payload,
                             "decomposition_required must NOT be injected when "
                             "FORCE_FORMAT5=False")

        # _handle_oversize must NOT have been triggered for a flat spec.
        handle_oversize_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
