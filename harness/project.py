from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

_TASK_FIELDS = frozenset({
    "id", "type", "objective", "files", "dependencies",
    "priority", "acceptance_criteria", "verification",
})


@dataclass
class Task:
    id: str
    type: str
    objective: str
    files: list
    dependencies: list
    priority: str
    acceptance_criteria: list
    verification: str
    # runtime state
    status: str = "pending"        # pending | running | done | failed | deprecated
    output_files: dict = field(default_factory=dict)  # relative_path -> content str
    binary_outputs: dict = field(default_factory=dict)  # relative_path -> Path (binary files written by workers)
    error_log: str = ""
    retry_count: int = 0


def _task_from_dict(d: dict) -> Task:
    return Task(**{k: v for k, v in d.items() if k in _TASK_FIELDS})


class ProjectInstance:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.spec: dict | None = None
        self.tasks: dict[str, Task] = {}
        self._id_watermark: int = 0

    # ── loading ──────────────────────────────────────────────────────────────

    def load_tasks(self, task_dicts: list[dict]) -> None:
        for d in task_dicts:
            t = _task_from_dict(d)
            self.tasks[t.id] = t
            self._bump_watermark(t.id)

    # ── FORMAT 3 application ─────────────────────────────────────────────────

    def apply_format3(self, refinement: dict) -> None:
        target_id = refinement["refinement_target_task_id"]
        action = refinement["action"]
        updated = refinement["updated_tasks"]

        if action == "deprecate":
            self.tasks[target_id].status = "deprecated"

        elif action == "modify":
            u = updated[0]
            t = self.tasks[target_id]
            t.objective = u["objective"]
            t.files = u["files"]
            t.dependencies = u["dependencies"]
            t.priority = u["priority"]
            t.acceptance_criteria = u["acceptance_criteria"]
            t.verification = u["verification"]
            t.status = "pending"
            t.error_log = ""

        elif action == "split":
            # first entry keeps the original id
            u0 = updated[0]
            t = self.tasks[target_id]
            t.objective = u0["objective"]
            t.files = u0["files"]
            t.dependencies = u0["dependencies"]
            t.priority = u0["priority"]
            t.acceptance_criteria = u0["acceptance_criteria"]
            t.verification = u0["verification"]
            t.status = "pending"
            t.error_log = ""
            # remaining entries are new tasks
            for u in updated[1:]:
                new_t = _task_from_dict(u)
                self.tasks[new_t.id] = new_t
                self._bump_watermark(new_t.id)

    # ── FORMAT 4 follow-ups ──────────────────────────────────────────────────

    def apply_format4_followups(self, followup_dicts: list[dict]) -> None:
        for d in followup_dicts:
            t = _task_from_dict(d)
            self.tasks[t.id] = t
            self._bump_watermark(t.id)

    # ── file I/O ─────────────────────────────────────────────────────────────

    def write_task_files(self, task: Task) -> None:
        for rel_path, content in task.output_files.items():
            full = self.output_dir / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")

    # ── queries ──────────────────────────────────────────────────────────────

    def get_dependency_files(self, task: Task) -> dict[str, dict[str, str]]:
        """Return {task_id: {rel_path: content}} for each completed dependency."""
        return {
            dep_id: self.tasks[dep_id].output_files
            for dep_id in task.dependencies
            if dep_id in self.tasks and self.tasks[dep_id].output_files
        }

    def all_tasks_done(self) -> bool:
        return all(t.status in ("done", "deprecated") for t in self.tasks.values())

    def active_dag_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status != "deprecated")

    def failed_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status == "failed"]

    def tasks_slim_list(self) -> list[dict]:
        """Lean version for REVIEW_FAILED orchestrator payloads.

        Omits fields the orchestrator doesn't need when issuing fix tasks
        (acceptance_criteria, priority, verification, dependencies). Keeps enough
        for ID-sequence tracking and file→task_id dependency lookup.
        - deprecated tasks are omitted entirely (no files were written)
        - failed tasks include type+objective so the orchestrator has error context
        - done/running tasks are reduced to {id, files, status}
        """
        result = []
        for t in self.tasks.values():
            if t.status == "deprecated":
                continue
            if t.status == "failed":
                result.append({
                    "id": t.id, "type": t.type, "objective": t.objective,
                    "files": t.files, "status": t.status,
                })
            else:
                result.append({"id": t.id, "files": t.files, "status": t.status})
        return result

    def tasks_as_list(self) -> list[dict]:
        return [
            {
                "id": t.id,
                "type": t.type,
                "objective": t.objective,
                "files": t.files,
                "dependencies": t.dependencies,
                "priority": t.priority,
                "acceptance_criteria": t.acceptance_criteria,
                "verification": t.verification,
                "status": t.status,
            }
            for t in self.tasks.values()
        ]

    # ── internals ────────────────────────────────────────────────────────────

    def _bump_watermark(self, task_id: str) -> None:
        try:
            seq = int(task_id.split("-")[1])
            self._id_watermark = max(self._id_watermark, seq)
        except (IndexError, ValueError):
            pass

    def next_task_id(self) -> str:
        self._id_watermark += 1
        return f"task-{self._id_watermark:03d}"
