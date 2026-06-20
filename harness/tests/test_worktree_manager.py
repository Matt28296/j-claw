"""Tests for worktree_manager.WorktreeManager.

All git subprocess calls are mocked so the tests run without a real git repo
and without touching the filesystem beyond a temporary directory.
"""
from __future__ import annotations

import os
import sys
import tempfile
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, call, MagicMock

_HARNESS = os.path.join(os.path.dirname(__file__), "..")
if _HARNESS not in sys.path:
    sys.path.insert(0, _HARNESS)

from worktree_manager import WorktreeManager, _find_repo_root


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_repo(tmp: Path) -> Path:
    """Create a minimal directory tree that looks like a git repo."""
    repo = tmp / "fake_repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr: str = "error") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# _find_repo_root
# ---------------------------------------------------------------------------

class TestFindRepoRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="wt_test_"))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_finds_root_at_given_path(self):
        repo = _make_fake_repo(self._tmp)
        self.assertEqual(_find_repo_root(repo), repo)

    def test_finds_root_from_subdir(self):
        repo = _make_fake_repo(self._tmp)
        sub = repo / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(_find_repo_root(sub), repo)

    def test_returns_none_when_no_git(self):
        bare_dir = self._tmp / "no_git"
        bare_dir.mkdir()
        self.assertIsNone(_find_repo_root(bare_dir))


# ---------------------------------------------------------------------------
# WorktreeManager.__init__
# ---------------------------------------------------------------------------

class TestWorktreeManagerInit(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="wt_test_"))
        self._repo = _make_fake_repo(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_init_sets_repo(self):
        wt = WorktreeManager(self._repo)
        self.assertEqual(wt.repo, self._repo)

    def test_init_from_subdir(self):
        sub = self._repo / "harness" / "projects"
        sub.mkdir(parents=True)
        wt = WorktreeManager(sub)
        self.assertEqual(wt.repo, self._repo)

    def test_init_raises_when_no_repo(self):
        bare = self._tmp / "no_repo"
        bare.mkdir()
        with self.assertRaises(ValueError):
            WorktreeManager(bare)


# ---------------------------------------------------------------------------
# WorktreeManager.create
# ---------------------------------------------------------------------------

class TestWorktreeManagerCreate(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="wt_test_"))
        self._repo = _make_fake_repo(self._tmp)
        self._wt = WorktreeManager(self._repo)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_create_calls_git_worktree_add(self):
        with patch("worktree_manager._run", return_value=_ok()) as mock_run:
            path = self._wt.create("task_001")

        # Verify the worktree add command was issued.
        calls = mock_run.call_args_list
        add_calls = [c for c in calls if "worktree" in c.args[0] and "add" in c.args[0]]
        self.assertEqual(len(add_calls), 1)
        args = add_calls[0].args[0]
        self.assertEqual(args[0], "git")
        self.assertEqual(args[1], "worktree")
        self.assertEqual(args[2], "add")
        # args[3] is the path, args[4] is "-b", args[5] is the branch name
        self.assertEqual(args[4], "-b")
        self.assertIn("wt-task_001-", args[5])

    def test_create_registers_task(self):
        with patch("worktree_manager._run", return_value=_ok()):
            self._wt.create("task_002")
        self.assertIn("task_002", self._wt._worktrees)

    def test_create_returns_correct_path(self):
        with patch("worktree_manager._run", return_value=_ok()):
            path = self._wt.create("task_003")
        # The worktree dir is namespaced as "<task_id>-<random suffix>" (HK1 fix)
        # so concurrent builds sharing an output base can't collide on the same path.
        self.assertEqual(path.parent, self._repo.parent / ".jclaw_worktrees")
        self.assertTrue(path.name.startswith("task_003-"),
                        f"expected namespaced 'task_003-<suffix>', got {path.name!r}")

    def test_create_raises_on_git_failure(self):
        with patch("worktree_manager._run", return_value=_fail("some git error")):
            with self.assertRaises(RuntimeError) as ctx:
                self._wt.create("task_fail")
        self.assertIn("git worktree add failed", str(ctx.exception))

    def test_create_raises_propagates_to_caller(self):
        """Simulate subprocess.run raising (e.g. git not on PATH)."""
        with patch("worktree_manager._run", side_effect=FileNotFoundError("git not found")):
            with self.assertRaises(FileNotFoundError):
                self._wt.create("task_exc")


# ---------------------------------------------------------------------------
# WorktreeManager.remove
# ---------------------------------------------------------------------------

class TestWorktreeManagerRemove(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="wt_test_"))
        self._repo = _make_fake_repo(self._tmp)
        self._wt = WorktreeManager(self._repo)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _register_task(self, task_id: str, branch: str = "wt-task-abcd1234") -> Path:
        wt_path = self._repo.parent / ".jclaw_worktrees" / task_id
        wt_path.mkdir(parents=True, exist_ok=True)
        self._wt._worktrees[task_id] = (wt_path, branch)
        return wt_path

    def test_remove_calls_worktree_remove_and_branch_delete(self):
        self._register_task("task_fail", "wt-task_fail-12345678")
        with patch("worktree_manager._run", return_value=_ok()) as mock_run:
            self._wt.remove("task_fail")

        all_args = [c.args[0] for c in mock_run.call_args_list]
        # Must have: git worktree remove --force, git branch -D
        wt_removes = [a for a in all_args if a[:3] == ["git", "worktree", "remove"]]
        branch_deletes = [a for a in all_args if "branch" in a and "-D" in a]
        self.assertTrue(wt_removes, "git worktree remove must be called")
        self.assertTrue(branch_deletes, "git branch -D must be called")

    def test_remove_does_NOT_call_merge(self):
        self._register_task("task_discard", "wt-task_discard-99998888")
        with patch("worktree_manager._run", return_value=_ok()) as mock_run:
            self._wt.remove("task_discard")

        all_args = [c.args[0] for c in mock_run.call_args_list]
        merges = [a for a in all_args if "merge" in a]
        self.assertFalse(merges, "remove() must NOT call git merge")

    def test_remove_clears_registry(self):
        self._register_task("task_bad", "wt-task_bad-aabbccdd")
        with patch("worktree_manager._run", return_value=_ok()):
            self._wt.remove("task_bad")
        self.assertNotIn("task_bad", self._wt._worktrees)

    def test_remove_noop_for_unknown_task(self):
        with patch("worktree_manager._run") as mock_run:
            self._wt.remove("nobody")
        mock_run.assert_not_called()

    def test_remove_calls_worktree_prune(self):
        """_cleanup_worktree must call git worktree prune to clear stale entries."""
        self._register_task("task_prune", "wt-task_prune-aabbccdd")
        with patch("worktree_manager._run", return_value=_ok()) as mock_run:
            self._wt.remove("task_prune")

        all_args = [c.args[0] for c in mock_run.call_args_list]
        prune_calls = [a for a in all_args if a[:3] == ["git", "worktree", "prune"]]
        self.assertTrue(prune_calls, "git worktree prune must be called during cleanup")

    def test_remove_acquires_merge_lock(self):
        """Bug-3 regression: remove() must hold _merge_lock to prevent concurrent git corruption."""
        self._register_task("task_lock_check", "wt-task_lock_check-11223344")

        lock_held_during_cleanup = []

        def _side_effect(args, cwd, check=True):
            # Check if the lock is held at the moment git commands run inside remove().
            lock_held_during_cleanup.append(not self._wt._merge_lock.acquire(blocking=False))
            if not lock_held_during_cleanup[-1]:
                # We acquired it ourselves (lock was free) — release immediately.
                self._wt._merge_lock.release()
            return _ok()

        with patch("worktree_manager._run", side_effect=_side_effect):
            self._wt.remove("task_lock_check")

        self.assertTrue(
            any(lock_held_during_cleanup),
            "_merge_lock must be held during remove() cleanup git calls",
        )


# ---------------------------------------------------------------------------
# Context manager __exit__ cleanup
# ---------------------------------------------------------------------------

class TestWorktreeManagerContextManager(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="wt_test_"))
        self._repo = _make_fake_repo(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_exit_cleans_up_remaining_worktrees(self):
        wt = WorktreeManager(self._repo)
        # Register two tasks without creating real worktree dirs.
        base = self._repo.parent / ".jclaw_worktrees"
        for tid in ("orphan_1", "orphan_2"):
            p = base / tid
            p.mkdir(parents=True, exist_ok=True)
            wt._worktrees[tid] = (p, f"wt-{tid}-deadbeef")

        with patch("worktree_manager._run", return_value=_ok()) as mock_run:
            wt.__exit__(None, None, None)

        self.assertEqual(len(wt._worktrees), 0, "all orphans must be cleaned up")
        all_args = [c.args[0] for c in mock_run.call_args_list]
        branch_deletes = [a for a in all_args if "branch" in a and "-D" in a]
        self.assertEqual(len(branch_deletes), 2)

    def test_context_manager_protocol(self):
        wt = WorktreeManager(self._repo)
        with patch("worktree_manager._run", return_value=_ok()):
            with wt as ctx:
                self.assertIs(ctx, wt)
        # No uncaught exception means __enter__/__exit__ work.


# ---------------------------------------------------------------------------
# _cleanup_worktree: Windows read-only file regression
# ---------------------------------------------------------------------------

class TestCleanupReadOnly(unittest.TestCase):
    """Regression: _cleanup_worktree must fully remove dirs containing read-only files.

    On Windows, git objects and pack files are marked read-only.  The old
    ``shutil.rmtree(path, ignore_errors=True)`` silently swallowed the
    PermissionError and left the directory behind.  The onerror handler added
    in task (1) chmods offending files writable before retrying the unlink so
    the directory is actually deleted.
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="wt_test_"))
        self._repo = _make_fake_repo(self._tmp)
        self._wt = WorktreeManager(self._repo)

    def tearDown(self):
        # Best-effort: make everything writable before cleanup in case the test
        # left read-only artefacts behind (e.g. if the assertion failed).
        for dirpath, dirnames, filenames in os.walk(str(self._tmp)):
            for fname in filenames:
                try:
                    os.chmod(os.path.join(dirpath, fname), stat.S_IWRITE)
                except Exception:
                    pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_cleanup_removes_readonly_files(self):
        """A worktree dir with a read-only file is fully deleted by _cleanup_worktree."""
        import stat as _stat

        # Build a fake worktree directory containing a read-only file.
        wt_path = self._repo.parent / ".jclaw_worktrees" / "task_ro-abcd1234"
        wt_path.mkdir(parents=True, exist_ok=True)
        ro_file = wt_path / "pack-deadbeef.idx"  # simulates a read-only git pack file
        ro_file.write_text("fake git object", encoding="utf-8")
        # Mark the file read-only (same as git does on Windows for objects).
        os.chmod(str(ro_file), _stat.S_IREAD)

        branch = "wt-task_ro-abcd1234"
        self._wt._worktrees["task_ro"] = (wt_path, branch)

        # git commands are mocked — we're testing the filesystem behaviour only.
        with patch("worktree_manager._run", return_value=_ok()):
            self._wt._cleanup_worktree("task_ro", wt_path, branch)

        self.assertFalse(
            wt_path.exists(),
            f"worktree dir must be fully removed even with read-only files inside; "
            f"dir still exists: {wt_path}",
        )

    def test_cleanup_handler_does_not_raise_on_permission_error(self):
        """The onerror handler must swallow exceptions and never propagate out of cleanup."""
        import stat as _stat

        wt_path = self._repo.parent / ".jclaw_worktrees" / "task_safe-cafe5678"
        wt_path.mkdir(parents=True, exist_ok=True)
        ro_file = wt_path / "readonly.idx"
        ro_file.write_text("data", encoding="utf-8")
        os.chmod(str(ro_file), _stat.S_IREAD)

        branch = "wt-task_safe-cafe5678"
        self._wt._worktrees["task_safe"] = (wt_path, branch)

        # Even if git commands fail, _cleanup_worktree must not raise.
        with patch("worktree_manager._run", return_value=_ok()):
            try:
                self._wt._cleanup_worktree("task_safe", wt_path, branch)
            except Exception as exc:
                self.fail(f"_cleanup_worktree raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# Graceful degradation: WorktreeManager not available
# ---------------------------------------------------------------------------

class TestWorktreeManagerDegradation(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="wt_test_"))
        self._repo = _make_fake_repo(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_create_failure_raises_for_caller_to_catch(self):
        """If git worktree add fails, create() raises so the scheduler can fall back."""
        wt = WorktreeManager(self._repo)
        with patch("worktree_manager._run", return_value=_fail("git: not supported")):
            with self.assertRaises(RuntimeError):
                wt.create("task_xyz")

    def test_init_raises_for_non_repo_path(self):
        """Scheduler catches ValueError from __init__ and sets _wt_manager=None."""
        no_git_dir = self._tmp / "no_git"
        no_git_dir.mkdir()
        with self.assertRaises(ValueError):
            WorktreeManager(no_git_dir)

    def test_create_prunes_after_removing_stale_dir(self):
        """Bug-4 regression: if a stale wt_path directory exists, git worktree prune is called
        after removing it so the crashed run's admin entry doesn't block the new add."""
        wt = WorktreeManager(self._repo)
        # The worktree dir is namespaced "<task_id>-<suffix>" (HK1 fix), so pin the
        # random suffix to stage the stale dir at the exact path create() will target.
        fixed_suffix = "deadbeef"
        stale_path = self._repo.parent / ".jclaw_worktrees" / f"task_stale-{fixed_suffix}"
        stale_path.mkdir(parents=True, exist_ok=True)

        prune_calls = []

        def _side_effect(args, cwd, check=True):
            if args[:3] == ["git", "worktree", "prune"]:
                prune_calls.append(args)
            return _ok()

        with patch("worktree_manager.secrets.token_hex", return_value=fixed_suffix), \
                patch("worktree_manager._run", side_effect=_side_effect):
            wt.create("task_stale")

        self.assertTrue(prune_calls, "git worktree prune must be called after removing a stale dir")


if __name__ == "__main__":
    unittest.main(verbosity=2)
