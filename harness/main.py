#!/usr/bin/env python3
"""J-Claw engineering harness — entry point."""
from __future__ import annotations
import sys
import json
import argparse
from pathlib import Path
from graphlib import TopologicalSorter

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

from config import PROJECTS_DIR, MAX_FORMAT5_DEPTH, ORCHESTRATOR_PROVIDER
from orchestrator import Orchestrator, ManualOrchestrator, OpenRouterOrchestrator
from state_writer import writer as sw
from project import ProjectInstance
from scheduler import Scheduler
from final_review import run_final_review

console = Console()


def run_project(intent: str, output_dir: Path, depth: int = 0, manual: bool = False, auto_accept: bool = False) -> None:
    """Run one project instance from intent to completion (recursive for FORMAT 5)."""
    if depth > MAX_FORMAT5_DEPTH:
        console.print(
            f"[bold red]FORMAT 5 recursion depth exceeded ({depth}). "
            "Stopping — manual decomposition required.[/bold red]"
        )
        return

    console.print(Panel(f"[bold cyan]{intent}[/bold cyan]", title=f"J-Claw {'Sub-project ' + str(depth) if depth else 'Project'}"))

    if manual:
        orch = ManualOrchestrator()
    elif ORCHESTRATOR_PROVIDER == "openrouter":
        orch = OpenRouterOrchestrator()
    else:
        orch = Orchestrator()

    sw.on_project_start(intent, str(output_dir))

    # ── INIT ──────────────────────────────────────────────────────────────────
    console.print("\n[bold]Generating project spec…[/bold]")
    sw.on_agent_call("orchestrator", "openrouter/auto", "INIT")
    spec = orch.call({"system_state": "INIT", "user_intent": intent})
    sw.on_agent_done()

    if spec.get("oversize"):
        _handle_oversize(spec, output_dir, depth, orch)
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
            _handle_oversize(spec, output_dir, depth, orch)
            return

    # ── SPEC_ACCEPTED ─────────────────────────────────────────────────────────
    sw.on_spec_accepted(spec)
    console.print("\n[bold]Generating task DAG…[/bold]")
    sw.on_agent_call("orchestrator", "openrouter/auto", "SPEC_ACCEPTED")
    dag_response = orch.call({"system_state": "SPEC_ACCEPTED", "accepted_spec": spec})
    sw.on_agent_done()

    if dag_response.get("oversize"):
        _handle_oversize(dag_response, output_dir, depth, orch)
        return

    instance = ProjectInstance(output_dir)
    instance.spec = spec
    sw.on_dag_loaded(dag_response["tasks"])
    instance.load_tasks(dag_response["tasks"])

    console.print(f"\n[bold]Executing {len(instance.tasks)} task(s)…[/bold]")
    Scheduler(instance, orch).run()

    console.print(f"\n[bold green]Project output written to: {output_dir}[/bold green]")

    if not manual:
        passed = run_final_review(output_dir, instance.spec)
        if not passed:
            sys.exit(1)


def _handle_oversize(response: dict, base_dir: Path, depth: int, orch) -> None:
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
        run_project(sp["goal"], sp_dir, depth + 1, manual=manual)


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
    args = parser.parse_args()

    intent: str = args.intent or Prompt.ask("[bold]Describe your project[/bold]")

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
