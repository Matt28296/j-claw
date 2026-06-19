#!/usr/bin/env python3
"""J-Claw engineering harness — entry point."""
from __future__ import annotations
import sys
import json
import re
import shutil
import stat
import argparse
import time
from pathlib import Path
from graphlib import TopologicalSorter

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

from config import (
    PROJECTS_DIR, MAX_FORMAT5_DEPTH, FORCE_FORMAT5, MIN_SUBPROJECT_COUNT,
    ORCHESTRATOR_PROVIDER, ORCHESTRATOR_MODEL,
    ORCHESTRATOR_API_MODEL, TECHNICAL_ARCHITECT_ENABLED, DASHBOARD_PORT, DASHBOARD_AUTOOPEN,
    PIPELINE_MAX_RETRIES,
    HEAL_MAX_CYCLES,
    GEMINI_ORCHESTRATOR_MODEL,
    spec_stack,
)
from completeness import check_completeness

# Display name shown in dashboard active-agent box during orchestrator calls
_ORCH_DISPLAY = {
    "openrouter": ORCHESTRATOR_API_MODEL,
    "gemini": GEMINI_ORCHESTRATOR_MODEL,
}.get(ORCHESTRATOR_PROVIDER, ORCHESTRATOR_MODEL)
from orchestrator import Orchestrator, ManualOrchestrator, OpenRouterOrchestrator, GeminiOrchestrator, make_orchestrator
from state_writer import writer as sw
from project import ProjectInstance
from scheduler import Scheduler
from final_review import run_final_review, parse_review_issues
from handoff import (write_handoff, write_parent_handoff, try_claude_stamp,
                     git_commit_project, deploy_project, append_deploy_section)
from verification import detect_ecosystem, run_playwright_project_check
from e2e_generator import generate_e2e_tests, run_e2e_tests
from creative_director import CreativeDirector
from technical_architect import TechnicalArchitect
from cost import reset_costs, cost_summary, format_cost_line, BuildCostCeilingExceeded
from notify import notify_build_outcome, notify_crash
from experience_log import get_stack_lessons

console = Console()


def _write_failure_handoff(output_dir: Path, intent: str, phase: str, exc: Exception) -> None:
    """Write a minimal HANDOFF.md when the pipeline crashes so the folder is never empty."""
    content = (
        "# J-Claw Handoff Report\n\n"
        f"**Status:** ✗ PIPELINE FAILURE — crashed in {phase} phase\n"
        f"**Project:** {intent}\n"
        f"**Error:** {type(exc).__name__}: {str(exc)[:500]}\n"
        f"**Output directory:** {output_dir.resolve()}\n\n"
        "## Recovery\n"
        f'Re-run: `python main.py --yes "{intent}" --output "{output_dir}"`\n'
    )
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "HANDOFF.md").write_text(content, encoding="utf-8")
        console.print(f"  [yellow]Failure report written to: {output_dir / 'HANDOFF.md'}[/yellow]")
    except Exception:
        pass


def _handoff_has_stamp_issues(handoff_path: Path) -> bool:
    """True unless the independent OpenClaw stamp is an explicit, unambiguous APPROVED.

    H4 — default NOT-clean. The old check was exact-string `"OPENCLAW: ISSUES FOUND"`
    and defaulted GREEN on every other case: a missing stamp (claude CLI absent /
    no API key), a timed-out stamp (nothing appended), an unreadable handoff, or a
    paraphrased verdict that never emitted the literal marker. All of those silently
    rendered a green check the stamp never actually granted. Invert the default:
    render green ONLY when an explicit `OPENCLAW: APPROVED` marker is present AND no
    `OPENCLAW: ISSUES FOUND` marker is — every other (missing/ambiguous/unreadable)
    case is treated as not-clean (returns True = has issues)."""
    try:
        text = handoff_path.read_text(encoding="utf-8")
    except Exception:
        return True  # unreadable handoff — cannot confirm a clean stamp
    if "OPENCLAW: ISSUES FOUND" in text:
        return True
    # Require the explicit APPROVED marker. A missing/paraphrased stamp is ambiguous
    # and must not render green on the strength of absence alone.
    return "OPENCLAW: APPROVED" not in text


def _dashboard_running() -> bool:
    """True when something already listens on the dashboard port. Prevents every
    build run from stacking another dashboard.py onto the port (Windows allows
    multiple binds via SO_REUSEADDR; stale instances then wedge the connection
    lottery and the UI stops getting data)."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", DASHBOARD_PORT), timeout=1.0):
            return True
    except OSError:
        return False


def _start_dashboard() -> None:
    """Start dashboard.py in the background and optionally open the browser."""
    import subprocess
    repo_root = Path(__file__).parent.parent
    try:
        if not _dashboard_running():
            subprocess.Popen(
                [sys.executable, "dashboard.py"],
                cwd=str(repo_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if DASHBOARD_AUTOOPEN:
            import webbrowser, time as _t
            _t.sleep(0.8)
            webbrowser.open(f"http://localhost:{DASHBOARD_PORT}")
    except Exception as exc:
        console.print(f"  [yellow]Dashboard failed to start: {exc}[/yellow]")


def run_continuation(new_intent: str, project_dir: Path, auto_accept: bool = False) -> bool:
    """Add features to an already-generated project. Returns True on PASS."""
    import json as _json
    spec_path = project_dir / "spec.json"
    tasks_path = project_dir / "tasks_done.json"

    if not spec_path.exists():
        console.print(f"[red]No spec.json found in {project_dir} — run the project first.[/red]")
        sys.exit(1)

    spec = _json.loads(spec_path.read_text(encoding="utf-8"))
    completed = _json.loads(tasks_path.read_text(encoding="utf-8")) if tasks_path.exists() else []

    brief_path = project_dir / "creative_brief.json"
    creative_brief = _json.loads(brief_path.read_text(encoding="utf-8")) if brief_path.exists() else {}

    console.print(Panel(
        f"[bold cyan]Continuing: {spec.get('goal', '?')}[/bold cyan]\n"
        f"[dim]Adding: {new_intent}[/dim]",
        title="J-Claw Continuation"
    ))

    # A continuation is a fresh orchestration pass on a new intent: reset the per-run
    # paid-call budget and the Gemini quota latch so neither leaks in from a prior run
    # if this is ever driven in-process (harmless no-op under the subprocess-per-run model).
    from worker import reset_paid_budget
    from orchestrator import reset_orchestrator_run
    reset_paid_budget()
    reset_orchestrator_run()

    orch = make_orchestrator()

    sw.on_project_start(new_intent, str(project_dir))

    console.print("\n[bold]Planning continuation tasks…[/bold]")
    sw.on_agent_call("orchestrator", _ORCH_DISPLAY, "CONTINUE")
    dag_response = orch.call({
        "system_state": "CONTINUE",
        "existing_spec": spec,
        "completed_tasks": completed,
        "new_intent": new_intent,
        "creative_brief": creative_brief,
    })
    sw.on_agent_done()

    if not dag_response.get("tasks"):
        console.print("[yellow]Orchestrator returned no new tasks.[/yellow]")
        sw.on_no_continuation_tasks("Orchestrator returned no new continuation tasks")
        return False

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

    heal_cycle = 0
    passed = run_final_review(project_dir, spec)
    sw.on_final_review_result(passed, heal_cycle=heal_cycle)
    handoff_path = write_handoff(project_dir, spec, passed, heal_cycle)
    try_claude_stamp(handoff_path, project_dir)
    git_commit_project(project_dir, {"goal": f"continuation: {new_intent}"})
    deploy_url, deploy_note = deploy_project(project_dir, spec)
    append_deploy_section(handoff_path, deploy_url, deploy_note)
    sw.on_deploy(deploy_url, deploy_note)
    _cost = cost_summary()
    sw.on_cost(_cost)
    sw.on_project_done("pass" if passed else "needs_followup", "Continuation final review complete")
    notify_build_outcome(
        project=f"continuation: {new_intent}"[:120],
        passed=passed,
        heal_cycles=heal_cycle,
        max_heal=HEAL_MAX_CYCLES,
        handoff_path=handoff_path,
        cost_line=format_cost_line(),
        stamp_issues=_handoff_has_stamp_issues(handoff_path),
        deploy_url=deploy_url,
    )
    return passed


def _subproject_decomposition_allowed(depth: int) -> bool:
    """Whether a project at this FORMAT-5 depth may still decompose further (#5 escape
    valve). True at the top level (depth 0) and for sub-projects below MAX_FORMAT5_DEPTH;
    False at/above the cap, where the sub-project must flatten to a ≤50-task spec. This
    activates the previously-dead MAX_FORMAT5_DEPTH knob (the old guard force-flattened
    every sub-project unconditionally). Set MAX_FORMAT5_DEPTH=1 to restore that strict rule."""
    return depth < MAX_FORMAT5_DEPTH


def _build_disposition(review_passed: bool, dynamic_passed: bool, failed_tasks: list,
                       all_done: bool = True) -> bool:
    """Honest overall build verdict (#6, #H2).

    PASS requires the final review AND the dynamic checks to pass AND that no task
    failed verification and exhausted its retries AND that every task actually
    finished. Task statuses were previously ignored — the verdict was only
    `review_passed and dynamic_passed` — so a build with a hard-failed/stalled task
    could still report PASS. `failed_tasks` is the list from
    ProjectInstance.failed_tasks(); a non-empty list fails the build.

    H2: a scheduler deadlock (unsatisfiable dependency cycle) leaves tasks stuck in
    `pending` — never `failed` — so `failed_tasks` stays empty and the stalled build
    used to PASS. `all_done` is ProjectInstance.all_tasks_done() read after the
    scheduler returns; a False value (some task never ran) fails the build too."""
    return bool(review_passed and dynamic_passed and not failed_tasks and all_done)


def run_project(intent: str, output_dir: Path, depth: int = 0, manual: bool = False, auto_accept: bool = False, wiring: dict | None = None) -> bool:
    """Run one project instance from intent to completion (recursive for FORMAT 5).

    Returns True when the build passed (review + dynamic checks, or the
    aggregate of all sub-projects for a FORMAT 5 decomposition)."""
    if depth > MAX_FORMAT5_DEPTH:
        console.print(
            f"[bold red]FORMAT 5 recursion depth exceeded ({depth}). "
            "Stopping — manual decomposition required.[/bold red]"
        )
        return False

    console.print(Panel(f"[bold cyan]{intent}[/bold cyan]", title=f"J-Claw {'Sub-project ' + str(depth) if depth else 'Project'}"))

    # Wipe any output from a previous run so stale files don't contaminate
    # the new run's review or verification steps.
    if output_dir.exists():
        def _force_remove_readonly(func, path, exc):
            # Windows locks .git object files as read-only; chmod before retry.
            import os
            os.chmod(path, stat.S_IWRITE)
            func(path)
        from permissions import observe
        observe("fs_delete", detail=f"pre-run wipe of output_dir {output_dir}")  # roadmap #6: observe-only
        shutil.rmtree(output_dir, onexc=_force_remove_readonly)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Reset the per-project paid (cloud) worker-call budget for this run.
    from worker import reset_paid_budget
    from orchestrator import reset_orchestrator_run
    reset_paid_budget()
    reset_orchestrator_run()  # clear the Gemini quota latch so it can't persist across runs
    # C1: the cost ceiling is build-GLOBAL, not per-sub-project. Reset the cost
    # accumulator ONCE for the top-level run (depth 0). A FORMAT-5 sub-project is
    # itself a run_project() call (depth > 0); resetting there would re-arm a fresh
    # budget per scene, letting a 10-scene build legally spend 10× the ceiling
    # unattended. The accumulator must persist across the whole decomposition so a
    # tripped ceiling halts the entire build.
    if depth == 0:
        reset_costs()

    # Mutable holder so the failure handoff below can report the phase the
    # pipeline actually crashed in (updated in-place by _run_project_inner).
    phase = {"current": "pipeline"}
    try:
        return _run_project_inner(intent, output_dir, depth, manual, auto_accept, wiring, phase)
    except Exception as exc:
        _write_failure_handoff(output_dir, intent, phase["current"], exc)
        sw.on_project_failed(f"{type(exc).__name__}: {exc}", phase["current"])
        raise


def _difficulty_from_brief(brief: dict) -> str | None:
    """Derive orchestrator difficulty tier from the creative brief's scale field.

    scale → difficulty mapping (Phase 4):
      "prototype" → "simple"   (Haiku primary, cheapest)
      "mvp"       → "medium"   (Codex-first, Sonnet fallback)
      "production" → "complex" (Sonnet → Opus, full capability)
      missing/unknown → None   (existing behavior, plain Sonnet)

    Cross-check: if the brief has more than 12 features, bump one tier up.
    Tier order: simple → medium → complex (cap at complex).
    """
    _TIER_ORDER = ["simple", "medium", "complex"]
    scale = brief.get("scale", "")
    difficulty = {
        "prototype": "simple",
        "mvp": "medium",
        "production": "complex",
    }.get(scale)

    if difficulty is None:
        return None

    # Bump one tier if the feature count suggests higher complexity than the scale label implies.
    features = brief.get("features", [])
    if isinstance(features, list) and len(features) > 12:
        idx = _TIER_ORDER.index(difficulty)
        difficulty = _TIER_ORDER[min(idx + 1, len(_TIER_ORDER) - 1)]

    return difficulty


def _bump_difficulty(difficulty: str | None) -> str | None:
    """Bump difficulty one tier up for heal-loop re-planning. Returns None unchanged."""
    _TIER_ORDER = ["simple", "medium", "complex"]
    if difficulty is None:
        return None
    idx = _TIER_ORDER.index(difficulty) if difficulty in _TIER_ORDER else -1
    if idx < 0:
        return difficulty
    return _TIER_ORDER[min(idx + 1, len(_TIER_ORDER) - 1)]


def _run_project_inner(intent: str, output_dir: Path, depth: int, manual: bool, auto_accept: bool, wiring: dict | None, phase: dict) -> bool:
    """Inner pipeline body — separated so run_project() can catch + report failures."""

    _start_dashboard()

    sw.on_project_start(intent, str(output_dir))
    phase["current"] = "creative-director"

    # ── Creative Director pre-pass ────────────────────────────────────────────
    console.print("\n[bold]Creative Director interpreting intent...[/bold]")
    try:
        creative_brief = CreativeDirector().interpret(intent)
        import json as _json_cd
        (output_dir / "creative_brief.json").write_text(
            _json_cd.dumps(creative_brief, indent=2), encoding="utf-8"
        )
    except Exception as _cd_exc:
        console.print(f"  [yellow]Creative Director skipped ({_cd_exc})[/yellow]")
        creative_brief = {}

    # ── Difficulty routing (Phase 4) ──────────────────────────────────────────
    # Derive difficulty from the creative brief's scale field BEFORE building the
    # orchestrator so make_orchestrator can select the right model tier.
    # manual mode bypasses difficulty routing (ManualOrchestrator ignores it).
    _difficulty: str | None = _difficulty_from_brief(creative_brief) if creative_brief else None
    if _difficulty:
        console.print(f"  [dim]Project difficulty: {_difficulty} (scale={creative_brief.get('scale', '?')})[/dim]")

    orch = make_orchestrator(manual=manual, difficulty=_difficulty)

    # ── Technical Architect pass ──────────────────────────────────────────────
    phase["current"] = "technical-architect"
    tech_spec: dict = {}
    if TECHNICAL_ARCHITECT_ENABLED and creative_brief:
        console.print("\n[bold]Technical Architect reviewing brief...[/bold]")
        try:
            tech_spec = TechnicalArchitect().review(creative_brief, intent, output_dir)
            import json as _json_ta
            (output_dir / "tech_spec.json").write_text(
                _json_ta.dumps(tech_spec, indent=2), encoding="utf-8"
            )
        except Exception as _ta_exc:
            console.print(f"  [yellow]Technical Architect skipped ({_ta_exc})[/yellow]")

    # ── INIT ──────────────────────────────────────────────────────────────────
    phase["current"] = "init"
    console.print("\n[bold]Generating project spec…[/bold]")
    sw.on_agent_call("orchestrator", _ORCH_DISPLAY, "INIT")
    init_payload: dict = {
        "system_state": "INIT",
        "user_intent": intent,
        "creative_brief": creative_brief,
    }
    # Cross-project learning: recurring failure patterns from past builds on
    # this stack, so the orchestrator designs tasks that avoid them up front.
    _lessons = get_stack_lessons((tech_spec or {}).get("confirmed_stack", ""))
    if _lessons:
        init_payload["past_failure_lessons"] = _lessons
    if tech_spec:
        init_payload["tech_spec"] = tech_spec
    if wiring:
        init_payload["wiring"] = wiring
    if depth:
        # Escape valve (#5): a sub-project that is itself over-scoped may decompose
        # ONE more level while there is depth headroom, bounded by MAX_FORMAT5_DEPTH
        # (and the hard backstop at run_project top, main.py:219). At/above the cap it
        # is force-flattened — preserving the original anti-spiral behaviour that the
        # earlier unconditional guard provided. Set MAX_FORMAT5_DEPTH=1 to restore the
        # strict "sub-projects never decompose" rule.
        init_payload["sub_project_depth"] = depth
        init_payload["decomposition_allowed"] = _subproject_decomposition_allowed(depth)
    # Test-harness escape hatch (#FORCE_FORMAT5): at the top level only, hand the
    # orchestrator a hard directive to decompose, bypassing its scale-down heuristic.
    # Sub-projects (depth>0) are never forced — they must flatten per the recursion cap.
    if FORCE_FORMAT5 and depth == 0:
        init_payload["decomposition_required"] = (
            f"FORCE_FORMAT5 is active for this run. You MUST emit an oversize FORMAT 5 "
            f"spec that decomposes this intent into at least {MIN_SUBPROJECT_COUNT} "
            f"independent, coherent sub-projects (oversize=true with a 'sub_projects' "
            f"list). Do NOT flatten to a single FORMAT 1 spec even if the intent seems "
            f"small — split it along natural service/module boundaries."
        )

    spec = orch.call(init_payload)
    sw.on_agent_done()

    # FORCE_FORMAT5 enforcement: if the orchestrator still returned a flat spec, re-request
    # once with a sharper directive, then fail honestly rather than run a flat build that
    # would silently NOT exercise the decomposing path (the whole point of the run).
    if FORCE_FORMAT5 and depth == 0 and not spec.get("oversize"):
        console.print(
            "  [yellow]FORCE_FORMAT5: orchestrator returned a FLAT spec — re-requesting "
            "decomposition.[/yellow]"
        )
        sw.on_agent_call("orchestrator", _ORCH_DISPLAY, "INIT")
        spec = orch.call({
            **init_payload,
            "decomposition_required": (
                f"Your previous response was a FLAT spec. This run REQUIRES a FORMAT 5 "
                f"decomposition into at least {MIN_SUBPROJECT_COUNT} independent sub-projects "
                f"(oversize=true with a 'sub_projects' list). Emit that now."
            ),
        })
        sw.on_agent_done()
        if not spec.get("oversize"):
            console.print(
                "  [bold red]FORCE_FORMAT5: orchestrator refused to decompose after a retry. "
                "Aborting — a flat build would not exercise FORMAT 5.[/bold red]"
            )
            return False

    if spec.get("oversize"):
        if not _subproject_decomposition_allowed(depth):
            # At the recursion cap — runtime enforcement, never trust the prompt alone.
            # One corrective retry, then an honest failure instead of a recursion spiral.
            console.print(
                "  [yellow]Sub-project tried to decompose at the recursion cap "
                f"(depth {depth} ≥ MAX_FORMAT5_DEPTH {MAX_FORMAT5_DEPTH}) — rejecting "
                "and requesting a flat FORMAT 1 spec.[/yellow]"
            )
            sw.on_agent_call("orchestrator", _ORCH_DISPLAY, "INIT")
            spec = orch.call({
                **init_payload,
                "decomposition_rejected": (
                    f"You are a sub-project at depth {depth}, the recursion cap "
                    f"(MAX_FORMAT5_DEPTH={MAX_FORMAT5_DEPTH}). Further decomposition is "
                    "FORBIDDEN. Emit a flat FORMAT 1 spec (≤50 tasks) that builds this one "
                    "scene/segment directly. Trim scope to fit if needed."
                ),
            })
            sw.on_agent_done()
            if spec.get("oversize"):
                console.print("  [red]Sub-project still demands decomposition at the cap — failing honestly.[/red]")
                return False
        else:
            # Top-level (depth 0) OR an under-cap sub-project: allow decomposition.
            return _handle_oversize(spec, output_dir, depth, auto_accept=auto_accept, manual=manual,
                                    intent=intent,
                                    parent_stack=(tech_spec or {}).get("confirmed_stack", ""))

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
            if not _subproject_decomposition_allowed(depth):
                console.print("  [red]Sub-project revision demands decomposition at the recursion cap — failing honestly.[/red]")
                return False
            return _handle_oversize(spec, output_dir, depth, auto_accept=auto_accept, manual=manual,
                                    intent=intent,
                                    parent_stack=(tech_spec or {}).get("confirmed_stack", ""))

    # ── SPEC_ACCEPTED ─────────────────────────────────────────────────────────
    phase["current"] = "dag-generation"
    sw.on_spec_accepted(spec)
    import json as _json
    (output_dir / "spec.json").write_text(_json.dumps(spec, indent=2), encoding="utf-8")
    console.print("\n[bold]Generating task DAG…[/bold]")
    sw.on_agent_call("orchestrator", _ORCH_DISPLAY, "SPEC_ACCEPTED")
    _dag_payload: dict = {"system_state": "SPEC_ACCEPTED", "accepted_spec": spec}
    _lessons = get_stack_lessons(spec_stack(spec) or (tech_spec or {}).get("confirmed_stack", ""))
    if _lessons:
        _dag_payload["past_failure_lessons"] = _lessons
    if depth:
        # Mirror the INIT escape valve (#5): allow one more decomposition level while
        # under MAX_FORMAT5_DEPTH, force-flatten at/above the cap. The cap is what
        # bounds the orchestrator call-count amplification the original guard feared.
        _dag_payload["sub_project_depth"] = depth
        _dag_payload["decomposition_allowed"] = _subproject_decomposition_allowed(depth)
    dag_response = orch.call(_dag_payload)
    sw.on_agent_done()

    if dag_response.get("oversize"):
        if not _subproject_decomposition_allowed(depth):
            # At the recursion cap — corrective retry, mirror the INIT guard pattern.
            console.print(
                "  [yellow]Sub-project tried to decompose at DAG stage at the recursion cap "
                f"(depth {depth} ≥ MAX_FORMAT5_DEPTH {MAX_FORMAT5_DEPTH}) — rejecting and "
                "requesting a flat FORMAT 2 task list.[/yellow]"
            )
            sw.on_agent_call("orchestrator", _ORCH_DISPLAY, "SPEC_ACCEPTED")
            dag_response = orch.call({
                **_dag_payload,
                "decomposition_rejected": (
                    f"You are a sub-project at depth {depth}, the recursion cap "
                    f"(MAX_FORMAT5_DEPTH={MAX_FORMAT5_DEPTH}). FORMAT 5 is FORBIDDEN here. Emit a "
                    "flat FORMAT 2 task list (≤50 tasks) that implements this scene/segment "
                    "directly. Trim scope to fit if needed — do NOT emit oversize/FORMAT 5."
                ),
            })
            sw.on_agent_done()
            if dag_response.get("oversize"):
                console.print("  [red]Sub-project still demands decomposition at DAG stage at the cap — failing honestly.[/red]")
                return False
        else:
            return _handle_oversize(dag_response, output_dir, depth, auto_accept=auto_accept, manual=manual,
                                    intent=intent,
                                    parent_stack=(tech_spec or {}).get("confirmed_stack", ""))

    instance = ProjectInstance(output_dir)
    instance.spec = spec
    sw.on_dag_loaded(dag_response["tasks"])
    instance.load_tasks(dag_response["tasks"])

    phase["current"] = "execution"
    console.print(f"\n[bold]Executing {len(instance.tasks)} task(s)…[/bold]")
    Scheduler(instance, orch).run()
    (output_dir / "tasks_done.json").write_text(
        _json.dumps(instance.tasks_as_list(), indent=2), encoding="utf-8"
    )

    console.print(f"\n[bold green]Project output written to: {output_dir}[/bold green]")

    # Auto-generate Playwright E2E tests for web ecosystems (once, before healing).
    ecosystem = detect_ecosystem(output_dir)
    if ecosystem in ("vanilla", "react-vite", "phaser", "three-js"):
        try:
            generate_e2e_tests(output_dir, instance.spec, instance.tasks_as_list(), ecosystem)
        except Exception as _e2e_exc:
            console.print(f"  [yellow]E2E test generation skipped ({_e2e_exc})[/yellow]")

    def _run_dynamic_checks() -> tuple[bool, list[str]]:
        """Run E2E + project-level Playwright checks and record them.

        Returns (all_passed, issues). Only genuine assertion / JS-error failures
        count as failures; an unavailable runner returns passed=True (skip) and
        does not block. Issues are phrased so the orchestrator can act on them.
        """
        ok = True
        issues: list[str] = []
        if ecosystem in ("vanilla", "react-vite", "phaser", "three-js"):
            e2e_passed, e2e_log = run_e2e_tests(output_dir)
            sw.on_verification_result("e2e", "playwright", ecosystem, e2e_passed, e2e_log)
            if not e2e_passed:
                ok = False
                detail = next((ln for ln in reversed(e2e_log.splitlines()) if ln.strip()), "see log")
                issues.append(f"E2E Playwright tests failed: {detail[:200]}")
        # Project-level Playwright check for phaser/vanilla — runs regardless of
        # task verification settings (which are always "none" for these stacks).
        if ecosystem in ("phaser", "three-js", "unknown") and (output_dir / "index.html").exists():
            passed_pw, log_pw = run_playwright_project_check(output_dir)
            sw.on_verification_result("project", "playwright", ecosystem, passed_pw, log_pw)
            if not passed_pw:
                ok = False
                detail = next((ln for ln in log_pw.splitlines() if ln.strip()), "see log")
                issues.append(f"Playwright project check failed: {detail[:200]}")
        comp_ok, comp_issues = check_completeness(project_dir=output_dir, ecosystem=ecosystem)
        sw.on_verification_result("project", "completeness", ecosystem, comp_ok, "\n".join(comp_issues))
        if not comp_ok:
            ok = False
            issues.extend(f"Completeness: {i}" for i in comp_issues)
        # Film/video-editor: the rendered video IS the deliverable. Require one
        # to exist (rendering it on demand) even if every task was mistyped with
        # verification "none" — otherwise a film build can go green frameless.
        if spec_stack(instance.spec) in ("film", "video-editor"):
            from verification import (_ensure_rendered, _run_ffprobe_check, _find_project_videos,
                                      _probe_duration, expected_film_duration)
            rendered, render_log = _ensure_rendered(output_dir)
            videos = _find_project_videos(output_dir, min_bytes=1024)
            if not videos:
                ok = False
                sw.on_verification_result("project", "film_render", ecosystem, False, render_log)
                issues.append(f"Film project produced no rendered video output: {render_log[:300]}")
            else:
                probe_ok, probe_log = _run_ffprobe_check(videos[0])
                sw.on_verification_result("project", "film_render", ecosystem, probe_ok, probe_log)
                if not probe_ok:
                    ok = False
                    issues.append(f"Rendered video failed ffprobe: {probe_log[:300]}")
                else:
                    # Duration honesty: ffprobe passes any >0.05s clip, so a
                    # 1-second render of a 20-second scene would sail through
                    # (observed live). Fail when the render is under half the
                    # spec/shotlist expectation.
                    expected = expected_film_duration(output_dir, instance.spec)
                    actual = _probe_duration(videos[0])
                    if expected and actual is not None and actual < 0.5 * expected:
                        ok = False
                        msg = (f"Rendered video is {actual:.1f}s but the spec/shotlist "
                               f"expects ~{expected:.0f}s — the render is incomplete")
                        sw.on_verification_result("project", "film_duration", ecosystem, False, msg)
                        issues.append(msg)
        return ok, issues

    if not manual:
        from heal_metrics import issue_set_similarity, classify_trend

        _MAX_HEAL = HEAL_MAX_CYCLES
        passed = False
        heal_cycle = 0
        prev_issues: list[str] | None = None   # issue set from the previous cycle
        escalated = False                        # have we already escalated the fix round?
        for heal_cycle in range(_MAX_HEAL + 1):
            review_passed = run_final_review(output_dir, instance.spec)
            sw.on_final_review_result(review_passed, heal_cycle=heal_cycle)
            dynamic_passed, dynamic_issues = _run_dynamic_checks()
            # Honest disposition (#6): a task that failed verification and exhausted its
            # retries must FAIL the build. Previously the verdict was only
            # `review_passed and dynamic_passed` — task statuses were never consulted, so a
            # broken build (failed/stalled task) could still report PASS. Fold failed tasks
            # into the verdict AND surface them as heal issues so the loop attempts a fix.
            failed_tasks = instance.failed_tasks()
            if failed_tasks:
                dynamic_issues = list(dynamic_issues) + [
                    f"Task {t.id} failed verification and exhausted retries: "
                    f"{(t.error_log or '').strip()[:200]}"
                    for t in failed_tasks
                ]
            # H2: include tasks the scheduler could never run (deadlock leaves them
            # `pending`, not `failed`) in the verdict — a stalled build must not PASS.
            all_done = instance.all_tasks_done()
            if not all_done:
                pending = [t for t in instance.tasks.values()
                           if t.status not in ("done", "deprecated", "failed")]
                dynamic_issues = list(dynamic_issues) + [
                    f"Task {t.id} never completed (status={t.status}) — scheduler "
                    "could not run it (likely unsatisfied dependency / deadlock)"
                    for t in pending
                ]
            sw.on_dynamic_checks(dynamic_passed, dynamic_issues)
            passed = _build_disposition(review_passed, dynamic_passed, failed_tasks, all_done)
            if passed or heal_cycle == _MAX_HEAL:
                break

            issues = parse_review_issues(output_dir / "REVIEW.md")
            issues.extend(dynamic_issues)
            if not issues:
                console.print("  [yellow]No parseable issues in REVIEW.md — stopping heal loop.[/yellow]")
                break

            console.print(
                f"\n[yellow]Review/E2E flagged {len(issues)} issue(s) — requesting fix tasks "
                f"(heal cycle {heal_cycle + 1}/{_MAX_HEAL})…[/yellow]"
            )
            for i, issue in enumerate(issues, 1):
                console.print(f"  {i}. {issue}")

            # ── Convergence / oscillation detection ───────────────────────────
            # If this cycle's issues aren't shrinking vs the last (same issues recur,
            # or the count grew), the heal budget is being burned counter-productively.
            # First such signal → escalate the fix round (stronger rung + sharper
            # guidance); a second consecutive signal → stop early rather than regress.
            convergence_hint: str | None = None
            if prev_issues is not None:
                trend = classify_trend(prev_issues, issues)
                sim = issue_set_similarity(prev_issues, issues)
                if trend in ("regressing", "stalled"):
                    detail = f"count {len(prev_issues)}→{len(issues)}, issue-overlap {sim:.0%}"
                    if escalated:
                        console.print(
                            f"  [bold red]Heal loop not converging ({trend}: {detail}) "
                            f"after escalation — stopping early to avoid regression.[/bold red]"
                        )
                        break
                    console.print(
                        f"  [bold yellow]Heal loop {trend} ({detail}) — escalating the fix "
                        f"round (stronger attempt, sharper guidance).[/bold yellow]"
                    )
                    escalated = True
                    convergence_hint = (
                        f"PREVIOUS FIX ROUND DID NOT CONVERGE ({trend}; issue overlap {sim:.0%}). "
                        f"Do NOT reintroduce removed/disallowed frameworks or rename established "
                        f"classes. Address the ROOT CAUSE of the recurring issues directly and minimally."
                    )

            # Difficulty re-plan bump (Phase 4): on REVIEW_FAILED, escalate the orchestrator
            # one difficulty tier so the fix-planning call uses a stronger model. This ensures
            # that a heal loop driven by complex issues doesn't stay on the cheap Haiku/Codex
            # rung that caused the first failure. Bump once; subsequent cycles keep the bumped
            # difficulty (it can only go up, never down). manual mode bypasses this.
            _bumped_diff = _bump_difficulty(_difficulty)
            if _bumped_diff != _difficulty:
                _difficulty = _bumped_diff
                console.print(
                    f"  [dim]Bumping orchestrator difficulty to '{_difficulty}' for REVIEW_FAILED re-plan.[/dim]"
                )
                orch = make_orchestrator(manual=manual, difficulty=_difficulty)

            sw.on_agent_call("orchestrator", ORCHESTRATOR_MODEL, "REVIEW_FAILED")
            sw.on_review_failed(len(issues), heal_cycle + 1)
            _fix_payload = {
                "system_state": "REVIEW_FAILED",
                "accepted_spec": instance.spec,
                "completed_tasks": instance.tasks_slim_list(),
                "review_issues": issues,
            }
            if convergence_hint:
                _fix_payload["convergence_hint"] = convergence_hint
            fix_resp = orch.call(_fix_payload)
            sw.on_agent_done()

            followups = fix_resp.get("followup_tasks", [])
            if not followups:
                console.print("  [yellow]Orchestrator returned no fix tasks — stopping.[/yellow]")
                break

            instance.apply_format4_followups(followups)
            # When escalating, pre-set retry_count on the injected followups so the scheduler
            # routes them to a stronger rung (routed_rung = base + retry_count) rather than a
            # fresh first attempt.
            if escalated:
                for _d in followups:
                    _t = instance.tasks.get(_d.get("id"))
                    if _t is not None:
                        _t.retry_count = max(_t.retry_count, 1)
            sw.on_tasks_added(followups)
            console.print(f"  Added {len(followups)} fix task(s). Re-running…\n")
            prev_issues = issues
            Scheduler(instance, orch).run()

        handoff_path = write_handoff(output_dir, instance.spec, passed, heal_cycle)
        try_claude_stamp(handoff_path, output_dir)
        git_commit_project(output_dir, instance.spec)
        deploy_url, deploy_note = deploy_project(output_dir, instance.spec)
        append_deploy_section(handoff_path, deploy_url, deploy_note)
        sw.on_deploy(deploy_url, deploy_note)
        _cost = cost_summary()
        sw.on_cost(_cost)
        sw.on_project_done(
            "pass" if passed else "needs_followup",
            "Final review and dynamic checks passed" if passed else "Review or dynamic checks need follow-up",
        )
        console.print(f"  [cyan]{format_cost_line()}[/cyan]")
        # Sub-projects (depth > 0) stay quiet — the FORMAT 5 parent sends one
        # aggregate push instead of one per scene.
        if depth == 0:
            notify_build_outcome(
                project=instance.spec.get("goal", intent)[:120],
                passed=passed,
                heal_cycles=heal_cycle,
                max_heal=_MAX_HEAL,
                handoff_path=handoff_path,
                cost_line=format_cost_line(),
                stamp_issues=_handoff_has_stamp_issues(handoff_path),
                deploy_url=deploy_url,
            )

        return passed
    else:
        # Manual mode: surface dynamic checks for the operator (no automated gating
        # ACTIONS — no heal loop, no handoff/deploy), but report an HONEST verdict.
        # H1 — the manual branch used to hard-return True, so a --manual run with a
        # failed-and-exhausted task reported PASS, re-opening the exact false-PASS hole
        # commit 760ec40 closed on the automated path. Compute the verdict from the same
        # inputs the automated branch feeds _build_disposition(): the final review, the
        # dynamic checks, and any task that failed verification and exhausted its retries.
        review_passed = run_final_review(output_dir, instance.spec)
        sw.on_final_review_result(review_passed, heal_cycle=0)
        dynamic_passed, dynamic_issues = _run_dynamic_checks()
        failed_tasks = instance.failed_tasks()
        if failed_tasks:
            dynamic_issues = list(dynamic_issues) + [
                f"Task {t.id} failed verification and exhausted retries: "
                f"{(t.error_log or '').strip()[:200]}"
                for t in failed_tasks
            ]
        # H2: a deadlock leaves tasks pending; fold not-done into the manual verdict too.
        all_done = instance.all_tasks_done()
        sw.on_dynamic_checks(dynamic_passed, dynamic_issues)
        passed = _build_disposition(review_passed, dynamic_passed, failed_tasks, all_done)
        sw.on_project_done(
            "pass" if passed else "needs_followup",
            "Manual run complete",
        )
        return passed


def _sub_project_stack(sp_dir: Path) -> str:
    """Stack of a completed sub-project, read from the spec.json its run wrote."""
    try:
        import json as _js
        spec = _js.loads((sp_dir / "spec.json").read_text(encoding="utf-8"))
        return spec_stack(spec)
    except Exception:
        return ""


def _best_scene_clip(sp_dir: Path) -> Path | None:
    """The clip to feed final assembly: prefer an edited/final clip, else the
    largest video (the edited cut carries the audio layer)."""
    from verification import _find_project_videos
    videos = _find_project_videos(sp_dir, min_bytes=1024)
    if not videos:
        return None
    return sorted(
        videos,
        key=lambda p: (not any(k in p.stem.lower() for k in ("final", "edit")),
                       -p.stat().st_size),
    )[0]


def _handle_oversize(response: dict, base_dir: Path, depth: int, auto_accept: bool = False,
                     manual: bool = False, intent: str = "", parent_stack: str = "") -> bool:
    console.print(
        f"\n[bold yellow]Oversize project — decomposing into sub-projects.[/bold yellow]\n"
        f"  Reason: {response['reason']}"
    )

    sub_projects = response["sub_projects"]
    graph = {sp["name"]: set(sp.get("depends_on", [])) for sp in sub_projects}
    wiring: dict = {}  # accumulated from completed sub-projects, forwarded to dependents
    results: dict[str, str] = {}  # name → "passed" | "failed" | "skipped"
    # Known from the parent's tech spec before any sub-project runs; confirmed
    # per sub-project below (their specs may refine the stack).
    film_decomposition = parent_stack in ("film", "video-editor")
    # C1: the cost accumulator is build-GLOBAL (reset only at depth 0), so it already
    # holds the CUMULATIVE spend across every sub-project. Read it ONCE after the loop
    # for the aggregate — summing cost_summary() per sub-project would double-count,
    # because each read already includes all prior sub-projects' spend (triangular sum).

    for name in TopologicalSorter(graph).static_order():
        sp = next(s for s in sub_projects if s["name"] == name)
        sp_dir = base_dir / name

        # Film decompositions: the parent assembles the final film itself (below),
        # so an orchestrator-emitted assembly sub-project is skipped — isolated in
        # its own directory it cannot reach the sibling scene clips and would fail.
        # Detect by name, by goal text, or by shape (depends on every other sub-project
        # — a scene chain only ever depends on the previous scene).
        # Observed live: one named 'orchestration'. NOTE: "orchestrat" is intentionally
        # absent from the goal check — Gemini uses "orchestrate" as a synonym for
        # "direct/coordinate" in scene goals, which caused all scenes to be falsely
        # skipped as assembly sub-projects (observed 2026-06-16).
        other_names = {s["name"] for s in sub_projects if s["name"] != name}
        looks_like_assembly = bool(
            re.search(r"assembl|concat|orchestrat|full_film|final_film|final_cut|final_movie",
                      name, re.IGNORECASE)
            or re.search(r"assembl", sp.get("goal", ""), re.IGNORECASE)
            or (len(other_names) >= 2 and set(sp.get("depends_on", [])) >= other_names)
        )
        if film_decomposition and looks_like_assembly:
            console.print(f"  [dim]⊘ {name} skipped — parent performs final assembly[/dim]")
            results[name] = "skipped"
            continue

        sp_dir.mkdir(parents=True, exist_ok=True)
        console.rule(f"[cyan]Sub-project: {name}[/cyan]")
        try:
            ok = run_project(sp["goal"], sp_dir, depth + 1, manual=manual,
                             auto_accept=auto_accept, wiring=wiring)
        except BuildCostCeilingExceeded:
            # C1: the cost ceiling is build-GLOBAL. A tripped ceiling must halt the
            # WHOLE decomposition, not just this scene — re-raise so it propagates out
            # of the sub-project loop to run_project()'s failure-handoff handler instead
            # of being swallowed by the broad `except Exception` below (which would
            # continue the loop with a still-tripped — and now sticky — budget, spending
            # nothing more useful but masking the honest "build halted on cost" verdict).
            console.print(
                f"  [bold red]Sub-project {name} hit the per-build cost ceiling — "
                "halting the entire build.[/bold red]"
            )
            raise
        except Exception as exc:  # noqa: BLE001 — one crashed scene must not sink the rest
            console.print(f"  [red]Sub-project {name} crashed: {exc} — continuing with remaining sub-projects.[/red]")
            ok = False
        results[name] = "passed" if ok else "failed"
        if _sub_project_stack(sp_dir) in ("film", "video-editor"):
            film_decomposition = True

        # Carry wiring.json from this sub-project forward to all later sub-projects
        wiring_path = sp_dir / "wiring.json"
        if wiring_path.exists():
            import json as _wj
            try:
                wiring.update(_wj.loads(wiring_path.read_text(encoding="utf-8")))
                console.print(f"  [dim]Wiring from {name}: {list(wiring.keys())}[/dim]")
            except Exception:
                pass

    all_passed = all(v != "failed" for v in results.values()) and any(
        v == "passed" for v in results.values()
    )

    # ── Film final assembly: concatenate scene clips into final.mp4 ──────────
    final_video: Path | None = None
    assembly_note = ""
    if film_decomposition:
        if all_passed:
            clips = []
            for name, verdict in results.items():
                if verdict != "passed":
                    continue
                clip = _best_scene_clip(base_dir / name)
                if clip is not None:
                    clips.append(clip)
            if clips:
                from video_worker import assemble_film
                from verification import _run_ffprobe_check, _run_frame_integrity_check
                out = base_dir / "final.mp4"
                asm_ok, asm_log = assemble_film(clips, out)
                if asm_ok:
                    probe_ok, probe_log = _run_ffprobe_check(out)
                    frame_ok, frame_log = _run_frame_integrity_check(out)
                    if probe_ok and frame_ok:
                        final_video = out
                        assembly_note = f"{asm_log}; {probe_log}"
                    else:
                        all_passed = False
                        assembly_note = f"assembled file failed probing: {probe_log}; {frame_log}"
                else:
                    all_passed = False
                    assembly_note = asm_log
            else:
                all_passed = False
                assembly_note = "no scene clips found to assemble"
        else:
            assembly_note = "final assembly skipped — one or more scene sub-projects failed"
    elif any(v == "skipped" for v in results.values()):
        assembly_note = "assembly skipped — not a film decomposition"

    handoff_path = write_parent_handoff(base_dir, intent or response.get("reason", ""),
                                        results, final_video, assembly_note)

    # Aggregate spend for the whole decomposition: the build-global cost accumulator
    # already holds the cumulative total across every sub-project — read it once.
    _agg = cost_summary()
    total_usd = _agg.get("total_usd", 0.0)
    total_calls = _agg.get("paid_calls", 0)

    # One aggregate push for the whole decomposition (sub-projects stay quiet).
    if depth == 0:
        sw.on_cost({
            "total_usd": total_usd,
            "paid_calls": total_calls,
            "by_model": {},
            "tokens": {},
        })
        sw.on_project_done(
            "pass" if all_passed else "needs_followup",
            "FORMAT 5 aggregate complete" if all_passed else "One or more sub-projects need follow-up",
        )
        notify_build_outcome(
            project=(intent or response.get("reason", ""))[:120],
            passed=all_passed,
            heal_cycles=0,
            max_heal=HEAL_MAX_CYCLES,
            handoff_path=handoff_path,
            cost_line=f"est. cost ${total_usd:.2f} over {total_calls} paid call(s), all sub-projects",
        )

    return all_passed


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
            cont_ok = run_continuation(intent, cont_dir, auto_accept=args.yes)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
            sw.on_project_canceled("Continuation interrupted by user")
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001
            sw.on_project_failed(f"{type(exc).__name__}: {exc}", "continuation")
            notify_crash(project=intent[:120], error=f"{type(exc).__name__}: {exc}",
                         output_dir=cont_dir)
            raise
        if not cont_ok:
            sys.exit(1)
        return

    if args.output:
        output_dir = Path(args.output)
    else:
        slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in intent[:50]).strip("_-")
        output_dir = PROJECTS_DIR / slug

    output_dir.mkdir(parents=True, exist_ok=True)

    for attempt in range(PIPELINE_MAX_RETRIES + 1):
        try:
            ok = run_project(intent, output_dir, manual=args.manual, auto_accept=args.yes)
            if not ok:
                # The build completed but failed review/checks — an honest
                # verdict, not a crash. Don't retry; exit non-zero.
                sys.exit(1)
            break  # success
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
            sw.on_project_canceled("Pipeline interrupted by user")
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001
            if attempt < PIPELINE_MAX_RETRIES:
                console.print(
                    f"\n[bold yellow]Pipeline failed (attempt {attempt + 1}/{PIPELINE_MAX_RETRIES + 1}): "
                    f"{exc}[/bold yellow]\n[yellow]Retrying in 5s…[/yellow]"
                )
                time.sleep(5)
            else:
                console.print(
                    f"\n[bold red]Pipeline failed after {PIPELINE_MAX_RETRIES + 1} attempt(s): {exc}[/bold red]"
                )
                sw.on_project_failed(f"{type(exc).__name__}: {exc}", "pipeline")
                notify_crash(project=intent[:120], error=f"{type(exc).__name__}: {exc}",
                             output_dir=output_dir)
                raise


if __name__ == "__main__":
    main()
