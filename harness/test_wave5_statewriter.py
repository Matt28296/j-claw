r"""
Regression tests for _write_json_atomic retry-on-PermissionError logic.
Run with:
  cd harness
  $env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"
  .\.venv\Scripts\python.exe test_wave5_statewriter.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Allow import without the full harness environment (session_log may be absent
# when run standalone; stub it out so we can import only the function under test)
# ---------------------------------------------------------------------------
try:
    from state_writer import _write_json_atomic
except ImportError as _err:
    # If session_log is missing, stub it then retry
    import types
    _sl = types.ModuleType("session_log")
    _sl.SessionLog = object  # type: ignore[attr-defined]
    _sl.new_mission_id = lambda: "stub"  # type: ignore[attr-defined]
    sys.modules.setdefault("session_log", _sl)
    from state_writer import _write_json_atomic


class TestWriteJsonAtomicRetry(unittest.TestCase):
    """Tests for the PermissionError retry loop inside _write_json_atomic."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.out_path = Path(self._tmpdir.name) / "state.json"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # (a) os.replace raises PermissionError on first N-1 attempts, then
    #     succeeds on the last attempt — the write must still land.
    # ------------------------------------------------------------------
    def test_retry_succeeds_after_initial_failures(self) -> None:
        """First 3 of 4 attempts raise PermissionError; 4th succeeds."""
        real_replace = os.replace
        call_count = {"n": 0}

        def _flaky_replace(src: str, dst: str) -> None:
            call_count["n"] += 1
            if call_count["n"] < 4:
                raise PermissionError("[WinError 5] Access is denied")
            real_replace(src, dst)

        data = {"status": "ok", "value": 42}
        with patch("os.replace", side_effect=_flaky_replace), \
             patch("time.sleep"):  # skip real sleeps in tests
            _write_json_atomic(self.out_path, data)

        self.assertTrue(self.out_path.exists(), "Output file must exist after successful retry")
        written = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(written["status"], "ok")
        self.assertEqual(written["value"], 42)
        self.assertGreaterEqual(call_count["n"], 4, "Must have retried at least 4 times")

    # ------------------------------------------------------------------
    # (b) os.replace raises on every attempt — function must NOT raise
    #     and the temp file must be cleaned up.
    # ------------------------------------------------------------------
    def test_all_attempts_fail_no_exception_raised(self) -> None:
        """Every os.replace attempt raises PermissionError; function is silent."""
        tmp_files_seen: list[str] = []

        def _always_fail(src: str, dst: str) -> None:
            tmp_files_seen.append(src)
            raise PermissionError("[WinError 5] Access is denied")

        data = {"level": "critical", "seq": 99}
        with patch("os.replace", side_effect=_always_fail), \
             patch("time.sleep"):
            # Must not raise
            try:
                _write_json_atomic(self.out_path, data)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"_write_json_atomic raised unexpectedly: {exc}")

        # Output file should not be created (all replaces failed)
        self.assertFalse(
            self.out_path.exists(),
            "Output file should not exist when all replace attempts fail"
        )

        # All temp files seen must have been cleaned up by the finally block
        for tmp in tmp_files_seen:
            self.assertFalse(
                Path(tmp).exists(),
                f"Temp file {tmp} should have been cleaned up"
            )

    # ------------------------------------------------------------------
    # (c) Normal happy-path: write round-trips the JSON correctly.
    # ------------------------------------------------------------------
    def test_normal_write_roundtrips_json(self) -> None:
        """Successful write produces valid, readable JSON."""
        data = {
            "pipeline_state": "RUNNING",
            "project": {"name": "MOBA_TEST"},
            "tasks": [{"id": 1, "name": "wave5"}],
            "sequence": 7,
        }
        _write_json_atomic(self.out_path, data)

        self.assertTrue(self.out_path.exists(), "Output file must exist after normal write")
        written = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(written["pipeline_state"], "RUNNING")
        self.assertEqual(written["project"]["name"], "MOBA_TEST")
        self.assertEqual(len(written["tasks"]), 1)
        self.assertEqual(written["tasks"][0]["id"], 1)
        self.assertEqual(written["sequence"], 7)

    # ------------------------------------------------------------------
    # (d) Temp file is cleaned up after a successful write.
    # ------------------------------------------------------------------
    def test_temp_file_cleaned_up_on_success(self) -> None:
        """The per-pid/thread temp file must not linger after a successful write."""
        data = {"cleanup": True}
        _write_json_atomic(self.out_path, data)

        # Enumerate any .tmp files that might have been left behind
        leftover = list(Path(self.out_path.parent).glob("*.tmp"))
        self.assertEqual(
            leftover,
            [],
            f"Temp files should be cleaned up, found: {leftover}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
