from __future__ import annotations
import json
import time
import threading
from pathlib import Path

_STATE_FILE = Path(__file__).parent.parent / "mission_control.json"
_lock = threading.Lock()
_MAX_ERROR_LOG_CHARS = 3000
_MAX_AGENT_NODES = 30


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
            "agent_nodes": {},
            "events": [],
            "output_files": [],
            "started_at": None,
            "elapsed_s": 0,
            "updated_at_epoch": None,
            "sequence": 0,
        }
        self._start_time: float | None = None
        self._last_orch_model: str = "orchestrator"
        self._last_worker_model: str = "worker"
        self._test_attempt_counts: dict[str, int] = {}  # task_id → attempt number

    # ── Public hooks ──────────────────────────────────────────────────────────

    def on_project_start(self, intent: str, output_dir: str) -> None:
        self._start_time = _now()
        # Compute a relative URL path so the dashboard can fetch output files
        try:
            output_url = Path(output_dir).resolve().relative_to(_STATE_FILE.parent.resolve()).as_posix()
        except ValueError:
            output_url = None
        self._state["pipeline_state"] = "INIT"
        self._state["project"] = {"intent": intent, "output_dir": output_dir, "output_url": output_url}
        self._state["tasks"] = []
        self._state["output_files"] = []
        self._state["events"] = []
        self._state["agent_nodes"] = {}
        self._state["active_agent"] = None
        self._state["work_log"] = []
        self._state["test_results"] = []
        self._state["started_at"] = _ts()
        self._test_attempt_counts = {}
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
                "error_log": None,
            }
            for t in tasks
        ]
        self._event(f"DAG loaded — {len(tasks)} task(s) queued")
        self._work_log("orchestrator", self._orch_model(), "DAG",
                       f"Planned {len(tasks)} task(s)")
        self._write()

    def on_task_start(self, task_id: str) -> None:
        self._update_task(task_id, status="running", error_log=None)
        self._event(f"▶ {task_id} started")
        self._write()

    def on_task_done(self, task_id: str, model_used: str) -> None:
        self._update_task(task_id, status="done", model_used=model_used, error_log=None)
        self._event(f"✓ {task_id} done  [{model_used}]")
        obj = self._task_objective(task_id)
        self._last_worker_model = model_used
        self._state["project"]["worker_model"] = model_used
        self._work_log("worker", model_used, task_id,
                       obj, status="done")
        self._write()

    def on_task_failed(self, task_id: str, error: str, retry_count: int) -> None:
        error_log = (error or "")[:_MAX_ERROR_LOG_CHARS]
        self._update_task(task_id, status="failed", retry_count=retry_count, error_log=error_log)
        self._event(f"✗ {task_id} failed (attempt {retry_count}): {error_log[:120]}")
        model = self._active_model()
        self._work_log("worker", model, task_id,
                       error_log[:100], status="failed", attempt=retry_count)
        self._write()

    def on_agent_call(self, agent: str, model: str, state: str,
                      task_id: str | None = None, task_type: str | None = None,
                      provider: str | None = None, rung: int | None = None,
                      summary: str | None = None) -> None:
        started_at = _ts()
        started_epoch = _now()
        node_key = self._agent_node_key(agent, task_id)
        self._state["active_agent"] = {
            "agent": agent,
            "model": model,
            "state": state,
            "started_at": started_at,
            "started_epoch": started_epoch,
            "node_key": node_key,
        }
        if task_id is not None:
            self._state["active_agent"]["task_id"] = task_id
        if task_type is not None:
            self._state["active_agent"]["task_type"] = task_type
        if provider is not None:
            self._state["active_agent"]["provider"] = provider
        if rung is not None:
            self._state["active_agent"]["rung"] = rung
        if summary:
            self._state["active_agent"]["summary"] = summary[:240]
        if agent == "orchestrator":
            self._state["pipeline_state"] = state
        event_target = f" for {task_id}" if task_id else f" for {state}"
        self._event(f"[{agent}] calling {model}{event_target}")
        if agent == "orchestrator":
            self._last_orch_model = model
            self._state["project"]["orch_model"] = model
        else:
            self._last_worker_model = model
            self._state["project"]["worker_model"] = model
        self._upsert_agent_node(
            node_key=node_key,
            agent=agent,
            model=model,
            state=state,
            status="running",
            task_id=task_id,
            task_type=task_type,
            provider=provider,
            rung=rung,
            summary=summary,
            started_at=started_at,
            started_epoch=started_epoch,
        )
        self._write()

    def on_agent_done(self, agent: str | None = None, task_id: str | None = None,
                      status: str = "done", summary: str | None = None,
                      model: str | None = None) -> None:
        aa = self._state.get("active_agent")
        if agent:
            node_key = self._agent_node_key(agent, task_id)
        elif task_id:
            node_key = self._find_agent_node_by_task(task_id)
        else:
            node_key = aa.get("node_key") if aa else None
        if node_key:
            self._mark_agent_node_done(node_key, status=status, summary=summary, model=model)

        if aa:
            should_clear = False
            if agent is None and task_id is None:
                should_clear = True
            elif agent is None and task_id is not None:
                should_clear = aa.get("task_id") == task_id
            else:
                should_clear = (
                    aa.get("agent") == agent
                    and (task_id is None or aa.get("task_id") == task_id)
                )
            if should_clear:
                self._state["active_agent"] = None
        self._write()

    def on_file_written(self, path: str, task_id: str) -> None:
        self._state["output_files"].append({"path": path, "task_id": task_id, "written_at": _ts()})
        self._event(f"  📄 {path}")
        self._write()

    def on_tasks_added(self, new_tasks: list[dict]) -> None:
        for t in new_tasks:
            self._state["tasks"].append({
                "id": t["id"],
                "type": t["type"],
                "objective": t["objective"][:120],
                "status": "pending",
                "retry_count": 0,
                "files": t.get("files", []),
                "model_used": None,
                "error_log": None,
            })
        self._event(f"Heal tasks added — {len(new_tasks)} fix task(s) queued")
        self._write()

    def on_verification_result(self, task_id: str, method: str, ecosystem: str,
                               passed: bool, log: str) -> None:
        if method == "none":
            return  # skip trivial auto-passes — nothing useful to show
        icon = "✓" if passed else "✗"
        self._test_attempt_counts[task_id] = self._test_attempt_counts.get(task_id, 0) + 1
        self._state.setdefault("test_results", []).append({
            "ts": _ts(),
            "task_id": task_id,
            "method": method,
            "ecosystem": ecosystem,
            "passed": passed,
            "log": log[:1200],
            "attempt": self._test_attempt_counts[task_id],
        })
        self._event(f"{icon} [{method}/{ecosystem}] {task_id}: {'passed' if passed else 'FAILED'}")
        self._write()

    def on_review_failed(self, issue_count: int, heal_cycle: int) -> None:
        """Emit a dedicated REVIEW_FAILED event so the dashboard heal-badge counter works."""
        self._event(f"REVIEW_FAILED — heal cycle {heal_cycle}, {issue_count} issue(s) to fix")
        self._write()

    def on_project_done(self, result: str, summary: str) -> None:
        self._state["pipeline_state"] = "DONE" if result == "pass" else "NEEDS_FOLLOWUP"
        self._state["active_agent"] = None
        self._event(f"Project complete — {result}: {summary[:120]}")
        self._work_log("orchestrator", self._orch_model(), "REVIEW",
                       summary[:120], status=result)
        self._write()

    def on_cost(self, summary: dict) -> None:
        # Normalize to the exact shape renderCostPanel expects so the
        # breakdown table and token display are never empty due to key
        # mismatches or a partially-populated dict.
        raw_tokens = summary.get("tokens") or {}
        self._state["cost"] = {
            "total_usd": float(
                summary.get("total_usd")
                or summary.get("usd")
                or summary.get("total")
                or 0.0
            ),
            "by_model": dict(summary.get("by_model") or {}),
            "tokens": {
                "input":      int(raw_tokens.get("input", 0) or 0),
                "cache_read": int(raw_tokens.get("cache_read", 0) or 0),
                "output":     int(raw_tokens.get("output", 0) or 0),
            },
            "paid_calls": int(summary.get("paid_calls", 0) or 0),
        }
        self._write()

    def on_openclaw_stamp(self, verdict: str) -> None:
        """Record the OpenClaw final-stamp verdict so the dashboard can display it.

        verdict is the full response text; the short stamp stored is either
        'PASS' (when 'OPENCLAW: APPROVED' is present) or 'ISSUES FOUND'.
        Both the top-level key and the nested project key are written so the
        frontend fallback chain (d.openclaw_stamp → d.project.openclaw_stamp)
        works regardless of which path the dashboard reads.
        """
        stamp = "PASS" if "OPENCLAW: APPROVED" in verdict else "ISSUES FOUND"
        self._state["openclaw_stamp"] = stamp
        self._state["project"]["openclaw_stamp"] = stamp
        self._event(f"OpenClaw stamp: {stamp}")
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
            self._last_orch_model = aa.get("model", self._last_orch_model)
        return self._last_orch_model

    def _active_model(self) -> str:
        aa = self._state.get("active_agent")
        return aa.get("model", "worker") if aa else "worker"

    def _agent_node_key(self, agent: str | None, task_id: str | None = None) -> str:
        return f"{agent}:{task_id}" if task_id else str(agent)

    def _find_agent_node_by_task(self, task_id: str | None) -> str | None:
        if task_id is None:
            return None
        nodes = self._state.setdefault("agent_nodes", {})
        for key, node in nodes.items():
            if node.get("task_id") == task_id and node.get("status") == "running":
                return key
        return None

    def _upsert_agent_node(self, node_key: str, agent: str, model: str, state: str,
                           status: str, task_id: str | None = None,
                           task_type: str | None = None, provider: str | None = None,
                           rung: int | None = None, summary: str | None = None,
                           started_at: str | None = None,
                           started_epoch: float | None = None) -> None:
        nodes = self._state.setdefault("agent_nodes", {})
        now = _now()
        existing = nodes.get(node_key, {})
        node = {
            **existing,
            "agent": agent,
            "model": model,
            "state": state,
            "status": status,
            "started_at": started_at or existing.get("started_at") or _ts(),
            "started_epoch": started_epoch or existing.get("started_epoch") or now,
            "updated_at_epoch": now,
        }
        if task_id is not None:
            node["task_id"] = task_id
        if task_type is not None:
            node["task_type"] = task_type
        if provider is not None:
            node["provider"] = provider
        if rung is not None:
            node["rung"] = rung
        if summary:
            node["summary"] = summary[:240]
        nodes[node_key] = node
        self._trim_agent_nodes()

    def _mark_agent_node_done(self, node_key: str, status: str = "done",
                              summary: str | None = None,
                              model: str | None = None) -> None:
        nodes = self._state.setdefault("agent_nodes", {})
        node = nodes.get(node_key)
        if not node:
            return
        now = _now()
        node["status"] = status
        node["state"] = status.upper()
        node["updated_at_epoch"] = now
        started = node.get("started_epoch")
        if started:
            node["elapsed_s"] = round(now - started, 1)
        if summary:
            node["summary"] = summary[:240]
        if model:
            node["model"] = model
        self._trim_agent_nodes()

    def _trim_agent_nodes(self) -> None:
        nodes = self._state.setdefault("agent_nodes", {})
        if len(nodes) <= _MAX_AGENT_NODES:
            return
        keep = sorted(
            nodes.items(),
            key=lambda item: item[1].get("updated_at_epoch", 0),
            reverse=True,
        )[:_MAX_AGENT_NODES]
        self._state["agent_nodes"] = dict(keep)

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
        with _lock:
            now = _now()
            if self._start_time:
                self._state["elapsed_s"] = round(now - self._start_time)
            self._state["updated_at_epoch"] = now
            self._state["sequence"] = int(self._state.get("sequence") or 0) + 1
            _STATE_FILE.write_text(
                json.dumps(self._state, indent=2), encoding="utf-8"
            )


# Global singleton used by the harness
writer = StateWriter()
