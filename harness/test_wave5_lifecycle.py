"""
test_wave5_lifecycle.py — Wave 5 regression coverage for:

  1. KILL-SWITCH / CLEAN-ABORT: worktrees are cleaned up on exception, crash, and
     KeyboardInterrupt exit paths (Scheduler context-manager guarantee).
  2. MEMORY_LINT AUTO-WIRING: lint_project_memory() is invoked on the --continue
     and FORMAT-5 sub-project paths; a failure inside it does NOT propagate.

Zero API spend; every external boundary is mocked.  Run with:
    cd harness
    $env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"
    .venv\\Scripts\\python.exe test_wave5_lifecycle.py
"""

from __future__ import annotations
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ── env setup (mirrors other wave test files) ────────────────────────────────
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("GOOGLE_API_KEY", "test-google-key")
os.environ.setdefault("OPENROUTER_API_KEY", "test-or-key")
os.environ.setdefault("ORCHESTRATOR_PROVIDER", "gemini")
os.environ.setdefault("OLLAMA_HOST", "http://localhost:11434")

sys.path.insert(0, str(Path(__file__).parent))


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_minimal_instance(tmp_path: Path | None = None):
    """Return a minimal ProjectInstance-like object with one pending task."""
    from project import ProjectInstance, Task
    import tempfile
    d = tmp_path or Path(tempfile.mkdtemp())
    d.mkdir(parents=True, exist_ok=True)
    inst = ProjectInstance(d)
    inst.spec = {"goal": "test goal", "stack": "vanilla"}
    return inst


# ═════════════════════════════════════════════════════════════════════════════
#  Suite 1 — Scheduler context-manager / worktree cleanup
# ═════════════════════════════════════════════════════════════════════════════

class TestSchedulerContextManager(unittest.TestCase):
    """Scheduler.__enter__/__exit__ are present and delegate to WorktreeManager."""

    def setUp(self):
        from project import ProjectInstance
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.inst = _make_minimal_instance(self.tmp)
        self.orch = MagicMock()

    def _make_scheduler(self):
        """Build a Scheduler with a mocked WorktreeManager."""
        from scheduler import Scheduler
        s = Scheduler.__new__(Scheduler)
        s.instance = self.inst
        s.orch = self.orch
        s._wt_manager = MagicMock()
        s._wt_manager.__enter__ = MagicMock(return_value=s._wt_manager)
        s._wt_manager.__exit__ = MagicMock(return_value=None)
        return s

    def test_scheduler_is_context_manager(self):
        """Scheduler must expose __enter__ and __exit__."""
        from scheduler import Scheduler
        self.assertTrue(hasattr(Scheduler, "__enter__"),
                        "Scheduler must have __enter__")
        self.assertTrue(hasattr(Scheduler, "__exit__"),
                        "Scheduler must have __exit__")

    def test_enter_returns_self(self):
        s = self._make_scheduler()
        result = s.__enter__()
        self.assertIs(result, s)

    def test_exit_calls_wt_manager_exit(self):
        """Scheduler.__exit__ must forward to WorktreeManager.__exit__."""
        s = self._make_scheduler()
        s.__exit__(None, None, None)
        s._wt_manager.__exit__.assert_called_once()

    def test_exit_swallows_wt_manager_exception(self):
        """If WorktreeManager.__exit__ raises, Scheduler.__exit__ must NOT re-raise."""
        s = self._make_scheduler()
        s._wt_manager.__exit__.side_effect = RuntimeError("boom")
        # Must not raise:
        try:
            s.__exit__(None, None, None)
        except Exception as exc:
            self.fail(f"Scheduler.__exit__ leaked an exception: {exc}")

    def test_exit_noop_when_no_wt_manager(self):
        """Scheduler.__exit__ must not crash when _wt_manager is None."""
        from scheduler import Scheduler
        s = Scheduler.__new__(Scheduler)
        s.instance = self.inst
        s.orch = self.orch
        s._wt_manager = None
        try:
            s.__exit__(None, None, None)
        except Exception as exc:
            self.fail(f"Scheduler.__exit__ raised when _wt_manager is None: {exc}")

    def test_context_manager_exit_on_exception(self):
        """Using Scheduler as a context manager fires __exit__ even when run() raises."""
        from scheduler import Scheduler
        s = self._make_scheduler()

        class _Boom(Exception):
            pass

        with patch.object(s, "run", side_effect=_Boom("crash")):
            with self.assertRaises(_Boom):
                with s:
                    s.run()
        # __exit__ on _wt_manager should have been called
        s._wt_manager.__exit__.assert_called_once()

    def test_context_manager_exit_on_keyboard_interrupt(self):
        """KeyboardInterrupt inside the with-block still fires Scheduler.__exit__."""
        s = self._make_scheduler()
        with patch.object(s, "run", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                with s:
                    s.run()
        s._wt_manager.__exit__.assert_called_once()

    def test_context_manager_exit_on_normal_return(self):
        """Normal exit also fires __exit__ (idempotent cleanup is fine)."""
        s = self._make_scheduler()
        with patch.object(s, "run", return_value=None):
            with s:
                s.run()
        s._wt_manager.__exit__.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
#  Suite 2 — WorktreeManager cleanup on abort paths (internal _run_task)
# ═════════════════════════════════════════════════════════════════════════════

class TestWorktreeCleanupOnAbort(unittest.TestCase):
    """Verifies that the Scheduler's run() wraps tasks in a context manager so
    WorktreeManager.__exit__ always fires on exception or KeyboardInterrupt."""

    def test_run_wraps_in_wt_context(self):
        """Scheduler.run() must enter the WorktreeManager context manager.

        We verify by checking that _wt_manager.__enter__ is called during run(),
        even when run() returns early (all_tasks_done immediately).
        """
        from scheduler import Scheduler

        inst = MagicMock()
        inst.all_tasks_done.return_value = True  # skip execution loop
        orch = MagicMock()

        s = Scheduler.__new__(Scheduler)
        s.instance = inst
        s.orch = orch
        s._wt_manager = MagicMock()
        s._wt_manager.__enter__ = MagicMock(return_value=s._wt_manager)
        s._wt_manager.__exit__ = MagicMock(return_value=None)

        # Patch out _project_review so it returns False immediately
        with patch.object(s, "_project_review", return_value=False):
            s.run()

        s._wt_manager.__enter__.assert_called_once()
        s._wt_manager.__exit__.assert_called_once()

    def test_run_wt_exit_on_exception_in_dispatch(self):
        """WorktreeManager.__exit__ fires even when _dispatch_batch raises."""
        from scheduler import Scheduler

        inst = MagicMock()
        # First call: tasks not done; second call would be done but we crash first
        inst.all_tasks_done.side_effect = [False]
        inst.failed_tasks.return_value = []
        orch = MagicMock()

        s = Scheduler.__new__(Scheduler)
        s.instance = inst
        s.orch = orch
        s._wt_manager = MagicMock()
        s._wt_manager.__enter__ = MagicMock(return_value=s._wt_manager)
        s._wt_manager.__exit__ = MagicMock(return_value=None)

        class _Boom(Exception):
            pass

        with patch.object(s, "_ready_tasks", return_value=["fake_task"]), \
             patch.object(s, "_dispatch_batch", side_effect=_Boom("dispatch crash")):
            with self.assertRaises(_Boom):
                s.run()

        s._wt_manager.__exit__.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
#  Suite 3 — memory_lint wired into run_continuation (--continue path)
# ═════════════════════════════════════════════════════════════════════════════

class TestMemoryLintContinuationPath(unittest.TestCase):
    """_run_memory_lint must be called on the run_continuation path."""

    def _stub_continuation_deps(self):
        """Return a patching context that stubs out heavy imports in main.py."""
        import tempfile, json
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        spec = {"goal": "test", "stack": "vanilla"}
        (tmp / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        (tmp / "tasks_done.json").write_text("[]", encoding="utf-8")

        # build a fake orchestrator response
        orch_mock = MagicMock()
        orch_mock.call.return_value = {"tasks": [
            {"id": "t1", "type": "backend", "objective": "x",
             "files": [], "dependencies": [], "acceptance_criteria": "",
             "verification": "none"}
        ]}

        return tmp, spec, orch_mock

    def test_memory_lint_called_on_continue(self):
        """run_continuation must call _run_memory_lint on the project dir."""
        import main as m

        tmp, spec, orch_mock = self._stub_continuation_deps()

        with patch.object(m, "make_orchestrator", return_value=orch_mock), \
             patch.object(m, "ProjectInstance") as pi_mock, \
             patch.object(m, "Scheduler") as sched_cls, \
             patch.object(m, "run_final_review", return_value=True), \
             patch.object(m, "write_handoff", return_value=tmp / "HANDOFF.md"), \
             patch.object(m, "try_claude_stamp"), \
             patch.object(m, "git_commit_project"), \
             patch.object(m, "deploy_project", return_value=(None, "")), \
             patch.object(m, "append_deploy_section"), \
             patch.object(m, "sw"), \
             patch.object(m, "cost_summary", return_value={}), \
             patch.object(m, "format_cost_line", return_value="$0.00"), \
             patch.object(m, "notify_build_outcome"), \
             patch.object(m, "_run_memory_lint") as ml_mock:

            # Fake ProjectInstance
            inst = MagicMock()
            inst.tasks = {}
            inst.tasks_as_list.return_value = []
            pi_mock.return_value = inst

            # Fake Scheduler as context manager
            sched_inst = MagicMock()
            sched_inst.__enter__ = MagicMock(return_value=sched_inst)
            sched_inst.__exit__ = MagicMock(return_value=None)
            sched_cls.return_value = sched_inst

            m.run_continuation("add login", tmp, auto_accept=True)

        ml_mock.assert_called_once_with(tmp)

    def test_memory_lint_failure_does_not_propagate_in_continue(self):
        """Even if _run_memory_lint raises internally, run_continuation must not crash."""
        import main as m

        tmp, spec, orch_mock = self._stub_continuation_deps()

        def _bad_lint(_path):
            raise RuntimeError("lint exploded")

        with patch.object(m, "make_orchestrator", return_value=orch_mock), \
             patch.object(m, "ProjectInstance") as pi_mock, \
             patch.object(m, "Scheduler") as sched_cls, \
             patch.object(m, "run_final_review", return_value=True), \
             patch.object(m, "write_handoff", return_value=tmp / "HANDOFF.md"), \
             patch.object(m, "try_claude_stamp"), \
             patch.object(m, "git_commit_project"), \
             patch.object(m, "deploy_project", return_value=(None, "")), \
             patch.object(m, "append_deploy_section"), \
             patch.object(m, "sw"), \
             patch.object(m, "cost_summary", return_value={}), \
             patch.object(m, "format_cost_line", return_value="$0.00"), \
             patch.object(m, "notify_build_outcome"), \
             patch.object(m, "_run_memory_lint", side_effect=_bad_lint):

            inst = MagicMock()
            inst.tasks = {}
            inst.tasks_as_list.return_value = []
            pi_mock.return_value = inst

            sched_inst = MagicMock()
            sched_inst.__enter__ = MagicMock(return_value=sched_inst)
            sched_inst.__exit__ = MagicMock(return_value=None)
            sched_cls.return_value = sched_inst

            # Should not raise despite the exploding lint
            try:
                m.run_continuation("add login", tmp, auto_accept=True)
            except RuntimeError as exc:
                self.fail(f"_run_memory_lint failure leaked into run_continuation: {exc}")


# ═════════════════════════════════════════════════════════════════════════════
#  Suite 4 — memory_lint wired into _handle_oversize (FORMAT-5 path)
# ═════════════════════════════════════════════════════════════════════════════

class TestMemoryLintOversizePath(unittest.TestCase):
    """_run_memory_lint must be called for each non-skipped sub-project in _handle_oversize."""

    def _minimal_oversize_response(self, names):
        return {
            "reason": "test decomposition",
            "sub_projects": [
                {"name": n, "goal": f"Build {n}", "depends_on": []}
                for n in names
            ],
        }

    def test_memory_lint_called_per_subproject(self):
        """_run_memory_lint is called once per non-skipped sub-project."""
        import tempfile
        import main as m

        tmp = Path(tempfile.mkdtemp())
        response = self._minimal_oversize_response(["scene_a", "scene_b"])

        with patch.object(m, "run_project", return_value=True), \
             patch.object(m, "_run_memory_lint") as ml_mock, \
             patch.object(m, "write_parent_handoff", return_value=tmp / "HANDOFF.md"), \
             patch.object(m, "try_claude_stamp"), \
             patch.object(m, "git_commit_project"), \
             patch.object(m, "deploy_project", return_value=(None, "")), \
             patch.object(m, "append_deploy_section"), \
             patch.object(m, "sw"), \
             patch.object(m, "cost_summary", return_value={"total_usd": 0.0, "paid_calls": 0}), \
             patch.object(m, "notify_build_outcome"), \
             patch.object(m, "console"):

            m._handle_oversize(response, tmp, depth=0, auto_accept=True)

        self.assertEqual(ml_mock.call_count, 2,
                         f"Expected 2 calls (one per sub-project), got {ml_mock.call_count}")

    def test_memory_lint_not_called_for_skipped_subproject(self):
        """Film assembly sub-projects that are skipped should NOT trigger memory_lint."""
        import tempfile
        import main as m

        tmp = Path(tempfile.mkdtemp())
        # 'scene_a' is real; 'assembly' matches the skip pattern and will be skipped
        response = {
            "reason": "film decomp",
            "sub_projects": [
                {"name": "scene_a", "goal": "Build scene", "depends_on": []},
                {"name": "assembly", "goal": "assemble film", "depends_on": ["scene_a"]},
            ],
        }

        with patch.object(m, "run_project", return_value=True), \
             patch.object(m, "_run_memory_lint") as ml_mock, \
             patch.object(m, "write_parent_handoff", return_value=tmp / "HANDOFF.md"), \
             patch.object(m, "try_claude_stamp"), \
             patch.object(m, "git_commit_project"), \
             patch.object(m, "deploy_project", return_value=(None, "")), \
             patch.object(m, "append_deploy_section"), \
             patch.object(m, "sw"), \
             patch.object(m, "cost_summary", return_value={"total_usd": 0.0, "paid_calls": 0}), \
             patch.object(m, "notify_build_outcome"), \
             patch.object(m, "console"), \
             patch.object(m, "_sub_project_stack", return_value="film"):

            m._handle_oversize(response, tmp, depth=0, auto_accept=True)

        # Only scene_a should have triggered lint; assembly was skipped
        called_paths = [c.args[0] for c in ml_mock.call_args_list]
        self.assertFalse(
            any("assembly" in str(p) for p in called_paths),
            f"memory_lint was called for a skipped assembly sub-project: {called_paths}"
        )

    def test_memory_lint_failure_does_not_abort_subproject(self):
        """Even if _run_memory_lint raises, the sub-project run_project call must proceed."""
        import tempfile
        import main as m

        tmp = Path(tempfile.mkdtemp())
        response = self._minimal_oversize_response(["scene_a"])

        run_project_called = []

        def _fake_run(*a, **kw):
            run_project_called.append(True)
            return True

        with patch.object(m, "run_project", side_effect=_fake_run), \
             patch.object(m, "_run_memory_lint", side_effect=RuntimeError("lint exploded")), \
             patch.object(m, "write_parent_handoff", return_value=tmp / "HANDOFF.md"), \
             patch.object(m, "try_claude_stamp"), \
             patch.object(m, "git_commit_project"), \
             patch.object(m, "deploy_project", return_value=(None, "")), \
             patch.object(m, "append_deploy_section"), \
             patch.object(m, "sw"), \
             patch.object(m, "cost_summary", return_value={"total_usd": 0.0, "paid_calls": 0}), \
             patch.object(m, "notify_build_outcome"), \
             patch.object(m, "console"):

            # Must not raise despite exploding lint
            try:
                m._handle_oversize(response, tmp, depth=0, auto_accept=True)
            except RuntimeError as exc:
                self.fail(f"_run_memory_lint failure aborted _handle_oversize: {exc}")

        self.assertTrue(run_project_called, "run_project was never called after a lint failure")


# ═════════════════════════════════════════════════════════════════════════════
#  Suite 5 — _run_memory_lint helper itself
# ═════════════════════════════════════════════════════════════════════════════

class TestRunMemoryLintHelper(unittest.TestCase):
    """_run_memory_lint is warn-only: never raises, always returns None."""

    def test_helper_present_in_main(self):
        """_run_memory_lint must be importable from main."""
        import main as m
        self.assertTrue(
            hasattr(m, "_run_memory_lint"),
            "_run_memory_lint not found in main module"
        )

    def test_helper_does_not_raise_on_missing_dir(self):
        """Calling _run_memory_lint on a non-existent dir must not raise."""
        import main as m
        try:
            result = m._run_memory_lint(Path("/nonexistent/path/abc123"))
        except Exception as exc:
            self.fail(f"_run_memory_lint raised on missing dir: {exc}")

    def test_helper_does_not_raise_when_lint_explodes(self):
        """If lint_project_memory itself raises, _run_memory_lint must swallow it."""
        import main as m
        with patch("main.lint_project_memory", side_effect=Exception("internal error")):
            try:
                m._run_memory_lint(Path("."))
            except Exception as exc:
                self.fail(f"_run_memory_lint leaked exception: {exc}")

    def test_helper_returns_none(self):
        """_run_memory_lint has no return value."""
        import main as m
        import tempfile
        d = Path(tempfile.mkdtemp())
        result = m._run_memory_lint(d)
        self.assertIsNone(result)

    def test_helper_invokes_lint_project_memory(self):
        """_run_memory_lint must call lint_project_memory with the given path."""
        import main as m
        import tempfile
        d = Path(tempfile.mkdtemp())
        with patch("main.lint_project_memory", return_value={"total": 0, "memory_present": False, "findings": []}) as mock_lint, \
             patch("main._ml_format_report", return_value="memory-lint: no project_memory/ — skipped."):
            m._run_memory_lint(d)
        mock_lint.assert_called_once_with(d)


# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    unittest.main(verbosity=2)
