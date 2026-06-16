from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harness"))

import telegram_bot as bot  # noqa: E402


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class PidAliveTests(unittest.TestCase):
    def test_current_process_is_alive(self) -> None:
        self.assertTrue(bot._pid_alive(os.getpid()))

    def test_unused_pid_is_not_alive(self) -> None:
        # A very high PID is almost certainly not in use.
        self.assertFalse(bot._pid_alive(2_000_000_000))

    def test_nonpositive_pid_is_not_alive(self) -> None:
        self.assertFalse(bot._pid_alive(0))
        self.assertFalse(bot._pid_alive(-1))


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.state_path = d / "mission_control.json"
        self.pidfile = d / ".pipeline.pid"
        self._old_mc = bot._MISSION_CONTROL
        self._old_pid = bot._PIPELINE_PIDFILE
        bot._MISSION_CONTROL = self.state_path
        bot._PIPELINE_PIDFILE = self.pidfile

    def tearDown(self) -> None:
        bot._MISSION_CONTROL = self._old_mc
        bot._PIPELINE_PIDFILE = self._old_pid
        self.tmp.cleanup()

    def write_state(self, pipeline_state: str) -> None:
        self.state_path.write_text(
            json.dumps({"pipeline_state": pipeline_state, "sequence": 1, "events": []}),
            encoding="utf-8",
        )

    def test_orphaned_run_with_dead_pid_is_failed(self) -> None:
        self.write_state("EXECUTING")
        self.pidfile.write_text("2000000000", encoding="utf-8")  # dead pid
        bot._reconcile_orphaned_run()
        state = read_json(self.state_path)
        self.assertEqual(state["pipeline_state"], "FAILED")
        self.assertEqual(state["terminal"]["state"], "FAILED")
        self.assertIn("orphaned", state["terminal"]["message"].lower())
        self.assertIsNone(state["active_agent"])
        self.assertFalse(self.pidfile.exists())

    def test_orphaned_run_with_no_pidfile_is_failed(self) -> None:
        self.write_state("EXECUTING")
        bot._reconcile_orphaned_run()
        self.assertEqual(read_json(self.state_path)["pipeline_state"], "FAILED")

    def test_live_pid_is_left_untouched(self) -> None:
        self.write_state("EXECUTING")
        self.pidfile.write_text(str(os.getpid()), encoding="utf-8")
        bot._reconcile_orphaned_run()
        self.assertEqual(read_json(self.state_path)["pipeline_state"], "EXECUTING")

    def test_already_terminal_is_left_untouched(self) -> None:
        self.write_state("DONE")
        self.pidfile.write_text("2000000000", encoding="utf-8")
        bot._reconcile_orphaned_run()
        self.assertEqual(read_json(self.state_path)["pipeline_state"], "DONE")

    def test_idle_is_left_untouched(self) -> None:
        self.write_state("IDLE")
        bot._reconcile_orphaned_run()
        self.assertEqual(read_json(self.state_path)["pipeline_state"], "IDLE")

    def test_missing_file_is_noop(self) -> None:
        bot._reconcile_orphaned_run()  # no state file at all
        self.assertFalse(self.state_path.exists())

    def test_exited_child_fails_nonterminal_state(self) -> None:
        self.write_state("EXECUTING")
        bot._reconcile_exited_child(1)
        self.assertEqual(read_json(self.state_path)["pipeline_state"], "FAILED")

    def test_exited_child_leaves_terminal_state(self) -> None:
        self.write_state("DONE")
        bot._reconcile_exited_child(0)
        self.assertEqual(read_json(self.state_path)["pipeline_state"], "DONE")

    def test_canceled_wrapper_still_works(self) -> None:
        self.write_state("EXECUTING")
        bot._write_canceled_state()
        state = read_json(self.state_path)
        self.assertEqual(state["pipeline_state"], "CANCELED")
        self.assertEqual(state["terminal"]["state"], "CANCELED")


if __name__ == "__main__":
    unittest.main()
