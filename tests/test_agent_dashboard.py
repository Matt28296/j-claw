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


class TestCodexTokens(unittest.TestCase):
    """Unit tests for _codex_tokens (parses usage from job["result"])."""

    def test_returns_none_when_no_result(self):
        self.assertIsNone(ad._codex_tokens({}))

    def test_returns_none_when_result_not_dict(self):
        self.assertIsNone(ad._codex_tokens({"result": "ok"}))

    def test_parses_nested_usage(self):
        job = {"result": {"usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}}}
        tok = ad._codex_tokens(job)
        self.assertEqual(tok, {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})

    def test_parses_flat_result(self):
        job = {"result": {"input_tokens": 200, "output_tokens": 80}}
        tok = ad._codex_tokens(job)
        self.assertEqual(tok["input_tokens"], 200)
        self.assertEqual(tok["output_tokens"], 80)

    def test_returns_none_when_no_numeric_fields(self):
        job = {"result": {"status": "ok"}}
        self.assertIsNone(ad._codex_tokens(job))


class TestSessionTokenTotals(unittest.TestCase):
    """Unit tests for _session_token_totals (per-model rollup)."""

    def test_empty_agents_returns_empty(self):
        self.assertEqual(ad._session_token_totals([]), {})

    def test_agents_without_tokens_skipped(self):
        agents = [{"kind": "codex", "tokens": None}, {"kind": "task", "tokens": None}]
        self.assertEqual(ad._session_token_totals(agents), {})

    def test_single_agent_totalled_under_label(self):
        agents = [{
            "kind": "codex", "label": "rescue",
            "tokens": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        }]
        totals = ad._session_token_totals(agents)
        self.assertIn("rescue", totals)
        self.assertEqual(totals["rescue"]["input_tokens"], 100)
        self.assertEqual(totals["rescue"]["total_tokens"], 150)

    def test_multiple_agents_same_label_accumulated(self):
        agents = [
            {"kind": "codex", "label": "rescue",
             "tokens": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140}},
            {"kind": "codex", "label": "rescue",
             "tokens": {"input_tokens": 200, "output_tokens": 60, "total_tokens": 260}},
        ]
        totals = ad._session_token_totals(agents)
        self.assertEqual(totals["rescue"]["input_tokens"], 300)
        self.assertEqual(totals["rescue"]["output_tokens"], 100)
        self.assertEqual(totals["rescue"]["total_tokens"], 400)

    def test_different_labels_bucketed_separately(self):
        agents = [
            {"kind": "codex", "label": "rescue",
             "tokens": {"total_tokens": 100}},
            {"kind": "codex", "label": "task",
             "tokens": {"total_tokens": 50}},
        ]
        totals = ad._session_token_totals(agents)
        self.assertEqual(totals["rescue"]["total_tokens"], 100)
        self.assertEqual(totals["task"]["total_tokens"], 50)

    def test_fallback_to_kind_when_no_label(self):
        agents = [{"kind": "codex", "label": None,
                   "tokens": {"total_tokens": 99}}]
        totals = ad._session_token_totals(agents)
        self.assertIn("codex", totals)
        self.assertEqual(totals["codex"]["total_tokens"], 99)

    def test_tolerant_mixed_agents(self):
        """Mix of agents with and without tokens — no crash, only token-bearing ones counted."""
        agents = [
            {"kind": "task", "label": None, "tokens": None},
            {"kind": "codex", "label": "rescue",
             "tokens": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
            {"kind": "codex", "label": "rescue", "tokens": {}},  # empty dict -> skipped
        ]
        totals = ad._session_token_totals(agents)
        self.assertEqual(list(totals.keys()), ["rescue"])
        self.assertEqual(totals["rescue"]["total_tokens"], 15)

    def test_totals_in_build_agents_response(self):
        """build_agents result feeds correctly into _session_token_totals (integration)."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            jobs = tmp / "codex_state" / "jobs"
            _write(jobs / "tok-aaa.json", {
                "id": "tok-aaa", "sessionId": "sess-1", "status": "completed",
                "title": "Token test job", "kindLabel": "rescue",
                "result": {"usage": {"input_tokens": 300, "output_tokens": 150, "total_tokens": 450}},
            })
            paths = _paths_for(tmp)
            reg = ad.Registry()
            agents = ad.build_agents(paths, reg)
            totals = ad._session_token_totals(agents)
            self.assertIn("rescue", totals)
            self.assertEqual(totals["rescue"]["total_tokens"], 450)


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
