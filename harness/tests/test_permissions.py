"""Tests for the action-risk classifier and its observe-only logging (roadmap item #6).

classify_action is pure/deterministic. observe() must classify + emit a risk_classified event into
the session log WITHOUT blocking, and never raise (even with no active run). All disk writes go to a
temp dir; the StateWriter singleton is patched so mission_control.json is untouched.
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

import permissions
import session_log
import state_writer
from state_writer import StateWriter


class _NoWriteStateWriter(StateWriter):
    def _write(self):
        pass


def _read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class TestClassifyAction(unittest.TestCase):
    def test_blast_radius_levels(self):
        c = permissions.classify_action
        self.assertEqual(c("deploy_hook")[0], "critical")
        self.assertEqual(c("deploy")[0], "critical")
        self.assertEqual(c("install")[0], "high")
        self.assertEqual(c("git", "commit")[0], "low")
        self.assertEqual(c("git", "git push origin main")[0], "high")
        self.assertEqual(c("fs_delete")[0], "medium")
        self.assertEqual(c("llm_cli")[0], "medium")
        self.assertEqual(c("test")[0], "low")
        self.assertEqual(c("build")[0], "low")
        self.assertEqual(c("render")[0], "low")
        self.assertEqual(c("shell")[0], "high")  # LLM-authored script exec — arbitrary local code
        self.assertEqual(c("mystery_kind")[0], "medium")  # unclassified → medium, with a reason
        self.assertTrue(c("mystery_kind")[1])

    def test_risk_rank_ordering_and_unknown(self):
        self.assertLess(permissions.risk_rank("low"), permissions.risk_rank("high"))
        self.assertLess(permissions.risk_rank("high"), permissions.risk_rank("critical"))
        # an unknown level ranks as most-severe so it can't slip under a future threshold
        self.assertEqual(permissions.risk_rank("nonsense"), len(permissions.RISK_LEVELS))


class TestObserveLogging(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="perm_test_")
        self._p_dir = patch.object(session_log, "SESSIONS_DIR", Path(self._tmp))
        self._p_dir.start()
        self._w = _NoWriteStateWriter()
        self._p_writer = patch.object(state_writer, "writer", self._w)
        self._p_writer.start()
        self._w.on_project_start("intent", "/out")

    def tearDown(self):
        self._p_writer.stop()
        self._p_dir.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_observe_logs_risk_classified_and_does_not_block(self):
        risk = permissions.observe("deploy_hook", detail="vercel --prod --yes")
        self.assertEqual(risk, "critical")  # returns the assessment; control flow is unchanged
        records = _read_jsonl(self._w._session.path)
        rc = [r for r in records if r["event"] == "risk_classified"]
        self.assertEqual(len(rc), 1)
        self.assertEqual(rc[0]["kind"], "deploy_hook")
        self.assertEqual(rc[0]["risk"], "critical")
        self.assertIn("vercel", rc[0]["detail"])

    def test_observe_never_raises_without_active_session(self):
        # A fresh writer with no started run → no session; observe must still classify + not raise.
        with patch.object(state_writer, "writer", _NoWriteStateWriter()):
            self.assertEqual(permissions.observe("install", detail="npm install"), "high")


if __name__ == "__main__":
    unittest.main(verbosity=2)
