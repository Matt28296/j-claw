from __future__ import annotations
from rich.console import Console

from config import MAX_RETRIES_PER_TASK, WORKER_MODEL
from project import ProjectInstance, Task
from worker import execute_task
from verification import run_verification
from validator import validate_dag, OrchestratorOutputError

console = Console()


class Scheduler:
    def __init__(self, instance: ProjectInstance, orchestrator) -> None:
        self.instance = instance
        self.orch = orchestrator

    def run(self) -> None:
        """Execute all tasks to completion (or terminal failure)."""
        while not self.instance.all_tasks_done():
            ready = self._ready_tasks()

            if not ready:
                failed = self.instance.failed_tasks()
                pending = [t for t in self.instance.tasks.values() if t.status == "pending"]
                if failed:
                    console.print(
                        f"\n[red]Scheduler stalled: {len(failed)} task(s) failed with no retries left.[/red]"
                    )
                    for t in failed:
                        console.print(f"  • {t.id}: {t.error_log[:200]}")
                elif pending:
                    console.print(
                        "[red]Scheduler deadlock: tasks are pending but none are ready "
                        "(unsatisfied dependencies?).[/red]"
                    )
                break

            for task in ready:
                self._run_task(task)

        if self.instance.all_tasks_done():
            self._project_review()

    # ── task execution ────────────────────────────────────────────────────────

    def _run_task(self, task: Task) -> None:
        console.print(
            f"\n[bold cyan]▶ {task.id}[/bold cyan] [{task.type}]  "
            f"{task.objective[:90]}{'…' if len(task.objective) > 90 else ''}"
        )
        task.status = "running"

        dep_files = self.instance.get_dependency_files(task)

        try:
            result = execute_task(task, self.instance.spec, dep_files)
            task.output_files = {f["path"]: f["content"] for f in result["files"]}
            self.instance.write_task_files(task)

            passed, log = run_verification(task, self.instance.output_dir)
            if passed:
                task.status = "done"
                console.print(f"  [green]✓ done[/green]  [dim](worker: {WORKER_MODEL})[/dim]")
            else:
                task.status = "failed"
                task.error_log = f"Verification ({task.verification}) failed:\n{log}"
                self._handle_error(task)

        except Exception as exc:  # noqa: BLE001
            task.status = "failed"
            task.error_log = str(exc)
            console.print(f"  [red]✗ error: {exc}[/red]")
            self._handle_error(task)

    # ── error handling ────────────────────────────────────────────────────────

    def _handle_error(self, task: Task) -> None:
        task.retry_count += 1
        if task.retry_count > MAX_RETRIES_PER_TASK:
            console.print(
                f"  [red]{task.id} exhausted {MAX_RETRIES_PER_TASK} retries — manual intervention required.[/red]"
            )
            return

        console.print(
            f"  [yellow]Requesting EXECUTION_ERROR refinement "
            f"(attempt {task.retry_count}/{MAX_RETRIES_PER_TASK})…[/yellow]"
        )

        refinement = self.orch.call({
            "system_state": "EXECUTION_ERROR",
            "failed_task": {
                "id": task.id,
                "type": task.type,
                "objective": task.objective,
                "files": task.files,
                "dependencies": task.dependencies,
                "acceptance_criteria": task.acceptance_criteria,
                "verification": task.verification,
            },
            "error_log": task.error_log[:3000],
            "active_dag": self.instance.tasks_as_list(),
        })

        action = refinement["action"]
        console.print(
            f"  [yellow]Refinement: {action} — {refinement['reason_for_refinement']}[/yellow]"
        )

        # Validate split additions against the active DAG
        if action == "split":
            existing = self.instance.tasks_as_list()
            new_tasks = refinement["updated_tasks"][1:]  # first keeps original id
            try:
                validate_dag(new_tasks, existing)
            except OrchestratorOutputError as exc:
                console.print(f"  [red]Split DAG invalid: {exc}  — marking task failed.[/red]")
                task.status = "failed"
                return

        self.instance.apply_format3(refinement)

    # ── project review ────────────────────────────────────────────────────────

    def _project_review(self) -> None:
        console.print("\n[bold]All tasks done — requesting PROJECT_REVIEW…[/bold]")

        completed_summary = [
            {"id": t.id, "status": t.status, "files_written": list(t.output_files.keys())}
            for t in self.instance.tasks.values()
            if t.status == "done"
        ]

        review = self.orch.call({
            "system_state": "PROJECT_REVIEW",
            "accepted_spec": self.instance.spec,
            "completed_tasks": completed_summary,
        })

        result = review["review_result"]
        color = "green" if result == "pass" else "yellow"
        console.print(f"\n  Review result: [bold {color}]{result}[/bold {color}]")
        console.print(f"  {review['summary']}")

        if result == "pass":
            return

        followups = review.get("followup_tasks", [])
        if not followups:
            return

        budget = self.instance.active_dag_count() + len(followups)
        if budget > 20:
            console.print(
                f"[red]Follow-up tasks would push Active DAG to {budget} (> 20 limit). Stopping.[/red]"
            )
            return

        try:
            validate_dag(followups, self.instance.tasks_as_list())
        except OrchestratorOutputError as exc:
            console.print(f"[red]Follow-up DAG invalid: {exc}  — skipping follow-ups.[/red]")
            return

        self.instance.apply_format4_followups(followups)
        console.print(f"  Added {len(followups)} follow-up task(s). Continuing execution…")
        # Loop back into run() naturally — caller will re-invoke if needed.
        # We just return; the while loop in run() will pick up the new tasks.

    # ── helpers ───────────────────────────────────────────────────────────────

    def _ready_tasks(self) -> list[Task]:
        """Tasks whose dependencies are all done/deprecated and which are still pending."""
        ready = []
        for task in self.instance.tasks.values():
            if task.status != "pending":
                continue
            deps_resolved = all(
                self.instance.tasks.get(dep_id) is not None
                and self.instance.tasks[dep_id].status in ("done", "deprecated")
                for dep_id in task.dependencies
            )
            if deps_resolved:
                ready.append(task)
        return ready
