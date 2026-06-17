"""Fixture-only tests for agent_dashboard.py.

Per the Codex-CLI review (RANK 6/8/10): verify against SYNTHETIC fixture data — never real session
transcripts — and assert agent shape/counts + the absence of the pipeline endpoints. No real Codex
job logs or *.output buffers are read here, so the test itself cannot leak context.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_dashboard as ad


def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj) if not isinstance(obj, str) else obj, encoding="utf-8")


def _paths_for(tmp: Path, session_id="sess-1") -> ad.Paths:
    """Build a Paths object then point it at fixture dirs (bypassing real autodetection)."""
    p = ad.Paths(tmp, session_id)
    p.codex_state_dir = tmp / "codex_state"
    p.tasks_dir = tmp / "tasks"
    p.transcript = tmp / f"{session_id}.jsonl"
    return p


class TestSlug(unittest.TestCase):
    def test_windows_path_slug_matches_claude_format(self):
        self.assertEqual(
            ad._slugify_repo(Path(r"C:\Users\Tyler\Desktop\Jarvis-Claw")),
            "C--Users-Tyler-Desktop-Jarvis-Claw",
        )


class TestTailPartialSafe(unittest.TestCase):
    def test_drops_trailing_partial_line(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.log"
            f.write_text("line1\nline2\n{partial-being-written", encoding="utf-8")
            lines = ad._tail_lines(f)
            self.assertEqual(lines, ["line1", "line2"])  # partial last line discarded

    def test_keeps_complete_last_line(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.log"
            f.write_text("a\nb\nc\n", encoding="utf-8")
            self.assertEqual(ad._tail_lines(f), ["a", "b", "c"])

    def test_missing_file_returns_empty_not_crash(self):
        self.assertEqual(ad._tail_lines(Path("does-not-exist.log")), [])


class TestCodexStatus(unittest.TestCase):
    def test_completed_job_is_done(self):
        st = ad._codex_status({"status": "completed", "phase": "done"}, now=1000.0)
        self.assertEqual(st["status"], "done")
        self.assertNotIn("cancel", st["actions"])

    def test_running_with_dead_pid_is_orphan(self):
        # The known companion bug: status stays 'running' after the process dies.
        st = ad._codex_status({"status": "running", "pid": 999999, "startedAt": None}, now=1000.0)
        self.assertEqual(st["status"], "orphan")
        self.assertEqual(st["confidence"], "high")

    def test_running_with_live_pid_is_running_and_cancelable(self):
        st = ad._codex_status({"status": "running", "pid": os.getpid()}, now=ad.time.time())
        self.assertEqual(st["status"], "running")
        self.assertIn("cancel", st["actions"])


class TestBuildAgents(unittest.TestCase):
    def test_codex_jobs_scoped_to_session_and_shaped(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            jobs = tmp / "codex_state" / "jobs"
            # current-session completed job
            _write(jobs / "task-aaa.json", {
                "id": "task-aaa", "kind": "task", "kindLabel": "rescue", "title": "Codex A",
                "summary": "do a thing", "sessionId": "sess-1", "status": "completed",
                "phase": "done", "pid": None, "logFile": str(jobs / "task-aaa.log"),
                "startedAt": "2026-06-16T20:51:41.109Z", "completedAt": "2026-06-16T20:52:41.101Z",
            })
            # other-session, finished -> excluded from live view (not a zombie: pid dead)
            _write(jobs / "task-bbb.json", {
                "id": "task-bbb", "sessionId": "other", "status": "completed", "pid": None,
                "title": "Codex B", "logFile": str(jobs / "task-bbb.log"),
            })
            paths = _paths_for(tmp)
            reg = ad.Registry()
            agents = ad.build_agents(paths, reg)
            ids = {a["id"] for a in agents}
            self.assertIn("codex:task-aaa", ids)
            self.assertNotIn("codex:task-bbb", ids)  # other-session + finished is History, not live
            a = next(a for a in agents if a["id"] == "codex:task-aaa")
            for key in ("status", "status_reason", "confidence", "actions", "kind", "title"):
                self.assertIn(key, a)
            # registry resolves the id; unknown id does not
            self.assertIsNotNone(reg.get("codex:task-aaa"))
            self.assertIsNone(reg.get("codex:task-zzz"))

    def test_empty_idle_task_buffer_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "tasks").mkdir(parents=True)
            empty = tmp / "tasks" / "dead.output"
            empty.write_text("", encoding="utf-8")
            old = ad.time.time() - 10_000
            os.utime(empty, (old, old))
            agents = ad.build_agents(_paths_for(tmp), ad.Registry())
            self.assertEqual([a for a in agents if a["id"] == "task:dead"], [])


class TestNoPipelineEndpoints(unittest.TestCase):
    """Codex RANK 8: a clean fork must not carry the pipeline's restart/continue/retry/mission_control."""

    def test_source_has_no_pipeline_control_surface(self):
        src = (Path(__file__).resolve().parent.parent / "agent_dashboard.py").read_text(encoding="utf-8")
        for forbidden in ("/api/restart", "/api/continue", "/api/retry_failed_task", "mission_control.json"):
            self.assertNotIn(forbidden, src, f"stale pipeline surface leaked: {forbidden}")

    def test_binds_localhost_by_default(self):
        src = (Path(__file__).resolve().parent.parent / "agent_dashboard.py").read_text(encoding="utf-8")
        self.assertIn('default="127.0.0.1"', src)
        self.assertNotIn('default="0.0.0.0"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
