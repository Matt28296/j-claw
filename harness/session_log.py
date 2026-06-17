"""Append-only, replayable per-run session transcript (roadmap item #5).

A chronological JSONL record of pipeline lifecycle events — orchestrator calls, worker TASK outcomes,
verification + final-review + dynamic-check results, DAG mutations, heals, deploys — written ALONGSIDE
mission_control.json (which is a current-state snapshot, not replayable). One file per run under
``<repo>/sessions/``, never overwritten, so a run can be replayed for debugging, audit, resume, and
tuning future routing. Recursive FORMAT-5 sub-projects each get their OWN file, correlated to the
parent via ``parent_mission_id`` (StateWriter keeps a session stack so a sub-run can't clobber its
parent's transcript).

Design notes / known limits:
- **Best-effort:** a logging failure must NEVER break the pipeline — every write is wrapped and
  swallowed, mirroring the existing telemetry hooks (e.g. state_writer.on_task_tokens).
- **Correlation:** every record carries ``mission_id`` + a monotonic ``seq`` (committed only after a
  successful write, so successful records are gap-free); callers add ``task_id`` / ``provider`` /
  ``rung`` etc. as event fields.
- **Granularity:** v1 logs TASK-level worker outcomes (the winning model via ``task_done`` /
  ``task_failed``); per-attempt ladder escalation inside ``worker.execute_task`` is a planned
  follow-up (it would emit from worker.py, not via the state_writer hooks).
- **Durability:** append-only, no ``fsync`` — a hard kill can leave a truncated final line, so a
  replay reader should tolerate a malformed last record. Field values are bounded/truncated at the
  call sites; retention/rotation of ``sessions/`` is a future concern as volume grows.
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

    def __init__(self, mission_id: str, *, intent: str = "", output_dir: str = "",
                 parent_mission_id: str | None = None) -> None:
        self.mission_id = mission_id
        self.parent_mission_id = parent_mission_id
        self._seq = 0
        self._lock = threading.Lock()
        self._path = SESSIONS_DIR / f"{mission_id}.jsonl"
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        started = {"intent": intent[:500], "output_dir": output_dir}
        if parent_mission_id:
            started["parent_mission_id"] = parent_mission_id  # FORMAT-5 child → parent correlation
        self.emit("mission_started", **started)

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, event: str, **fields) -> None:
        """Append one event record. Never raises — telemetry must not abort the pipeline."""
        try:
            with self._lock:
                next_seq = self._seq + 1
                record = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "seq": next_seq,
                    "mission_id": self.mission_id,
                    "event": event,
                }
                record.update(fields)
                line = json.dumps(record, ensure_ascii=False, default=str)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                self._seq = next_seq  # commit the seq only after a successful write (no gaps on success)
        except Exception:  # noqa: BLE001 — best-effort logging, never propagate
            pass
