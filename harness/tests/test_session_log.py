"""Tests for the append-only session log (roadmap item #5) and its StateWriter integration.

All disk writes go to a temp dir (session_log.SESSIONS_DIR is patched) and StateWriter's
mission_control.json write is suppressed, so neither the repo's sessions/ nor mission_control.json
is touched.
"""
from __future__ import annotations
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HARNESS = os.path.join(os.path.dirname(__file__), "..")
if _HARNESS not in sys.path:
    sys.path.insert(0, _HARNESS)

import session_log
from session_log import SessionLog, new_mission_id
from state_writer import StateWriter


class _NoWriteStateWriter(StateWriter):
    """Suppress the mission_control.json snapshot write; the session log still writes to disk."""
    def _write(self):
        pass


def _read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class TestSessionLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="sesslog_test_")
        self._patch = patch.object(session_log, "SESSIONS_DIR", Path(self._tmp))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_append_only_with_seq_and_correlation(self):
        log = SessionLog("mid-1", intent="build a thing", output_dir="/out")
        log.emit("worker_attempt", task_id="t1", provider="ollama", rung=0)
        log.emit("verification", task_id="t1", passed=True)
        records = _read_jsonl(log.path)
        self.assertEqual([r["event"] for r in records],
                         ["mission_started", "worker_attempt", "verification"])
        self.assertEqual([r["seq"] for r in records], [1, 2, 3])
        self.assertTrue(all(r["mission_id"] == "mid-1" for r in records))
        self.assertTrue(all("ts" in r for r in records))
        self.assertEqual(records[1]["task_id"], "t1")

    def test_emit_never_raises_on_unserializable(self):
        log = SessionLog("mid-2")
        log.emit("weird", obj=object())  # non-JSON value must not raise (default=str)
        records = _read_jsonl(log.path)
        self.assertEqual(records[-1]["event"], "weird")

    def test_new_mission_id_unique(self):
        self.assertNotEqual(new_mission_id(), new_mission_id())

    def test_statewriter_emits_replayable_transcript(self):
        w = _NoWriteStateWriter()
        w.on_project_start("make a site", "/tmp/out")
        w.on_spec_accepted({"goal": "g", "complexity": "mvp",
                            "architecture": {"stack": "vanilla"}})
        w.on_dag_loaded([{"id": "t1", "type": "frontend", "objective": "o"}])
        w.on_agent_call("code_worker", "qwen3:8b", "EXECUTING", task_id="t1",
                        provider="ollama", rung=0)
        w.on_task_start("t1")
        w.on_verification_result("t1", "smoke", "vanilla", True, "ok")
        w.on_task_done("t1", "ollama/qwen3:8b")
        w.on_tasks_added([{"id": "t2", "type": "frontend", "objective": "heal it"}])
        w.on_dynamic_checks(True, [])
        w.on_final_review_result(True, "PASS", heal_cycle=1)
        w.on_deploy("https://x.netlify.app", "deployed")
        _ended = w._session  # capture before on_project_done pops the stack
        session_path = _ended.path
        w.on_project_done("pass", "all good")
        self.assertIsNone(w._session, "the run's session is popped on terminal")

        records = _read_jsonl(session_path)
        events = [r["event"] for r in records]
        self.assertEqual(events[0], "mission_started")
        for expected in ("spec_accepted", "dag_loaded", "agent_call", "task_started",
                         "verification", "task_done", "tasks_added", "dynamic_checks",
                         "final_review", "deploy", "mission_finished"):
            self.assertIn(expected, events)
        seqs = [r["seq"] for r in records]
        self.assertEqual(seqs, sorted(seqs), "seq must be chronological")
        self.assertEqual(len(seqs), len(set(seqs)), "seq must be unique")
        ac = next(r for r in records if r["event"] == "agent_call")
        self.assertEqual(ac["provider"], "ollama")
        self.assertEqual(ac["rung"], 0)
        self.assertEqual(w._state["project"]["mission_id"], _ended.mission_id)

    def test_format5_nested_sessions_do_not_clobber_parent(self):
        """A recursive sub-project must get its OWN transcript correlated to the parent, and the
        parent's terminal event must land in the PARENT file (the FORMAT-5 ownership bug)."""
        w = _NoWriteStateWriter()
        w.on_project_start("parent build", "/out/parent")
        parent_sess = w._session
        parent_id = parent_sess.mission_id

        # Sub-project starts + finishes WITHIN the parent run.
        w.on_project_start("child scene", "/out/parent/scene1")
        child_sess = w._session
        self.assertIsNot(child_sess, parent_sess)
        w.on_task_done("c1", "ollama/qwen3:8b")          # → child transcript
        w.on_project_done("pass", "child done")          # child terminal + pop → parent current
        self.assertIs(w._session, parent_sess, "parent resumes as current after the sub-run ends")

        w.on_project_done("pass", "parent done")         # parent terminal
        self.assertIsNone(w._session)

        parent_records = _read_jsonl(parent_sess.path)
        child_records = _read_jsonl(child_sess.path)
        parent_finishes = [r for r in parent_records if r["event"] == "mission_finished"]
        child_finishes = [r for r in child_records if r["event"] == "mission_finished"]
        self.assertEqual([r["summary"] for r in parent_finishes], ["parent done"])
        self.assertEqual([r["summary"] for r in child_finishes], ["child done"])
        child_started = next(r for r in child_records if r["event"] == "mission_started")
        self.assertEqual(child_started["parent_mission_id"], parent_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
