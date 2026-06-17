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
        w.on_deploy("https://x.netlify.app", "deployed")
        w.on_project_done("pass", "all good")

        records = _read_jsonl(w._session.path)
        events = [r["event"] for r in records]
        self.assertEqual(events[0], "mission_started")
        for expected in ("spec_accepted", "dag_loaded", "agent_call", "task_started",
                         "verification", "task_done", "deploy", "mission_finished"):
            self.assertIn(expected, events)
        seqs = [r["seq"] for r in records]
        self.assertEqual(seqs, sorted(seqs), "seq must be chronological")
        self.assertEqual(len(seqs), len(set(seqs)), "seq must be unique")
        ac = next(r for r in records if r["event"] == "agent_call")
        self.assertEqual(ac["provider"], "ollama")
        self.assertEqual(ac["rung"], 0)
        self.assertEqual(w._state["project"]["mission_id"], w._session.mission_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
