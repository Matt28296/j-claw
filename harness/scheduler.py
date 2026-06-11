from __future__ import annotations
import json
from concurrent.futures import ThreadPoolExecutor, wait as _cf_wait, FIRST_EXCEPTION
from pathlib import Path
from rich.console import Console

from config import MAX_RETRIES_PER_TASK, WORKER_MODEL, MAX_PARALLEL_WORKERS, MAX_TASKS, WORKER_TASK_TIMEOUT, WORKER_LADDER
from experience_log import log_outcome, get_relevant_hints
from project import ProjectInstance, Task
from worker import execute_task, routed_rung
from verification import run_verification, detect_ecosystem
from completeness import check_completeness
from validator import validate_dag, OrchestratorOutputError
from state_writer import writer as sw
from asset_worker import generate_assets, can_generate
from video_worker import generate_video, can_generate as video_can_generate

console = Console()

_STUB_PATTERNS = [
    "// existing logic",
    "// existing code",
    "// implementation unchanged",
    "// keep existing",
    "// ... existing",
    "// TODO: implement",
    "# TODO: implement",
    "pass  # stub",
    "raise NotImplementedError",
    "/* existing",
    "// placeholder",
    "# placeholder",
]


def _scan_for_stubs(output_files: dict) -> str | None:
    """Return the first stub pattern found across all written files, or None."""
    for path, content in output_files.items():
        lower = content.lower()
        for pat in _STUB_PATTERNS:
            if pat.lower() in lower:
                return f"{path}: contains '{pat}'"
    return None


# Binary raster extensions. A task producing ONLY these is routed to asset_worker regardless of
# its declared type, so the code worker never has to emit binary (base64-in-JSON) content —
# which reliably fails on local models and forces expensive paid escalation.
_ASSET_BINARY_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".bmp"}


def _is_asset_task(task) -> bool:
    """True for declared asset tasks, or any task whose outputs are all binary images."""
    if task.type == "asset":
        return True
    files = getattr(task, "files", None) or []
    return bool(files) and all(Path(f).suffix.lower() in _ASSET_BINARY_EXTS for f in files)


_VIDEO_OUTPUT_EXTS = {".mp4", ".webm", ".mov", ".avi"}


def _is_video_task(task) -> bool:
    """True only when the task's declared files include an actual video output.

    Routing is by OUTPUT, not label, in both directions:
    - a task labelled 'backend' whose every output is an .mp4 must go to
      video_worker (a code model can only emit text);
    - a task labelled 'video' that declares only text files (render.sh,
      shotlist.json) is the DIRECTOR writing scripts — that's code-worker work.
      Observed live: render.sh typed 'video' was silently skipped by
      video_worker and marked done with nothing written.
    """
    files = getattr(task, "files", None) or []
    has_video_output = any(Path(f).suffix.lower() in _VIDEO_OUTPUT_EXTS for f in files)
    if task.type in ("video", "editing", "composition", "vfx"):
        return has_video_output
    return bool(files) and all(Path(f).suffix.lower() in _VIDEO_OUTPUT_EXTS for f in files)


class Scheduler:
    def __init__(self, instance: ProjectInstance, orchestrator) -> None:
        self.instance = instance
        self.orch = orchestrator

    def run(self) -> None:
        """Execute all tasks to completion (or terminal failure).

        Outer loop: after all tasks finish, run PROJECT_REVIEW; if it injects follow-up fix
        tasks, loop back and execute them (bounded by _MAX_REVIEW_ROUNDS) instead of silently
        dropping them.
        """
        _MAX_REVIEW_ROUNDS = 2
        review_rounds = 0

        while True:
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
                    return  # stalled/deadlocked — not actually done, skip review

                self._dispatch_batch(ready)

            # All tasks done. Run PROJECT_REVIEW once; if it added follow-ups, loop to run them.
            if review_rounds >= _MAX_REVIEW_ROUNDS:
                break
            review_rounds += 1
            if not self._project_review():
                break

    def _dispatch_batch(self, ready: list[Task]) -> None:
        """Run a batch of ready tasks under a per-batch timeout.

        ALL batches go through the timeout path — including single-task batches — so one hung
        worker cannot stall the pipeline indefinitely (the previous serial path had no timeout).

        KNOWN LIMITATION: _cf_wait() bounds how long we WAIT, and fut.cancel() cannot stop a
        thread that has already started, so the `with` block's implicit shutdown(wait=True) will
        still block on a truly uninterruptible worker until it returns. The timeout therefore
        only guarantees liveness if every worker I/O path (Ollama HTTP, subprocesses) carries its
        own internal timeout — which they currently do (WORKER_TASK_TIMEOUT / request timeouts).
        Do not remove those inner timeouts assuming this wait alone bounds wall-clock.
        """
        workers = max(1, min(MAX_PARALLEL_WORKERS, len(ready)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._run_task, task): task for task in ready}
            done, not_done = _cf_wait(list(futures.keys()), timeout=WORKER_TASK_TIMEOUT)
            for fut in not_done:
                task = futures[fut]
                task.status = "failed"
                task.error_log = f"Task timed out after {WORKER_TASK_TIMEOUT}s"
                sw.on_task_failed(task.id, task.error_log, task.retry_count + 1)
                console.print(f"  [red]Task {task.id} timed out ({WORKER_TASK_TIMEOUT}s) — retrying.[/red]")
                fut.cancel()
                self._handle_error(task)
            for fut in done:
                exc = fut.exception()
                if exc:
                    console.print(f"  [red]Worker thread raised: {exc}[/red]")

    # ── task execution ────────────────────────────────────────────────────────

    def _run_task(self, task: Task) -> None:
        console.print(
            f"\n[bold cyan]▶ {task.id}[/bold cyan] [{task.type}]  "
            f"{task.objective[:90]}{'…' if len(task.objective) > 90 else ''}"
        )
        task.status = "running"
        sw.on_task_start(task.id)

        dep_files = self.instance.get_dependency_files(task)

        # Asset tasks (declared, or any task that produces only binary image files) route to
        # asset_worker — keeps binary base64-in-JSON generation out of the code worker.
        if _is_asset_task(task):
            written = generate_assets(task, self.instance.spec, self.instance.output_dir)
            result = {"files": [], "model_used": "sd-webui" if can_generate() else "placeholder"}
            task.status = "done"
            sw.on_task_done(task.id, result["model_used"])
            console.print(f"  [green]✓ asset done[/green]  [dim]({len(written)} file(s) written)[/dim]")
            return

        # Audio tasks: route to audio_worker instead of code worker
        if task.type == "audio":
            from audio_worker import generate_audio, can_generate as audio_can_generate
            written = generate_audio(task, self.instance.spec, self.instance.output_dir)
            result = {"files": [], "model_used": "coqui-tts" if audio_can_generate() else "silent-placeholder"}
            task.status = "done"
            sw.on_task_done(task.id, result["model_used"])
            console.print(f"  [green]✓ audio done[/green]  [dim]({len(written)} file(s) written)[/dim]")
            return

        # Video tasks: route to video_worker (incl. mistyped tasks whose outputs
        # are all video files — the code worker cannot produce binary video).
        if _is_video_task(task):
            written, failures = generate_video(task, self.instance.spec, self.instance.output_dir)
            task.binary_outputs = {str(p.relative_to(self.instance.output_dir)): p for p in written}
            # Same declared-files guarantee as the code-worker path: a video
            # task mixing text outputs (render.sh) with its video output cannot
            # have the text half silently skipped.
            for rel in (getattr(task, "files", None) or []):
                if rel not in failures and not (self.instance.output_dir / rel).exists():
                    failures[rel] = ("declared file was never produced — video_worker has no "
                                     "content for it; declare script files in a code task instead")
            if failures:
                task.status = "failed"
                task.error_log = "Video render failed:\n" + "\n".join(
                    f"  - {rel}: {reason}" for rel, reason in failures.items()
                )
                sw.on_task_failed(task.id, task.error_log, task.retry_count + 1)
                console.print(f"  [red]✗ video render failed ({len(failures)} file(s))[/red]")
                self._handle_error(task)
                return
            model_used = "ffmpeg" if video_can_generate() else "video-stub"
            # Video tasks must pass their declared verification (ffprobe/
            # frame_integrity/sync_check) like any other task — previously they
            # were marked done without ever running it.
            passed, log = run_verification(task, self.instance.output_dir)
            sw.on_verification_result(task.id, task.verification, "film", passed, log)
            if not passed:
                task.status = "failed"
                task.error_log = f"Verification ({task.verification}) failed:\n{log}"
                sw.on_task_failed(task.id, task.error_log, task.retry_count + 1)
                self._handle_error(task)
                return
            task.status = "done"
            sw.on_task_done(task.id, model_used)
            console.print(f"  [green]✓ video done[/green]  [dim]({len(written)} file(s) written)[/dim]")
            return

        # Music tasks: route to music_worker
        if task.type == "music":
            from music_worker import generate_music
            written = generate_music(task, self.instance.spec, self.instance.output_dir)
            task.status = "done"
            sw.on_task_done(task.id, "music_worker")
            console.print(f"  [green]✓ music done[/green]  [dim]({len(written)} file(s) written)[/dim]")
            return

        if WORKER_LADDER:
            rung = routed_rung(task)
            prov, mdl = WORKER_LADDER[rung]
            esc = "  [yellow](escalated)[/yellow]" if task.retry_count else ""
            console.print(
                f"  [dim]routed → rung {rung}: {prov}/{mdl}  "
                f"(type={task.type}, files={len(task.files)}, deps={len(task.dependencies)})[/dim]{esc}"
            )

        try:
            context = _build_context(task, self.instance.output_dir)
            result = execute_task(task, self.instance.spec, dep_files, context)
            task.output_files = {f["path"]: f["content"] for f in result["files"]}
            self.instance.write_task_files(task)
            for path in task.output_files:
                sw.on_file_written(path, task.id)

            # Apply memory patch if worker produced one
            patch_json = task.output_files.get("memory_patch.json")
            if patch_json:
                _apply_memory_patch(patch_json, self.instance.output_dir, task.id)

            # A task is only done when every file it DECLARED exists on disk
            # (non-empty, except intentional dotfiles like .gitkeep). Workers
            # reliably return plausible JSON that omits the hard file — observed
            # live: render.sh "done" but never materialized across 3 heal cycles.
            missing_decl = []
            for rel in (task.files or []):
                p = self.instance.output_dir / rel
                if not p.exists() or (p.stat().st_size == 0 and not p.name.startswith(".")):
                    missing_decl.append(rel)
            if missing_decl:
                raise ValueError(
                    "Declared output file(s) never written: " + ", ".join(missing_decl)
                    + " — the task must emit the COMPLETE content of every file in its files list"
                )

            stub_hit = _scan_for_stubs(task.output_files)
            if stub_hit:
                raise ValueError(f"Stub detected in output: {stub_hit}")

            ecosystem = detect_ecosystem(self.instance.output_dir)
            passed, log = run_verification(task, self.instance.output_dir)
            comp_ok, comp_issues = check_completeness(files=task.output_files, ecosystem=ecosystem)
            if not comp_ok:
                passed = False
                _comp = "\n".join(f"  - {i}" for i in comp_issues)
                log = (log + "\n" if log else "") + "Completeness gate failed:\n" + _comp
            sw.on_verification_result(task.id, task.verification, ecosystem, passed, log)
            if passed:
                model_used = result.get("model_used", WORKER_MODEL)
                task.status = "done"
                sw.on_task_done(task.id, model_used)
                console.print(f"  [green]✓ done[/green]  [dim](worker: {model_used})[/dim]")
            else:
                task.status = "failed"
                task.error_log = f"Verification ({task.verification}) failed:\n{log}"
                sw.on_task_failed(task.id, task.error_log, task.retry_count + 1)
                self._handle_error(task)

        except Exception as exc:  # noqa: BLE001
            task.status = "failed"
            task.error_log = str(exc)
            console.print(f"  [red]✗ error: {exc}[/red]")
            sw.on_task_failed(task.id, task.error_log, task.retry_count + 1)
            if task.retry_count >= MAX_RETRIES_PER_TASK:
                log_outcome(task.id, task.type, str(exc)[:200], "none", "", succeeded=False,
                            stack=self.instance.spec.get("stack", ""))
            self._handle_error(task)

    # ── error handling ────────────────────────────────────────────────────────

    def _handle_error(self, task: Task) -> None:
        task.retry_count += 1
        if task.retry_count > MAX_RETRIES_PER_TASK:
            console.print(
                f"  [red]{task.id} exhausted {MAX_RETRIES_PER_TASK} retries — manual intervention required.[/red]"
            )
            return

        hints = get_relevant_hints(task.type, task.error_log[:200])

        console.print(
            f"  [yellow]Requesting EXECUTION_ERROR refinement "
            f"(attempt {task.retry_count}/{MAX_RETRIES_PER_TASK})…[/yellow]"
        )

        payload: dict = {
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
        }
        if hints:
            payload["experience_hints"] = hints

        refinement = self.orch.call(payload)

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
        log_outcome(
            task.id,
            task.type,
            task.error_log[:200],
            refinement["action"],
            refinement["updated_tasks"][0]["objective"] if refinement.get("updated_tasks") else "",
            succeeded=True,
            stack=self.instance.spec.get("stack", ""),
        )

    # ── project review ────────────────────────────────────────────────────────

    def _project_review(self) -> bool:
        """Run PROJECT_REVIEW. Returns True if follow-up fix tasks were injected (so run()
        should loop and execute them), False if the project passed or no follow-ups apply."""
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
            sw.on_project_done(result, review["summary"])
            return False

        followups = review.get("followup_tasks", [])
        if not followups:
            return False

        budget = self.instance.active_dag_count() + len(followups)
        if budget > MAX_TASKS:
            console.print(
                f"[red]Follow-up tasks would push Active DAG to {budget} (> {MAX_TASKS} limit). Stopping.[/red]"
            )
            return False

        try:
            validate_dag(followups, self.instance.tasks_as_list())
        except OrchestratorOutputError as exc:
            console.print(f"[red]Follow-up DAG invalid: {exc}  — skipping follow-ups.[/red]")
            return False

        self.instance.apply_format4_followups(followups)
        console.print(f"  Added {len(followups)} follow-up task(s). Continuing execution…")
        # run()'s outer loop will now re-enter the execution loop and run these tasks.
        return True

    # ── helpers ───────────────────────────────────────────────────────────────

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


# ── module-level helpers ──────────────────────────────────────────────────────

def _build_context(task, output_dir: Path) -> dict | None:
    """Build structured context for a task. Returns None if project_memory/ not initialized."""
    try:
        from context_builder import ContextBuilder
        return ContextBuilder().build(task, output_dir)
    except Exception:
        return None


def _apply_memory_patch(patch_json: str, output_dir: Path, task_id: str) -> None:
    """Validate and apply a memory_patch.json produced by a worker."""
    try:
        from memory_validator import MemoryValidator
        from project_memory import ProjectMemory
        patch = json.loads(patch_json)
        pm = ProjectMemory(output_dir)
        if not pm.exists():
            return
        result = MemoryValidator().validate(patch, pm.root)
        if result.ok:
            pm.apply_patch(patch)
            console.print(
                f"  [dim]Memory patch applied ({result.outcome})"
                f"{' — ' + result.reason if result.reason else ''}[/dim]"
            )
        else:
            console.print(
                f"  [yellow]Memory patch from {task_id} rejected: {result.reason}[/yellow]"
            )
    except Exception as exc:
        console.print(f"  [yellow]Memory patch error ({task_id}): {exc}[/yellow]")
