#!/usr/bin/env python3
"""J-Claw engineering harness — entry point."""
from __future__ import annotations
import sys
import json
import shutil
import argparse
from pathlib import Path
from graphlib import TopologicalSorter

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

from config import PROJECTS_DIR, MAX_FORMAT5_DEPTH, ORCHESTRATOR_PROVIDER, ORCHESTRATOR_MODEL, ORCHESTRATOR_API_MODEL

# Display name shown in dashboard active-agent box during orchestrator calls
_ORCH_DISPLAY = ORCHESTRATOR_API_MODEL if ORCHESTRATOR_PROVIDER == "openrouter" else ORCHESTRATOR_MODEL
from orchestrator import Orchestrator, ManualOrchestrator, OpenRouterOrchestrator
from state_writer import writer as sw
from project import ProjectInstance
from scheduler import Scheduler
from final_review import run_final_review, parse_review_issues
from handoff import write_handoff, try_claude_stamp, git_commit_project, deploy_project
from verification import detect_ecosystem, run_playwright_project_check

console = Console()


def run_continuation(new_intent: str, project_dir: Path, auto_accept: bool = False) -> None:
    """Add features to an already-generated project."""
    import json as _json
    spec_path = project_dir / "spec.json"
    tasks_path = project_dir / "tasks_done.json"

    if not spec_path.exists():
        console.print(f"[red]No spec.json found in {project_dir} — run the project first.[/red]")
        sys.exit(1)

    spec = _json.loads(spec_path.read_text(encoding="utf-8"))
    completed = _json.loads(tasks_path.read_text(encoding="utf-8")) if tasks_path.exists() else []

    console.print(Panel(
        f"[bold cyan]Continuing: {spec.get('goal', '?')}[/bold cyan]\n"
        f"[dim]Adding: {new_intent}[/dim]",
        title="J-Claw Continuation"
    ))

    if ORCHESTRATOR_PROVIDER == "openrouter":
        orch = OpenRouterOrchestrator()
    else:
        orch = Orchestrator()

    sw.on_project_start(new_intent, str(project_dir))

    console.print("\n[bold]Planning continuation tasks…[/bold]")
    sw.on_agent_call("orchestrator", _ORCH_DISPLAY, "CONTINUE")
    dag_response = orch.call({
        "system_state": "CONTINUE",
        "existing_spec": spec,
        "completed_tasks": completed,
        "new_intent": new_intent,
    })
    sw.on_agent_done()

    if not dag_response.get("tasks"):
        console.print("[yellow]Orchestrator returned no new tasks.[/yellow]")
        return

    instance = ProjectInstance(project_dir)
    instance.spec = spec
    # Pre-populate with completed tasks so dependency references work
    instance.load_tasks(completed)
    # Load the new follow-up tasks
    instance.apply_format4_followups(dag_response["tasks"])
    sw.on_dag_loaded(dag_response["tasks"])

    console.print(f"\n[bold]Executing {len(dag_response['tasks'])} new task(s)…[/bold]")
    Scheduler(instance, orch).run()

    # Save updated tasks
    (project_dir / "tasks_done.json").write_text(
        _json.dumps(instance.tasks_as_list(), indent=2), encoding="utf-8"
    )

    passed = run_final_review(project_dir, spec)
    heal_cycle = 0
    handoff_path = write_handoff(project_dir, spec, passed, heal_cycle)
    try_claude_stamp(handoff_path, project_dir)
    git_commit_project(project_dir, {"goal": f"continuation: {new_intent}"})
    deploy_project(project_dir, spec)


def run_project(intent: str, output_dir: Path, depth: int = 0, manual: bool = False, auto_accept: bool = False) -> None:
    """Run one project instance from intent to completion (recursive for FORMAT 5)."""
    if depth > MAX_FORMAT5_DEPTH:
        console.print(
            f"[bold red]FORMAT 5 recursion depth exceeded ({depth}). "
            "Stopping — manual decomposition required.[/bold red]"
        )
        return

    console.print(Panel(f"[bold cyan]{intent}[/bold cyan]", title=f"J-Claw {'Sub-project ' + str(depth) if depth else 'Project'}"))

    # Wipe any output from a previous run so stale files don't contaminate
    # the new run's review or verification steps.
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if manual:
        orch = ManualOrchestrator()
    elif ORCHESTRATOR_PROVIDER == "openrouter":
        orch = OpenRouterOrchestrator()
    else:
        orch = Orchestrator()

    sw.on_project_start(intent, str(output_dir))

    # ── INIT ──────────────────────────────────────────────────────────────────
    console.print("\n[bold]Generating project spec…[/bold]")
    sw.on_agent_call("orchestrator", _ORCH_DISPLAY, "INIT")
    spec = orch.call({"system_state": "INIT", "user_intent": intent})
    sw.on_agent_done()

    if spec.get("oversize"):
        _handle_oversize(spec, output_dir, depth, auto_accept=auto_accept, manual=manual)
        return

    # Spec review loop
    while True:
        console.print("\n[bold]Proposed spec:[/bold]")
        console.print_json(json.dumps(spec, indent=2))
        if auto_accept or Confirm.ask("\n[bold green]Accept this spec?[/bold green]"):
            if auto_accept:
                console.print("[dim]Auto-accepting spec (--yes mode)[/dim]")
            break
        feedback = Prompt.ask("[bold yellow]Revision feedback[/bold yellow]")
        spec = orch.call({
            "system_state": "SPEC_REVISION",
            "current_spec": spec,
            "revision_feedback": feedback,
        })
        if spec.get("oversize"):
            _handle_oversize(spec, output_dir, depth, auto_accept=auto_accept, manual=manual)
            return

    # ── SPEC_ACCEPTED ─────────────────────────────────────────────────────────
    sw.on_spec_accepted(spec)
    import json as _json
    (output_dir / "spec.json").write_text(_json.dumps(spec, indent=2), encoding="utf-8")
    console.print("\n[bold]Generating task DAG…[/bold]")
    sw.on_agent_call("orchestrator", _ORCH_DISPLAY, "SPEC_ACCEPTED")
    dag_response = orch.call({"system_state": "SPEC_ACCEPTED", "accepted_spec": spec})
    sw.on_agent_done()

    if dag_response.get("oversize"):
        _handle_oversize(dag_response, output_dir, depth, auto_accept=auto_accept, manual=manual)
        return

    instance = ProjectInstance(output_dir)
    instance.spec = spec
    sw.on_dag_loaded(dag_response["tasks"])
    instance.load_tasks(dag_response["tasks"])

    console.print(f"\n[bold]Executing {len(instance.tasks)} task(s)…[/bold]")
    Scheduler(instance, orch).run()
    (output_dir / "tasks_done.json").write_text(
        _json.dumps(instance.tasks_as_list(), indent=2), encoding="utf-8"
    )

    console.print(f"\n[bold green]Project output written to: {output_dir}[/bold green]")

    # Project-level Playwright check for phaser/vanilla — runs regardless of
    # task verification settings (which are always "none" for these stacks).
    ecosystem = detect_ecosystem(output_dir)
    if ecosystem in ("phaser", "three-js", "unknown") and (output_dir / "index.html").exists():
        passed_pw, log_pw = run_playwright_project_check(output_dir)
        sw.on_verification_result("project", "playwright", ecosystem, passed_pw, log_pw)

    if not manual:
        _MAX_HEAL = 2
        passed = False
        heal_cycle = 0
        for heal_cycle in range(_MAX_HEAL + 1):
            passed = run_final_review(output_dir, instance.spec)
            if passed or heal_cycle == _MAX_HEAL:
                break

            issues = parse_review_issues(output_dir / "REVIEW.md")
            if not issues:
                console.print("  [yellow]No parseable issues in REVIEW.md — stopping heal loop.[/yellow]")
                break

            console.print(
                f"\n[yellow]Review flagged {len(issues)} issue(s) — requesting fix tasks "
                f"(heal cycle {heal_cycle + 1}/{_MAX_HEAL})…[/yellow]"
            )
            for i, issue in enumerate(issues, 1):
                console.print(f"  {i}. {issue}")

            sw.on_agent_call("orchestrator", ORCHESTRATOR_MODEL, "REVIEW_FAILED")
            fix_resp = orch.call({
                "system_state": "REVIEW_FAILED",
                "accepted_spec": instance.spec,
                "completed_tasks": instance.tasks_as_list(),
                "review_issues": issues,
            })
            sw.on_agent_done()

            followups = fix_resp.get("followup_tasks", [])
            if not followups:
                console.print("  [yellow]Orchestrator returned no fix tasks — stopping.[/yellow]")
                break

            instance.apply_format4_followups(followups)
            sw.on_tasks_added(followups)
            console.print(f"  Added {len(followups)} fix task(s). Re-running…\n")
            Scheduler(instance, orch).run()

        handoff_path = write_handoff(output_dir, instance.spec, passed, heal_cycle)
        try_claude_stamp(handoff_path, output_dir)
        git_commit_project(output_dir, instance.spec)
        deploy_project(output_dir, instance.spec)

        if not passed:
            sys.exit(1)


def _handle_oversize(response: dict, base_dir: Path, depth: int, auto_accept: bool = False, manual: bool = False) -> None:
    console.print(
        f"\n[bold yellow]Oversize project — decomposing into sub-projects.[/bold yellow]\n"
        f"  Reason: {response['reason']}"
    )

    sub_projects = response["sub_projects"]
    graph = {sp["name"]: set(sp.get("depends_on", [])) for sp in sub_projects}

    for name in TopologicalSorter(graph).static_order():
        sp = next(s for s in sub_projects if s["name"] == name)
        sp_dir = base_dir / name
        sp_dir.mkdir(parents=True, exist_ok=True)
        console.rule(f"[cyan]Sub-project: {name}[/cyan]")
        run_project(sp["goal"], sp_dir, depth + 1, manual=manual, auto_accept=auto_accept)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="J-Claw — local-first autonomous software harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n  python main.py \"A single-page to-do app\" -o ./projects/todo",
    )
    parser.add_argument("intent", nargs="?", help="Natural-language project description")
    parser.add_argument("--output", "-o", help="Output directory (default: ./projects/<slug>)")
    parser.add_argument("--manual", action="store_true",
                        help="You act as the orchestrator — no API key required")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Auto-accept the first spec without prompting")
    parser.add_argument("--continue", dest="continue_dir", metavar="PROJECT_DIR",
                        help="Continue an existing project — add features to it")
    args = parser.parse_args()

    intent: str = args.intent or Prompt.ask("[bold]Describe your project[/bold]")

    if args.continue_dir:
        cont_dir = Path(args.continue_dir)
        if not cont_dir.exists():
            console.print(f"[red]Project directory not found: {cont_dir}[/red]")
            sys.exit(1)
        try:
            run_continuation(intent, cont_dir, auto_accept=args.yes)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
            sys.exit(1)
        return

    if args.output:
        output_dir = Path(args.output)
    else:
        slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in intent[:50]).strip("_-")
        output_dir = PROJECTS_DIR / slug

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        run_project(intent, output_dir, manual=args.manual, auto_accept=args.yes)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        console.print(f"\n[bold red]Fatal error:[/bold red] {exc}")
        raise


if __name__ == "__main__":
    main()
