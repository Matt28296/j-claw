"""test_wave4_worktree.py — HK1 regression: worktree DIRECTORY paths must be
namespaced so concurrent builds sharing an output base cannot collide.

The husk fix's safety rested on an undocumented invariant: worktree dirs were
keyed only by ``task_id`` (which resets to "task-001" per sub-project under
FORMAT-5 decomposition). Only the BRANCH got a random suffix; the PATH did not.
Two concurrent builds therefore collided on the same path and ``create()``
rmtree'd the other run's LIVE worktree -> fresh husk.

These tests assert:
  1. Two ``create()`` calls with the SAME task_id (different managers / builds)
     yield DISTINCT worktree directory paths (no collision).
  2. ``remove()`` targets the path that ``create()`` actually returned.

The tests stub out the git subprocess (``_run``) so they exercise the path
keying logic without needing a real repo or git operations.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import worktree_manager as wm
from worktree_manager import WorktreeManager


def _make_manager(repo_root: Path) -> WorktreeManager:
    """Build a WorktreeManager rooted at a real (git) dir without touching git.

    We bypass __init__'s repo discovery by constructing the object directly and
    setting the attributes it would set, so no actual git repo is required.
    """
    mgr = WorktreeManager.__new__(WorktreeManager)
    mgr.repo = repo_root
    mgr._base = repo_root.parent / wm._WORKTREE_DIRNAME
    mgr._worktrees = {}
    import threading

    mgr._merge_lock = threading.Lock()
    return mgr


class _RunRecorder:
    """Stand-in for worktree_manager._run that records calls and never shells out.

    git worktree add succeeds (rc 0); it also creates the target dir so the
    belt-and-suspenders ``wt_path.exists()`` branches behave realistically.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd, check=True):  # noqa: ANN001 - mirror real sig
        self.calls.append(list(args))
        # Emulate `git worktree add <path> -b <branch>` materializing the dir.
        if len(args) >= 4 and args[:3] == ["git", "worktree", "add"]:
            Path(args[3]).mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_same_task_id_distinct_paths() -> None:
    """Same task_id across two builds/managers -> distinct worktree dirs."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        original_run = wm._run
        wm._run = _RunRecorder()
        try:
            mgr_a = _make_manager(repo)
            mgr_b = _make_manager(repo)

            path_a = mgr_a.create("task-001")
            path_b = mgr_b.create("task-001")

            assert path_a != path_b, (
                "Two concurrent builds with the same task_id collided on the "
                f"same worktree path ({path_a!r}) — husk regression."
            )
            # Both must still live under the shared base, and encode the task_id.
            assert path_a.parent == mgr_a._base
            assert path_b.parent == mgr_b._base
            assert path_a.name.startswith("task-001-")
            assert path_b.name.startswith("task-001-")
        finally:
            wm._run = original_run


def test_repeated_create_same_manager_distinct_paths() -> None:
    """Even within one manager, two create() of the same task_id differ."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        original_run = wm._run
        wm._run = _RunRecorder()
        try:
            mgr = _make_manager(repo)
            p1 = mgr.create("task-001")
            p2 = mgr.create("task-001")
            assert p1 != p2, (
                f"Re-creating task-001 reused the same path {p1!r} — the stale-dir "
                "rmtree in create() would wipe a live worktree."
            )
        finally:
            wm._run = original_run


def test_remove_targets_created_path() -> None:
    """remove() must clean up the exact directory create() returned."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        recorder = _RunRecorder()
        original_run = wm._run
        wm._run = recorder
        try:
            mgr = _make_manager(repo)
            created = mgr.create("task-001")
            assert created.exists(), "create() should have materialized the dir"

            recorder.calls.clear()
            mgr.remove("task-001")

            # The cleanup must issue `git worktree remove --force <created path>`
            # for the EXACT path create() returned (not the bare task_id dir).
            remove_calls = [
                c for c in recorder.calls
                if c[:3] == ["git", "worktree", "remove"]
            ]
            assert remove_calls, "remove() did not invoke `git worktree remove`"
            targeted = remove_calls[-1][-1]
            assert targeted == str(created), (
                f"remove() targeted {targeted!r} but create() returned {created!r} "
                "— removal keyed on the wrong path would orphan the worktree."
            )
            # And the bookkeeping entry is gone.
            assert "task-001" not in mgr._worktrees
        finally:
            wm._run = original_run


def test_stale_namespaced_dir_is_pruned() -> None:
    """Stale-dir cleanup must still fire on the NAMESPACED path.

    Reviewer gap: the old bare-path prune test staged the stale dir at the bare
    ``task_id`` path, which the namespaced ``create()`` can never collide with —
    so the rmtree+prune branch was never exercised. Here we pin
    ``secrets.token_hex`` to a fixed suffix and pre-stage the stale dir at the
    EXACT namespaced path ``{task_id}-{suffix}`` so ``wt_path.exists()`` is True
    and the cleanup branch actually runs.
    """
    fixed_suffix = "deadbeef"
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()

        recorder = _RunRecorder()
        original_run = wm._run
        original_token_hex = wm.secrets.token_hex
        wm._run = recorder
        wm.secrets.token_hex = lambda n=4: fixed_suffix
        try:
            mgr = _make_manager(repo)
            # Pre-stage a stale leftover at the namespaced path create() will target.
            stale = mgr._base / f"task-001-{fixed_suffix}"
            stale.mkdir(parents=True, exist_ok=True)
            (stale / "leftover.txt").write_text("husk", encoding="utf-8")

            created = mgr.create("task-001")

            # create() must have targeted the staged stale path...
            assert created == stale, (
                f"create() targeted {created!r}, expected staged stale path {stale!r}"
            )
            # ...and pruned the git admin entry after rmtree'ing it.
            prune_calls = [
                c for c in recorder.calls
                if c[:3] == ["git", "worktree", "prune"]
            ]
            assert prune_calls, (
                "git worktree prune was not called — the stale-dir rmtree+prune "
                "branch did not fire on the namespaced path."
            )
            # The stale leftover file must have been wiped by the rmtree.
            assert not (stale / "leftover.txt").exists(), (
                "stale leftover survived — rmtree branch did not run"
            )
        finally:
            wm._run = original_run
            wm.secrets.token_hex = original_token_hex


def _run_all() -> int:
    tests = [
        test_same_task_id_distinct_paths,
        test_repeated_create_same_manager_distinct_paths,
        test_remove_targets_created_path,
        test_stale_namespaced_dir_is_pruned,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001 - test harness
            failures += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    total = len(tests)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
