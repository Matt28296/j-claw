"""Append-only, replayable per-run session transcript (roadmap item #5).

A chronological JSONL record of every pipeline lifecycle event — orchestrator calls, worker
attempts/outcomes, verification results, heals, deploys — written ALONGSIDE mission_control.json
(which is a current-state snapshot, not replayable). One file per run under ``<repo>/sessions/``,
never overwritten, so a run can be replayed for debugging, audit, resume, and tuning future routing.

Design notes:
- **Best-effort:** a logging failure must NEVER break the pipeline — every write is wrapped and
  swallowed, mirroring the existing telemetry hooks (e.g. state_writer.on_task_tokens).
- **Correlation:** every record carries ``mission_id`` + a monotonic ``seq``; callers add
  ``task_id`` / ``provider`` / ``rung`` etc. as event fields.
- Driven from ``StateWriter`` (state_writer.py), the hub every lifecycle event already flows through,
  so emission piggybacks the existing ``on_*`` hooks rather than scattering across the pipeline.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

# Lives next to mission_control.json (repo root), NOT inside the per-run output_dir — which
# main.py wipes with shutil.rmtree at the start of each run. Persists across runs for audit/history.
SESSIONS_DIR = Path(__file__).parent.parent / "sessions"


def new_mission_id() -> str:
    """A sortable, unique per-run id: UTC-ish timestamp + short random suffix."""
    return time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


class SessionLog:
    """Append-only JSONL writer for one mission/run. Thread-safe and non-raising."""

    def __init__(self, mission_id: str, *, intent: str = "", output_dir: str = "") -> None:
        self.mission_id = mission_id
        self._seq = 0
        self._lock = threading.Lock()
        self._path = SESSIONS_DIR / f"{mission_id}.jsonl"
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.emit("mission_started", intent=intent[:500], output_dir=output_dir)

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, event: str, **fields) -> None:
        """Append one event record. Never raises — telemetry must not abort the pipeline."""
        try:
            with self._lock:
                self._seq += 1
                record = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "seq": self._seq,
                    "mission_id": self.mission_id,
                    "event": event,
                }
                record.update(fields)
                line = json.dumps(record, ensure_ascii=False, default=str)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:  # noqa: BLE001 — best-effort logging, never propagate
            pass
