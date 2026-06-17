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
        expected = self._repo.parent / ".jclaw_worktrees" / "task_003"
        self.assertEqual(path, expected)

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
# WorktreeManager.merge_and_remove
# ---------------------------------------------------------------------------

class TestWorktreeManagerMergeAndRemove(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="wt_test_"))
        self._repo = _make_fake_repo(self._tmp)
        self._wt = WorktreeManager(self._repo)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _register_task(self, task_id: str, branch: str = "wt-task-abcd1234") -> Path:
        """Directly insert a task into the manager's registry (bypassing create)."""
        wt_path = self._repo.parent / ".jclaw_worktrees" / task_id
        wt_path.mkdir(parents=True, exist_ok=True)
        self._wt._worktrees[task_id] = (wt_path, branch)
        return wt_path

    def test_merge_and_remove_issues_correct_commands(self):
        self._register_task("task_ok", "wt-task_ok-deadbeef")

        def _side_effect(args, cwd, check=True):
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return _ok(stdout="main\n")
            return _ok()

        with patch("worktree_manager._run", side_effect=_side_effect) as mock_run:
            self._wt.merge_and_remove("task_ok", into_branch="main")

        all_args = [c.args[0] for c in mock_run.call_args_list]

        # Must have: git add --all, git commit, git checkout <into_branch>,
        # git merge --no-ff <branch>, git worktree remove --force, git branch -D
        cmds = [tuple(a[:4]) for a in all_args]
        self.assertIn(("git", "add", "--all"), [tuple(a[:3]) for a in all_args])
        self.assertIn(("git", "commit", "-m"), [tuple(a[:3]) for a in all_args])
        checkout_calls = [a for a in all_args if a[:2] == ["git", "checkout"]]
        self.assertTrue(checkout_calls, "git checkout <into_branch> must be called")
        self.assertIn(("git", "merge", "--no-ff", "wt-task_ok-deadbeef"), cmds)
        self.assertIn(("git", "worktree", "remove", "--force"), cmds)
        branch_deletes = [a for a in all_args if a[:2] == ["git", "branch"] and "-D" in a]
        self.assertTrue(branch_deletes, "git branch -D must be called")

    def test_merge_and_remove_clears_registry(self):
        self._register_task("task_done", "wt-task_done-aabb1234")

        def _side_effect(args, cwd, check=True):
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return _ok(stdout="main\n")
            return _ok()

        with patch("worktree_manager._run", side_effect=_side_effect):
            self._wt.merge_and_remove("task_done")
        self.assertNotIn("task_done", self._wt._worktrees)

    def test_merge_and_remove_noop_for_unknown_task(self):
        with patch("worktree_manager._run") as mock_run:
            self._wt.merge_and_remove("ghost_task")
        mock_run.assert_not_called()

    def test_merge_failure_raises_and_still_cleans_up(self):
        self._register_task("task_conflict", "wt-task_conflict-cafe4321")

        def _side_effect(args, cwd, check=True):
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return _ok(stdout="main\n")
            if args[:3] == ["git", "merge", "--no-ff"]:
                return _fail("CONFLICT")
            return _ok()

        with patch("worktree_manager._run", side_effect=_side_effect):
            with self.assertRaises(RuntimeError) as ctx:
                self._wt.merge_and_remove("task_conflict")

        self.assertIn("git merge failed", str(ctx.exception))
        # Registry must be cleared even on merge failure.
        self.assertNotIn("task_conflict", self._wt._worktrees)

    def test_merge_restores_original_branch(self):
        """After merging into a different branch, HEAD is restored to original."""
        self._register_task("task_restore", "wt-task_restore-ffff0000")

        checkouts_seen = []

        def _side_effect(args, cwd, check=True):
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return _ok(stdout="feat/my-feature\n")
            if args[:2] == ["git", "checkout"]:
                checkouts_seen.append(args[2])
            return _ok()

        with patch("worktree_manager._run", side_effect=_side_effect):
            self._wt.merge_and_remove("task_restore", into_branch="main")

        self.assertIn("main", checkouts_seen, "must checkout into_branch before merging")
        self.assertIn("feat/my-feature", checkouts_seen, "must restore original branch after merge")

    def test_checkout_failure_raises_and_cleans_up(self):
        """If git checkout <into_branch> fails, raises RuntimeError and cleans up."""
        self._register_task("task_co_fail", "wt-task_co_fail-11223344")

        def _side_effect(args, cwd, check=True):
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return _ok(stdout="main\n")
            if args[:2] == ["git", "checkout"]:
                return _fail("pathspec 'missing-branch' did not match")
            return _ok()

        with patch("worktree_manager._run", side_effect=_side_effect):
            with self.assertRaises(RuntimeError) as ctx:
                self._wt.merge_and_remove("task_co_fail", into_branch="missing-branch")

        self.assertIn("git checkout", str(ctx.exception))
        self.assertNotIn("task_co_fail", self._wt._worktrees)

    def test_merge_lock_is_a_lock(self):
        """WorktreeManager must expose _merge_lock as a threading.Lock."""
        import threading
        wt = WorktreeManager(self._repo)
        self.assertIsInstance(wt._merge_lock, type(threading.Lock()))

    def test_detached_head_does_not_restore_branch(self):
        """Bug-1 regression: when HEAD is detached, git checkout 'HEAD' must NOT be called."""
        self._register_task("task_detached", "wt-task_detached-00001111")

        checkouts_seen = []

        def _side_effect(args, cwd, check=True):
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                # Simulate detached HEAD: git returns the literal string "HEAD".
                return _ok(stdout="HEAD\n")
            if args[:2] == ["git", "checkout"]:
                checkouts_seen.append(args[2])
            return _ok()

        with patch("worktree_manager._run", side_effect=_side_effect):
            self._wt.merge_and_remove("task_detached", into_branch="main")

        # The checkout to *into_branch* is expected.
        self.assertIn("main", checkouts_seen)
        # But "HEAD" (the detached-state sentinel) must NEVER be checked out.
        self.assertNotIn("HEAD", checkouts_seen, "must not git checkout 'HEAD' in detached state")


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
        # Create a fake stale directory that looks like a leftover from a crashed run.
        stale_path = self._repo.parent / ".jclaw_worktrees" / "task_stale"
        stale_path.mkdir(parents=True, exist_ok=True)

        prune_calls = []

        def _side_effect(args, cwd, check=True):
            if args[:3] == ["git", "worktree", "prune"]:
                prune_calls.append(args)
            return _ok()

        with patch("worktree_manager._run", side_effect=_side_effect):
            wt.create("task_stale")

        self.assertTrue(prune_calls, "git worktree prune must be called after removing a stale dir")


if __name__ == "__main__":
    unittest.main(verbosity=2)
