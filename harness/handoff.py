"""Write HANDOFF.md and invoke the claude CLI or Anthropic API for a final verdict."""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import anthropic
from rich.console import Console

from config import ANTHROPIC_API_KEY, ORCHESTRATOR_MODEL, HEAL_MAX_CYCLES, PAID_ORCH_ENABLED
from cost import record_usage, check_cost_ceiling
from verification import SKIP_PREFIX
import config as cfg
from state_writer import writer as sw

_STATE_FILE = Path(__file__).parent.parent / "mission_control.json"
_MAX_HEAL = HEAL_MAX_CYCLES

console = Console()


def _run_ipfs_pin(project_dir: Path) -> str | None:
    """Run scripts/pin-to-ipfs.js in project_dir and return the CID if found, else None."""
    if not cfg.IPFS_AUTO_PIN:
        return None
    if not cfg.PINATA_API_KEY:
        return None
    pin_script = project_dir / "scripts" / "pin-to-ipfs.js"
    if not pin_script.exists():
        return None
    try:
        result = subprocess.run(
            ["node", "scripts/pin-to-ipfs.js"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PINATA_API_KEY": cfg.PINATA_API_KEY, "PINATA_SECRET_KEY": cfg.PINATA_SECRET_KEY},
        )
        match = re.search(r'(Qm[a-zA-Z0-9]{44}|bafy[a-zA-Z0-9]+)', result.stdout)
        if match:
            return match.group(1)
    except Exception:  # noqa: BLE001
        pass
    return None


def write_handoff(output_dir: Path, spec: dict, passed: bool, heal_cycles: int) -> Path:
    """Write HANDOFF.md to output_dir and return its path."""
    goal       = spec.get("goal", spec.get("description", "Unknown"))
    stack      = spec.get("architecture", {}).get("stack", "unknown")
    complexity = spec.get("complexity", "unknown")

    status_line = "✓ PASS — ready for final review" if passed else "✗ ISSUES REMAIN — needs attention"

    # Pull test results from mission_control.json. A check that returned True only
    # because its tool/runner was unavailable is a SKIP, not a real PASS — mark those
    # distinctly (⊘) so a green report isn't silently hollow.
    test_lines: list[str] = []
    skipped_count = 0
    try:
        mc = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        for t in mc.get("test_results", []):
            log = t.get("log", "")
            if t["passed"] and log.startswith(SKIP_PREFIX):
                skipped_count += 1
                test_lines.append(
                    f"- ⊘ [{t['method']}/{t['ecosystem']}] {t['task_id']} — {log}"
                )
            else:
                icon = "✓" if t["passed"] else "✗"
                test_lines.append(
                    f"- {icon} [{t['method']}/{t['ecosystem']}] {t['task_id']}"
                )
        if skipped_count:
            test_lines.append(
                f"\n> ⚠ {skipped_count} check(s) were SKIPPED (tool/runner unavailable) — "
                "these are NOT verified passes."
            )
    except Exception:  # noqa: BLE001
        pass

    # Pull REVIEW.md first line (verdict)
    review_verdict = "Not run"
    review_path = output_dir / "REVIEW.md"
    if review_path.exists():
        for line in review_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("VERDICT:"):
                review_verdict = line.strip()
                break

    # Detect contract deployment info from deployment.json (web3 projects)
    deployment_note = ""
    for candidate in [
        output_dir / "frontend" / "public" / "deployment.json",
        output_dir / "deployment.json",
    ]:
        if candidate.exists():
            try:
                dep = json.loads(candidate.read_text(encoding="utf-8"))
                network = dep.get("network", "unknown network")
                address = dep.get("address", "")
                chain_id = dep.get("chainId", "")
                if address:
                    chain_str = f" (chainId {chain_id})" if chain_id else ""
                    deployment_note = (
                        f"\n## Contract Deployment\n"
                        f"Contract deployed on **{network}**{chain_str}: `{address}`\n"
                    )
            except Exception:  # noqa: BLE001
                pass
            break

    content = f"""# J-Claw Handoff Report

**Status:** {status_line}
**Heal cycles used:** {heal_cycles} / {_MAX_HEAL}
**Project:** {goal}
**Stack:** {stack} · {complexity}
**Output directory:** {output_dir.resolve()}

## Final Review Verdict
{review_verdict}
{deployment_note}
## Test Results
{chr(10).join(test_lines) if test_lines else "No automated tests recorded."}

## For Claude Code
The project files are in: `{output_dir.resolve()}`

Read `REVIEW.md` for the full automated review. If issues remain, fix them directly in the
output directory. The project goal is:

> {goal}
"""
    handoff_path = output_dir / "HANDOFF.md"
    handoff_path.write_text(content, encoding="utf-8")
    console.print(f"\n  Handoff report written to: [dim]{handoff_path}[/dim]")

    cid = _run_ipfs_pin(output_dir)
    if cid:
        with open(handoff_path, "a", encoding="utf-8") as f:
            f.write(f"\n## IPFS\n- CID: `{cid}`\n- Gateway: https://gateway.pinata.cloud/ipfs/{cid}\n")
        console.print(f"  [dim]IPFS CID pinned: {cid}[/dim]")

    return handoff_path


def try_claude_stamp(handoff_path: Path, output_dir: Path) -> None:
    """Invoke the claude CLI (preferred) or Anthropic API (fallback) for a final verdict."""
    cli_ok = shutil.which("claude") and _stamp_via_cli(handoff_path, output_dir)
    if cli_ok:
        return
    # Metered API stamp is gated by PAID_ORCH_ENABLED — on a $0-credit box (knob false) the stamp
    # is a non-critical QA nicety, so skip it rather than issue a metered call that only 400s.
    if ANTHROPIC_API_KEY and PAID_ORCH_ENABLED:
        _stamp_via_api(handoff_path, output_dir)
    else:
        reason = ("no ANTHROPIC_API_KEY" if not ANTHROPIC_API_KEY
                  else "metered stamp disabled (PAID_ORCH_ENABLED=false)")
        console.print(
            f"\n  [dim]claude CLI not found and {reason} "
            "— skipping autonomous stamp.[/dim]\n"
            f"  To run OpenClaw manually: cd \"{output_dir}\" && claude\n"
            "  Then ask Claude to review HANDOFF.md."
        )


# ── CLI path ──────────────────────────────────────────────────────────────────

def _stamp_via_cli(handoff_path: Path, output_dir: Path) -> bool:
    """Run the claude CLI subprocess for the final verdict. Returns True if verdict was written."""
    console.print("\n[bold]Running OpenClaw final stamp (claude CLI)…[/bold]")
    prompt = (
        "You are doing a final autonomous quality check for a software project. "
        "Read HANDOFF.md first to understand what was built and what (if anything) is flagged. "
        "Then read the relevant source files. "
        "Give a concise one-paragraph verdict: does the project meet its stated goal? "
        "If there are remaining issues, list them briefly. "
        "End with either OPENCLAW: APPROVED or OPENCLAW: ISSUES FOUND."
    )
    # On Windows, .cmd wrappers require shell=True to be invokable
    use_shell = sys.platform == "win32"
    cmd: str | list = f'claude --print "{prompt}"' if use_shell else ["claude", "--print", prompt]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=output_dir,
            # Scrub the metered ANTHROPIC_API_KEY so the stamp runs on the subscription
            # OAuth, not the (possibly $0-credit) metered API. Without this the claude CLI
            # inherits the key and silently meters — or fails "credit balance too low".
            env=cfg.claude_cli_env({"PYTHONUTF8": "1"}),
            shell=use_shell,
        )
        verdict = result.stdout.strip() or result.stderr.strip()
        if not verdict:
            console.print("  [yellow]claude CLI returned no output — trying API stamp.[/yellow]")
            return False
    except subprocess.TimeoutExpired:
        console.print("  [yellow]claude CLI timed out — trying API stamp.[/yellow]")
        return False
    except FileNotFoundError:
        console.print("  [yellow]claude CLI not executable — trying API stamp.[/yellow]")
        return False

    _append_verdict(handoff_path, verdict)
    return True


# ── API path ──────────────────────────────────────────────────────────────────

def _stamp_via_api(handoff_path: Path, output_dir: Path) -> None:
    """Call the Anthropic API directly when claude CLI is not on PATH."""
    console.print("\n[bold]Running OpenClaw final stamp (Anthropic API)…[/bold]")

    context = _collect_stamp_context(handoff_path, output_dir)
    system = (
        "You are doing a final autonomous quality check on an AI-generated software project. "
        "Review the project files and handoff report provided. "
        "Give a concise one-paragraph verdict: does the project meet its stated goal? "
        "If there are remaining issues, list them briefly. "
        "End your response with exactly one of:\n"
        "  OPENCLAW: APPROVED\n"
        "  OPENCLAW: ISSUES FOUND"
    )
    # Per-build cost circuit-breaker: refuse before spending if the ceiling was
    # already crossed. Placed BEFORE the try so BuildCostCeilingExceeded (a
    # RuntimeError) is NOT masked by the broad except below — it propagates out
    # and fails the stamp closed rather than draining money on the failure path.
    check_cost_ceiling()
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=ORCHESTRATOR_MODEL,
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": context}],
        )
        record_usage(response.usage, ORCHESTRATOR_MODEL, "stamp")
        verdict = response.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]OpenClaw API stamp failed: {exc} — skipping.[/yellow]")
        return

    _append_verdict(handoff_path, verdict)


def _collect_stamp_context(handoff_path: Path, output_dir: Path) -> str:
    """Build context string: HANDOFF.md + project source files (capped at 80k chars)."""
    _SKIP_DIRS  = {"node_modules", ".venv", "__pycache__", ".git", "dist", ".playwright"}
    _EXTS       = {".js", ".py", ".html", ".css", ".ts", ".jsx", ".tsx", ".json", ".md"}
    _SKIP_FILES = {"REVIEW.md", "HANDOFF.md", "package-lock.json", "yarn.lock"}
    _MAX        = 80_000

    parts: list[str] = []
    if handoff_path.exists():
        parts.append(f"=== HANDOFF.md ===\n{handoff_path.read_text(encoding='utf-8')}\n")

    BINARY_EXTS = {".mp4", ".webm", ".mov", ".wav", ".mp3", ".flac", ".ogg",
                   ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot"}
    total = sum(len(p) for p in parts)
    for path in sorted(output_dir.rglob("*")):
        if path.is_dir() or any(d in path.parts for d in _SKIP_DIRS):
            continue
        if path.name in _SKIP_FILES or path.suffix.lower() not in _EXTS:
            continue
        if path.suffix.lower() in BINARY_EXTS:
            continue
        if path.stat().st_size > 500_000:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel   = str(path.relative_to(output_dir)).replace("\\", "/")
        chunk = f"=== {rel} ===\n{content}\n\n"
        if total + len(chunk) > _MAX:
            break
        parts.append(chunk)
        total += len(chunk)

    return "\n".join(parts)


# ── Git auto-commit ───────────────────────────────────────────────────────────

def git_commit_project(output_dir: Path, spec: dict) -> None:
    """Init a git repo in output_dir and commit all generated files."""
    if not shutil.which("git"):
        console.print("  [dim]git not found on PATH — skipping auto-commit.[/dim]")
        return

    goal = spec.get("goal", spec.get("description", "generated project"))[:72]
    msg  = f"j-claw: {goal}"

    def _run(args: list[str]) -> bool:
        r = subprocess.run(
            args, cwd=output_dir, capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "j-claw",
                 "GIT_AUTHOR_EMAIL": "jclaw@local",
                 "GIT_COMMITTER_NAME": "j-claw",
                 "GIT_COMMITTER_EMAIL": "jclaw@local"},
        )
        return r.returncode == 0

    try:
        from permissions import observe
        observe("git", detail="init/add/commit (local, no push)")  # roadmap #6: observe-only
        _run(["git", "init"])
        # Write a minimal .gitignore if none exists
        gi = output_dir / ".gitignore"
        if not gi.exists():
            gi.write_text("node_modules/\n.venv/\n__pycache__/\ndist/\n*.pyc\n", encoding="utf-8")
        _run(["git", "add", "."])
        ok = _run(["git", "commit", "-m", msg])
        if ok:
            console.print(f"  [dim]Git commit: {msg}[/dim]")
        else:
            console.print("  [dim]Git commit skipped (nothing to commit or repo already up to date).[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]Git auto-commit failed: {exc}[/yellow]")


# Stacks whose output is static web content a generic host can serve. Everything
# else (APIs, desktop apps, films) is skipped honestly rather than half-deployed.
_DEPLOYABLE_STACKS = {"vanilla", "react-vite", "phaser", "three-js"}


def deploy_project(output_dir: Path, spec: dict) -> tuple[str | None, str]:
    """Run the configured DEPLOY_HOOK command in output_dir (e.g. 'vercel --prod --yes').

    Returns (url, note): the deployed URL when one can be extracted from the
    CLI output (else None), and a human-readable outcome note for HANDOFF.md
    (skip reason / failure summary / success)."""
    from config import DEPLOY_HOOK, DEPLOY_TIMEOUT
    if not DEPLOY_HOOK:
        return None, "deploy skipped: DEPLOY_HOOK not configured"
    from config import spec_stack
    stack = spec_stack(spec)
    if stack not in _DEPLOYABLE_STACKS:
        note = f"deploy skipped: stack '{stack or 'unknown'}' is not a deployable web stack"
        console.print(f"  [dim]⊘ {note}[/dim]")
        return None, note
    console.print(f"\n[bold]Running deployment hook: {DEPLOY_HOOK}[/bold]")
    from permissions import observe
    observe("deploy_hook", detail=DEPLOY_HOOK)  # roadmap #6: classify + log (observe-only)
    use_shell = sys.platform == "win32"
    cmd = DEPLOY_HOOK if use_shell else DEPLOY_HOOK.split()
    try:
        result = subprocess.run(
            cmd,
            cwd=output_dir,
            capture_output=True,
            text=True,
            timeout=DEPLOY_TIMEOUT,
            shell=use_shell,
            env={**os.environ, "CI": "1"},
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            console.print(f"  [green]Deployment succeeded.[/green]")
            # Extract URL from common CLI output patterns
            for line in output.splitlines():
                if line.startswith("https://") or "https://" in line.lower() and ("url" in line.lower() or "deployed" in line.lower()):
                    url = line[line.index("https://"):].split()[0].strip()
                    console.print(f"  [bold cyan]URL: {url}[/bold cyan]")
                    return url, "deployed"
            return None, "deploy hook exited 0 but printed no URL"
        else:
            console.print(f"  [yellow]Deployment failed (exit {result.returncode}):[/yellow]")
            console.print(f"  [dim]{output[-1000:]}[/dim]")
            return None, f"deploy failed (exit {result.returncode}): {output[-300:]}"
    except subprocess.TimeoutExpired:
        console.print(f"  [yellow]Deployment timed out after {DEPLOY_TIMEOUT}s.[/yellow]")
        return None, f"deploy timed out after {DEPLOY_TIMEOUT}s"
    except Exception as exc:
        console.print(f"  [yellow]Deployment hook error: {exc}[/yellow]")
        return None, f"deploy hook error: {exc}"


def append_deploy_section(handoff_path: Path, url: str | None, note: str) -> None:
    """Record the deployment outcome in HANDOFF.md (## Deployment section)."""
    if url:
        body = f"- ✓ URL: {url}"
    else:
        body = f"- ⊘ {note}"
    try:
        existing = handoff_path.read_text(encoding="utf-8")
        handoff_path.write_text(existing + f"\n## Deployment\n\n{body}\n", encoding="utf-8")
    except OSError as exc:
        console.print(f"  [yellow]Could not append deploy section to HANDOFF: {exc}[/yellow]")


def write_parent_handoff(
    base_dir: Path,
    intent: str,
    results: dict[str, str],
    final_video: Path | None = None,
    assembly_note: str = "",
) -> Path:
    """Write the aggregate HANDOFF.md for a FORMAT 5 parent project.

    results maps sub-project name → "passed" | "failed" | "skipped". The parent
    verdict is PASS only when every non-skipped sub-project passed (and, for
    film decompositions, final assembly succeeded — reflected by final_video).
    """
    icons = {"passed": "✓", "failed": "✗", "skipped": "⊘"}
    all_passed = all(v != "failed" for v in results.values()) and any(
        v == "passed" for v in results.values()
    )
    status_line = (
        "✓ PASS — all sub-projects complete"
        if all_passed
        else "✗ ISSUES REMAIN — one or more sub-projects failed"
    )

    sub_lines = [
        f"- {icons.get(v, '?')} `{name}` — {v}"
        + (f" (see `{name}/HANDOFF.md`)" if v != "skipped" else "")
        for name, v in results.items()
    ]

    assembly_lines: list[str] = []
    if final_video is not None:
        assembly_lines = [f"- ✓ Final film assembled: `{final_video.name}`"]
        if assembly_note:
            assembly_lines.append(f"  - {assembly_note}")
    elif assembly_note:
        assembly_lines = [f"- ⊘ {assembly_note}"]

    content = f"""# HANDOFF — {intent[:120]}

**Status:** {status_line}
**Type:** FORMAT 5 decomposition ({len(results)} sub-project(s))

## Sub-projects

{chr(10).join(sub_lines)}
"""
    if assembly_lines:
        content += f"\n## Final Assembly\n\n{chr(10).join(assembly_lines)}\n"

    handoff_path = base_dir / "HANDOFF.md"
    handoff_path.write_text(content, encoding="utf-8")
    console.print(f"\n[bold]Parent handoff written:[/bold] [dim]{handoff_path}[/dim]")
    return handoff_path


# ── Shared ────────────────────────────────────────────────────────────────────

def _append_verdict(handoff_path: Path, verdict: str) -> None:
    """Print the verdict summary and append it to HANDOFF.md."""
    approved = "OPENCLAW: APPROVED" in verdict
    color    = "green" if approved else "yellow"
    summary  = verdict.splitlines()[-1] if verdict else "No verdict"
    console.print(f"\n  OpenClaw: [bold {color}]{summary}[/bold {color}]")

    existing = handoff_path.read_text(encoding="utf-8")
    handoff_path.write_text(
        existing + f"\n## Claude Code Verdict\n\n{verdict}\n",
        encoding="utf-8",
    )
    console.print(f"  Verdict appended to: [dim]{handoff_path}[/dim]")
    sw.on_openclaw_stamp(verdict)
