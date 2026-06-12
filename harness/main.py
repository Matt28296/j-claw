#!/usr/bin/env python3
"""J-Claw engineering harness — entry point."""
from __future__ import annotations
import sys
import json
import re
import shutil
import argparse
import time
from pathlib import Path
from graphlib import TopologicalSorter

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

from config import (
    PROJECTS_DIR, MAX_FORMAT5_DEPTH, ORCHESTRATOR_PROVIDER, ORCHESTRATOR_MODEL,
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
from orchestrator import Orchestrator, ManualOrchestrator, OpenRouterOrchestrator, GeminiOrchestrator
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
from cost import reset_costs, cost_summary, format_cost_line
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
    """True when the independent OpenClaw stamp appended an ISSUES FOUND verdict —
    a PASS build can still carry caveats the heal loop never resolved."""
    try:
        return "OPENCLAW: ISSUES FOUND" in handoff_path.read_text(encoding="utf-8")
    except Exception:
        return False


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

    if ORCHESTRATOR_PROVIDER == "openrouter":
        orch = OpenRouterOrchestrator()
    elif ORCHESTRATOR_PROVIDER == "gemini":
        orch = GeminiOrchestrator()
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
        "creative_brief": creative_brief,
    })
    sw.on_agent_done()

    if not dag_response.get("tasks"):
        console.print("[yellow]Orchestrator returned no new tasks.[/yellow]")
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

    passed = run_final_review(project_dir, spec)
    heal_cycle = 0
    handoff_path = write_handoff(project_dir, spec, passed, heal_cycle)
    try_claude_stamp(handoff_path, project_dir)
    git_commit_project(project_dir, {"goal": f"continuation: {new_intent}"})
    deploy_url, deploy_note = deploy_project(project_dir, spec)
    append_deploy_section(handoff_path, deploy_url, deploy_note)
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
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Reset the per-project paid (cloud) worker-call budget for this run.
    from worker import reset_paid_budget
    reset_paid_budget()
    reset_costs()

    # Mutable holder so the failure handoff below can report the phase the
    # pipeline actually crashed in (updated in-place by _run_project_inner).
    phase = {"current": "pipeline"}
    try:
        return _run_project_inner(intent, output_dir, depth, manual, auto_accept, wiring, phase)
    except Exception as exc:
        _write_failure_handoff(output_dir, intent, phase["current"], exc)
        raise


def _run_project_inner(intent: str, output_dir: Path, depth: int, manual: bool, auto_accept: bool, wiring: dict | None, phase: dict) -> bool:
    """Inner pipeline body — separated so run_project() can catch + report failures."""

    _start_dashboard()

    if manual:
        orch = ManualOrchestrator()
    elif ORCHESTRATOR_PROVIDER == "openrouter":
        orch = OpenRouterOrchestrator()
    elif ORCHESTRATOR_PROVIDER == "gemini":
        orch = GeminiOrchestrator()
    else:
        orch = Orchestrator()

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
        # Already a sub-project: one scene/segment of a FORMAT 5 decomposition.
        # Recursive decomposition multiplies cost without producing anything
        # (observed live: scene → scripts → … until the depth cap).
        init_payload["sub_project_depth"] = depth
        init_payload["decomposition_allowed"] = False
    spec = orch.call(init_payload)
    sw.on_agent_done()

    if spec.get("oversize"):
        if depth:
            # Runtime enforcement — never trust the prompt alone. One corrective
            # retry, then an honest failure instead of a recursion spiral.
            console.print(
                "  [yellow]Sub-project tried to decompose again (FORMAT 5 inside FORMAT 5) "
                "— rejecting and requesting a flat FORMAT 1 spec.[/yellow]"
            )
            sw.on_agent_call("orchestrator", _ORCH_DISPLAY, "INIT")
            spec = orch.call({
                **init_payload,
                "decomposition_rejected": (
                    f"You are already a sub-project at depth {depth} of a FORMAT 5 "
                    "decomposition. Further decomposition is FORBIDDEN. Emit a flat "
                    "FORMAT 1 spec (≤50 tasks) that builds this one scene/segment "
                    "directly. Trim scope to fit if needed."
                ),
            })
            sw.on_agent_done()
            if spec.get("oversize"):
                console.print("  [red]Sub-project still demands decomposition — failing honestly.[/red]")
                return False
        else:
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
            if depth:
                console.print("  [red]Sub-project revision demands decomposition — failing honestly.[/red]")
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
    dag_response = orch.call(_dag_payload)
    sw.on_agent_done()

    if dag_response.get("oversize"):
        _handle_oversize(dag_response, output_dir, depth, auto_accept=auto_accept, manual=manual)
        return

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
            dynamic_passed, dynamic_issues = _run_dynamic_checks()
            passed = review_passed and dynamic_passed
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

            sw.on_agent_call("orchestrator", ORCHESTRATOR_MODEL, "REVIEW_FAILED")
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
        _cost = cost_summary()
        sw.on_cost(_cost)
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
        # Manual mode: surface dynamic checks for the operator (no automated gating).
        _run_dynamic_checks()
        return True


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
    # Each sub-project's run_project() resets the global cost accumulator, so
    # the aggregate cost must be summed here as each one finishes.
    total_usd = 0.0
    total_calls = 0

    for name in TopologicalSorter(graph).static_order():
        sp = next(s for s in sub_projects if s["name"] == name)
        sp_dir = base_dir / name

        # Film decompositions: the parent assembles the final film itself (below),
        # so an orchestrator-emitted assembly sub-project is skipped — isolated in
        # its own directory it cannot reach the sibling scene clips and would fail.
        # Detect by name, by goal text (conservative stems only — a scene goal may
        # legitimately mention 'concat' for its internal xfade), or by shape
        # (depends on every other sub-project — a scene chain only ever depends
        # on the previous scene). Observed live: one named 'orchestration'.
        other_names = {s["name"] for s in sub_projects if s["name"] != name}
        looks_like_assembly = bool(
            re.search(r"assembl|concat|orchestrat|full_film|final_film|final_cut|final_movie",
                      name, re.IGNORECASE)
            or re.search(r"assembl|orchestrat", sp.get("goal", ""), re.IGNORECASE)
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
        except Exception as exc:  # noqa: BLE001 — one crashed scene must not sink the rest
            console.print(f"  [red]Sub-project {name} crashed: {exc} — continuing with remaining sub-projects.[/red]")
            ok = False
        results[name] = "passed" if ok else "failed"
        _sub_cost = cost_summary()
        total_usd += _sub_cost.get("total_usd", 0.0)
        total_calls += _sub_cost.get("paid_calls", 0)
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

    # One aggregate push for the whole decomposition (sub-projects stay quiet).
    if depth == 0:
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
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001
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
                notify_crash(project=intent[:120], error=f"{type(exc).__name__}: {exc}",
                             output_dir=output_dir)
                raise


if __name__ == "__main__":
    main()
