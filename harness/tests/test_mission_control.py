"""Tests for per-task per-LLM token persistence in StateWriter.

Focused on the tokens_by_model accumulation and serialization path introduced
to track which model spent how many tokens on each task.  No live API calls —
all tests are pure in-memory, no disk writes.
"""
from __future__ import annotations
import json
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal state_writer import
# ---------------------------------------------------------------------------
# state_writer imports pathlib but writes to disk only inside _write_json_atomic.
# We monkey-patch _write to avoid touching the filesystem.
import sys
import os

# Add harness/ to sys.path so we can import state_writer directly.
_HARNESS = os.path.join(os.path.dirname(__file__), "..")
if _HARNESS not in sys.path:
    sys.path.insert(0, _HARNESS)

import state_writer as _sw_mod
from state_writer import StateWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _noop_write(self):
    """Replace StateWriter._write with a no-op to avoid filesystem access."""
    pass


class NoWriteStateWriter(StateWriter):
    """StateWriter subclass with _write suppressed for unit testing."""

    def _write(self):
        # still update sequence + elapsed so state is consistent
        import time
        now = time.time()
        if self._start_time:
            self._state["elapsed_s"] = round(now - self._start_time)
        self._state["updated_at_epoch"] = now
        self._state["sequence"] = int(self._state.get("sequence") or 0) + 1
        # do NOT call _write_json_atomic


def _make_tasks(ids=("t1", "t2")):
    """Return a list of minimal task dicts for on_dag_loaded."""
    return [
        {"id": tid, "type": "code", "objective": f"Implement {tid}", "files": ["a.js"]}
        for tid in ids
    ]


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestTokensByModelInitialization(unittest.TestCase):
    """on_dag_loaded and on_tasks_added should initialise tokens_by_model={}."""

    def setUp(self):
        self.sw = NoWriteStateWriter()
        self.sw._start_time = 0.0  # satisfy elapsed_s calc

    def test_dag_loaded_adds_empty_tokens_by_model(self):
        self.sw.on_dag_loaded(_make_tasks(["t1", "t2"]))
        for t in self.sw._state["tasks"]:
            self.assertIn("tokens_by_model", t)
            self.assertEqual(t["tokens_by_model"], {})

    def test_tasks_added_adds_empty_tokens_by_model(self):
        self.sw.on_dag_loaded(_make_tasks(["t1"]))
        self.sw.on_tasks_added(_make_tasks(["t2", "t3"]))
        for t in self.sw._state["tasks"]:
            self.assertIn("tokens_by_model", t)
            self.assertEqual(t["tokens_by_model"], {})


class TestMergeTaskTokens(unittest.TestCase):
    """_merge_task_tokens should accumulate additively."""

    def setUp(self):
        self.sw = NoWriteStateWriter()
        self.sw._start_time = 0.0
        self.sw.on_dag_loaded(_make_tasks(["task1"]))

    def test_first_merge_sets_values(self):
        self.sw._merge_task_tokens("task1", {"qwen3:8b": {"input": 100, "output": 50}})
        tbm = self._get_task("task1")["tokens_by_model"]
        self.assertEqual(tbm["qwen3:8b"]["input"], 100)
        self.assertEqual(tbm["qwen3:8b"]["output"], 50)

    def test_second_merge_accumulates(self):
        self.sw._merge_task_tokens("task1", {"qwen3:8b": {"input": 100, "output": 50}})
        self.sw._merge_task_tokens("task1", {"qwen3:8b": {"input": 200, "output": 80}})
        tbm = self._get_task("task1")["tokens_by_model"]
        self.assertEqual(tbm["qwen3:8b"]["input"], 300)
        self.assertEqual(tbm["qwen3:8b"]["output"], 130)

    def test_multiple_models_accumulate_independently(self):
        self.sw._merge_task_tokens("task1", {
            "grok/grok-build": {"input": 500, "output": 200},
            "qwen3:8b":        {"input": 100, "output": 40},
        })
        self.sw._merge_task_tokens("task1", {
            "anthropic/claude-sonnet-4-6": {"input": 1000, "output": 300},
            "qwen3:8b":                    {"input": 50,   "output": 20},
        })
        tbm = self._get_task("task1")["tokens_by_model"]
        self.assertEqual(tbm["grok/grok-build"]["input"], 500)
        self.assertEqual(tbm["grok/grok-build"]["output"], 200)
        self.assertEqual(tbm["qwen3:8b"]["input"], 150)
        self.assertEqual(tbm["qwen3:8b"]["output"], 60)
        self.assertEqual(tbm["anthropic/claude-sonnet-4-6"]["input"], 1000)

    def test_merge_noop_for_unknown_task(self):
        # Should not raise; no task with id "ghost"
        self.sw._merge_task_tokens("ghost", {"model": {"input": 1, "output": 1}})
        self.assertEqual(self.sw._state["tasks"][0]["tokens_by_model"], {})

    def test_merge_tolerates_missing_sub_keys(self):
        # Partial dict (only "input" present)
        self.sw._merge_task_tokens("task1", {"m": {"input": 10}})
        tbm = self._get_task("task1")["tokens_by_model"]
        self.assertEqual(tbm["m"]["input"], 10)
        self.assertEqual(tbm["m"]["output"], 0)

    def _get_task(self, task_id):
        for t in self.sw._state["tasks"]:
            if t["id"] == task_id:
                return t
        raise KeyError(task_id)


class TestOnTaskTokens(unittest.TestCase):
    """on_task_tokens should merge and call _write."""

    def setUp(self):
        self.sw = NoWriteStateWriter()
        self.sw._start_time = 0.0
        self.sw.on_dag_loaded(_make_tasks(["t1"]))
        self._write_calls = 0
        # patch _write to count invocations
        orig_write = self.sw._write
        def counting_write():
            self._write_calls += 1
            orig_write()
        self.sw._write = counting_write

    def test_tokens_are_merged(self):
        self.sw.on_task_tokens("t1", {"grok/grok-build": {"input": 300, "output": 100}})
        t = self.sw._state["tasks"][0]
        self.assertEqual(t["tokens_by_model"]["grok/grok-build"]["input"], 300)

    def test_write_called_on_nonempty_tokens(self):
        self.sw.on_task_tokens("t1", {"m": {"input": 1, "output": 1}})
        self.assertGreaterEqual(self._write_calls, 1)

    def test_empty_tokens_skips_write(self):
        self.sw.on_task_tokens("t1", {})
        self.assertEqual(self._write_calls, 0)


class TestOnTaskDoneWithTokens(unittest.TestCase):
    """on_task_done optional tokens_by_model kwarg should persist tokens."""

    def setUp(self):
        self.sw = NoWriteStateWriter()
        self.sw._start_time = 0.0
        # on_project_start resets tasks, so call it first, then load DAG
        self.sw.on_project_start("test intent", "/tmp/out")
        self.sw.on_dag_loaded(_make_tasks(["t1"]))

    def test_tokens_persisted_via_kwarg(self):
        self.sw.on_task_done("t1", "qwen3:8b",
                             tokens_by_model={"qwen3:8b": {"input": 512, "output": 128}})
        t = self.sw._state["tasks"][0]
        self.assertEqual(t["tokens_by_model"]["qwen3:8b"]["input"], 512)
        self.assertEqual(t["tokens_by_model"]["qwen3:8b"]["output"], 128)

    def test_no_tokens_kwarg_leaves_dict_empty(self):
        self.sw.on_task_done("t1", "qwen3:8b")
        t = self.sw._state["tasks"][0]
        self.assertIn("tokens_by_model", t)
        self.assertEqual(t["tokens_by_model"], {})


class TestTokensPresentInSerializedState(unittest.TestCase):
    """tokens_by_model must survive JSON round-trip unchanged."""

    def test_round_trip(self):
        sw = NoWriteStateWriter()
        sw._start_time = 0.0
        sw.on_dag_loaded(_make_tasks(["t1"]))
        sw._merge_task_tokens("t1", {
            "grok/grok-build":            {"input": 999, "output": 333},
            "codex/gpt-5.5":              {"input": 0,   "output": 0},
            "anthropic/claude-sonnet-4-6": {"input": 2048, "output": 512},
        })
        serialized = json.dumps(sw._state, indent=2)
        loaded = json.loads(serialized)
        tbm = loaded["tasks"][0]["tokens_by_model"]
        self.assertEqual(tbm["grok/grok-build"]["input"], 999)
        self.assertEqual(tbm["codex/gpt-5.5"]["output"], 0)
        self.assertEqual(tbm["anthropic/claude-sonnet-4-6"]["input"], 2048)


if __name__ == "__main__":
    unittest.main()
