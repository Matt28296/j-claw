from __future__ import annotations
import json
import time
import threading
from pathlib import Path

_STATE_FILE = Path(__file__).parent.parent / "mission_control.json"
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _ts() -> str:
    return time.strftime("%H:%M:%S")


class StateWriter:
    def __init__(self) -> None:
        self._state: dict = {
            "pipeline_state": "IDLE",
            "project": {},
            "tasks": [],
            "active_agent": None,
            "events": [],
            "output_files": [],
            "started_at": None,
            "elapsed_s": 0,
        }
        self._start_time: float | None = None

    # ── Public hooks ──────────────────────────────────────────────────────────

    def on_project_start(self, intent: str, output_dir: str) -> None:
        self._start_time = _now()
        # Compute a relative URL path so the dashboard can fetch output files
        try:
            output_url = Path(output_dir).relative_to(_STATE_FILE.parent).as_posix()
        except ValueError:
            output_url = None
        self._state["pipeline_state"] = "INIT"
        self._state["project"] = {"intent": intent, "output_dir": output_dir, "output_url": output_url}
        self._state["tasks"] = []
        self._state["output_files"] = []
        self._state["events"] = []
        self._state["work_log"] = []
        self._state["test_results"] = []
        self._state["started_at"] = _ts()
        self._event(f"Project started: {intent[:80]}")
        self._write()

    def on_spec_accepted(self, spec: dict) -> None:
        self._state["pipeline_state"] = "SPEC_ACCEPTED"
        self._state["project"]["goal"] = spec.get("goal", "")
        self._state["project"]["complexity"] = spec.get("complexity", "")
        stack = spec.get("architecture", {}).get("stack", "")
        self._state["project"]["stack"] = stack
        self._event("Spec accepted — generating task DAG")
        self._work_log("orchestrator", self._orch_model(), "SPEC",
                       f"Generated spec: {spec.get('goal', '')[:80]}")
        self._write()

    def on_dag_loaded(self, tasks: list[dict]) -> None:
        self._state["pipeline_state"] = "EXECUTING"
        self._state["tasks"] = [
            {
                "id": t["id"],
                "type": t["type"],
                "objective": t["objective"][:120],
                "status": t.get("status", "pending"),
                "retry_count": 0,
                "files": t.get("files", []),
                "model_used": None,
            }
            for t in tasks
        ]
        self._event(f"DAG loaded — {len(tasks)} task(s) queued")
        self._work_log("orchestrator", self._orch_model(), "DAG",
                       f"Planned {len(tasks)} task(s)")
        self._write()

    def on_task_start(self, task_id: str) -> None:
        self._update_task(task_id, status="running")
        self._event(f"▶ {task_id} started")
        self._write()

    def on_task_done(self, task_id: str, model_used: str) -> None:
        self._update_task(task_id, status="done", model_used=model_used)
        self._event(f"✓ {task_id} done  [{model_used}]")
        obj = self._task_objective(task_id)
        self._work_log("worker", model_used, task_id,
                       obj, status="done")
        self._write()

    def on_task_failed(self, task_id: str, error: str, retry_count: int) -> None:
        self._update_task(task_id, status="failed", retry_count=retry_count)
        self._event(f"✗ {task_id} failed (attempt {retry_count}): {error[:120]}")
        model = self._active_model()
        self._work_log("worker", model, task_id,
                       error[:100], status="failed", attempt=retry_count)
        self._write()

    def on_agent_call(self, agent: str, model: str, state: str) -> None:
        self._state["active_agent"] = {
            "agent": agent,
            "model": model,
            "state": state,
            "started_at": _ts(),
            "started_epoch": _now(),
        }
        self._state["pipeline_state"] = state
        self._event(f"[{agent}] calling {model} for {state}")
        self._write()

    def on_agent_done(self) -> None:
        self._state["active_agent"] = None
        self._write()

    def on_file_written(self, path: str, task_id: str) -> None:
        self._state["output_files"].append({"path": path, "task_id": task_id, "written_at": _ts()})
        self._event(f"  📄 {path}")
        self._write()

    def on_verification_result(self, task_id: str, method: str, ecosystem: str,
                               passed: bool, log: str) -> None:
        if method == "none":
            return  # skip trivial auto-passes — nothing useful to show
        icon = "✓" if passed else "✗"
        self._state.setdefault("test_results", []).append({
            "ts": _ts(),
            "task_id": task_id,
            "method": method,
            "ecosystem": ecosystem,
            "passed": passed,
            "log": log[:1200],
        })
        self._event(f"{icon} [{method}/{ecosystem}] {task_id}: {'passed' if passed else 'FAILED'}")
        self._write()

    def on_project_done(self, result: str, summary: str) -> None:
        self._state["pipeline_state"] = "DONE" if result == "pass" else "NEEDS_FOLLOWUP"
        self._state["active_agent"] = None
        self._event(f"Project complete — {result}: {summary[:120]}")
        self._work_log("orchestrator", self._orch_model(), "REVIEW",
                       summary[:120], status=result)
        self._write()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _update_task(self, task_id: str, **kwargs) -> None:
        for t in self._state["tasks"]:
            if t["id"] == task_id:
                t.update(kwargs)
                return

    def _task_objective(self, task_id: str) -> str:
        for t in self._state["tasks"]:
            if t["id"] == task_id:
                return t.get("objective", "")[:80]
        return ""

    def _orch_model(self) -> str:
        aa = self._state.get("active_agent")
        if aa and aa.get("agent") == "orchestrator":
            return aa.get("model", "orchestrator")
        return "orchestrator"

    def _active_model(self) -> str:
        aa = self._state.get("active_agent")
        return aa.get("model", "worker") if aa else "worker"

    def _work_log(self, agent: str, model: str, action: str,
                  detail: str, status: str = "ok", attempt: int = 0) -> None:
        entry: dict = {
            "ts": _ts(),
            "agent": agent,
            "model": model,
            "action": action,
            "detail": detail,
            "status": status,
        }
        if attempt:
            entry["attempt"] = attempt
        wl = self._state.setdefault("work_log", [])
        wl.append(entry)

    def _event(self, message: str) -> None:
        self._state["events"].insert(0, {"ts": _ts(), "msg": message})
        self._state["events"] = self._state["events"][:100]  # keep last 100

    def _write(self) -> None:
        if self._start_time:
            self._state["elapsed_s"] = round(_now() - self._start_time)
        with _lock:
            _STATE_FILE.write_text(
                json.dumps(self._state, indent=2), encoding="utf-8"
            )


# Global singleton used by the harness
writer = StateWriter()
