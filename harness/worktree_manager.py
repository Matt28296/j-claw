"""worktree_manager.py — git worktree isolation for parallel task execution.

Each code task gets its own git worktree on a temporary branch so that:
  - Parallel workers cannot stomp on each other's files.
  - A failing task leaves zero partial writes in the shared tree.

The "repo" is the j-claw HARNESS repo root (the parent of the harness/ package
directory).  Generated project files live under harness/projects/<slug>/, which
is a subdirectory of that repo — so git tracks them and worktrees can isolate them.

Usage (lifecycle managed by Scheduler.run):

    with WorktreeManager(repo_root) as wt:
        wt_path = wt.create(task_id)   # isolated branch
        # worker writes to wt_path / relative_output
        # _copy_tree copies files to the real output_dir, then:
        wt.remove(task_id)             # discard the worktree (pass or fail)
"""
from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
import threading
from pathlib import Path

# Worktrees are created as siblings of the repo root so they never nest inside
# the repo (which would confuse git).  The directory is gitignored at the root.
_WORKTREE_DIRNAME = ".jclaw_worktrees"

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "j-claw",
    "GIT_AUTHOR_EMAIL": "jclaw@local",
    "GIT_COMMITTER_NAME": "j-claw",
    "GIT_COMMITTER_EMAIL": "jclaw@local",
}


def _force_remove_readonly(func, path, excinfo):
    """shutil.rmtree onerror handler: on Windows, git objects/pack files are
    marked read-only, so the default rmtree fails to delete them.  Make the
    offending path writable and retry the removal.  Never raises out of cleanup."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def _run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command, capturing stdout+stderr."""
    return subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=check,
    )


def _find_repo_root(start: Path) -> Path | None:
    """Walk up from *start* to find the root of the enclosing git repo.
    Returns None if no .git directory is found up to the filesystem root.
    """
    current = start.resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


class WorktreeManager:
    """Manage temporary git worktrees for task isolation.

    Parameters
    ----------
    project_repo_path:
        Any path inside (or equal to) the j-claw git repo.  The manager will
        walk up to find the actual repo root so callers can pass
        ``harness_dir``, ``output_dir``, or the repo root itself.
    """

    def __init__(self, project_repo_path: str | Path) -> None:
        repo = _find_repo_root(Path(project_repo_path))
        if repo is None:
            raise ValueError(
                f"No git repository found at or above {project_repo_path!r}. "
                "Worktree isolation requires a git repo."
            )
        self.repo: Path = repo
        self._base: Path = self.repo.parent / _WORKTREE_DIRNAME
        self._worktrees: dict[str, tuple[Path, str]] = {}  # task_id -> (path, branch)
        # Serializes merge operations: git does not support concurrent merges
        # into the same working tree.  Worktree creation/removal are thread-safe
        # (each operates on its own isolated path), but merge must be sequential.
        self._merge_lock: threading.Lock = threading.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def create(self, task_id: str) -> Path:
        """Create a git worktree for *task_id*.

        Returns the worktree root path.  The caller is responsible for
        writing task output files inside this tree (under the same relative
        path as the main output_dir uses within the repo).

        Raises on git failure — callers should catch and fall back to
        non-isolated execution.
        """
        suffix = secrets.token_hex(4)
        branch = f"wt-{task_id}-{suffix}"
        # Namespace the worktree DIRECTORY with the same random suffix as the
        # branch (not just task_id). task_id resets to "task-001" per sub-project
        # under FORMAT-5 decomposition, so two concurrent builds sharing an output
        # base would otherwise collide on the same path — and the stale-dir rmtree
        # below would wipe the OTHER run's live worktree (husk regression). The
        # suffix makes every create() target a distinct directory.
        wt_path = self._base / f"{task_id}-{suffix}"

        # Remove stale worktree dir if it exists from a previous crashed run, and
        # prune the corresponding git admin entry so the next add can succeed cleanly.
        # Use the read-only handler (not ignore_errors) so stale read-only git
        # objects are actually deleted — otherwise the dir lingers and the
        # subsequent `git worktree add` fails because the path is not empty.
        if wt_path.exists():
            shutil.rmtree(wt_path, onerror=_force_remove_readonly)
            _run(["git", "worktree", "prune"], cwd=self.repo, check=False)

        self._base.mkdir(parents=True, exist_ok=True)

        result = _run(
            ["git", "worktree", "add", str(wt_path), "-b", branch],
            cwd=self.repo,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed for task {task_id!r}:\n{result.stderr.strip()}"
            )

        self._worktrees[task_id] = (wt_path, branch)
        return wt_path

    def remove(self, task_id: str) -> None:
        """Discard the worktree without merging (verification failed or error)."""
        if task_id not in self._worktrees:
            return
        wt_path, branch = self._worktrees[task_id]
        with self._merge_lock:
            self._cleanup_worktree(task_id, wt_path, branch)

    # ── internal ──────────────────────────────────────────────────────────────

    def _cleanup_worktree(self, task_id: str, wt_path: Path, branch: str) -> None:
        """Remove the worktree directory and delete the temporary branch."""
        _run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=self.repo,
            check=False,
        )
        # Belt-and-suspenders: remove the directory if git left it behind.
        # On Windows, git objects/pack files are marked read-only; the onerror
        # handler chmods them writable before retrying the unlink so they are
        # actually deleted instead of silently skipped.
        if wt_path.exists():
            shutil.rmtree(wt_path, onerror=_force_remove_readonly)
        # Prune stale .git/worktrees/<name> admin entries left by crashes.
        _run(["git", "worktree", "prune"], cwd=self.repo, check=False)
        _run(
            ["git", "branch", "-D", branch],
            cwd=self.repo,
            check=False,
        )
        self._worktrees.pop(task_id, None)

    # ── context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "WorktreeManager":
        return self

    def __exit__(self, *_) -> None:
        """Safety net: discard any worktrees that were not explicitly closed."""
        for task_id in list(self._worktrees):
            wt_path, branch = self._worktrees[task_id]
            self._cleanup_worktree(task_id, wt_path, branch)
