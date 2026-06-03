"""Write HANDOFF.md and invoke the claude CLI or Anthropic API for a final verdict."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import anthropic
from rich.console import Console

from config import ANTHROPIC_API_KEY, ORCHESTRATOR_MODEL

_STATE_FILE = Path(__file__).parent.parent / "mission_control.json"
_MAX_HEAL = 2

console = Console()


def write_handoff(output_dir: Path, spec: dict, passed: bool, heal_cycles: int) -> Path:
    """Write HANDOFF.md to output_dir and return its path."""
    goal       = spec.get("goal", spec.get("description", "Unknown"))
    stack      = spec.get("architecture", {}).get("stack", "unknown")
    complexity = spec.get("complexity", "unknown")

    status_line = "✓ PASS — ready for final review" if passed else "✗ ISSUES REMAIN — needs attention"

    # Pull test results from mission_control.json
    test_lines: list[str] = []
    try:
        mc = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        for t in mc.get("test_results", []):
            icon = "✓" if t["passed"] else "✗"
            test_lines.append(
                f"- {icon} [{t['method']}/{t['ecosystem']}] {t['task_id']}"
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

    content = f"""# J-Claw Handoff Report

**Status:** {status_line}
**Heal cycles used:** {heal_cycles} / {_MAX_HEAL}
**Project:** {goal}
**Stack:** {stack} · {complexity}
**Output directory:** {output_dir.resolve()}

## Final Review Verdict
{review_verdict}

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
    return handoff_path


def try_claude_stamp(handoff_path: Path, output_dir: Path) -> None:
    """Invoke the claude CLI (preferred) or Anthropic API (fallback) for a final verdict."""
    cli_ok = shutil.which("claude") and _stamp_via_cli(handoff_path, output_dir)
    if cli_ok:
        return
    if ANTHROPIC_API_KEY:
        _stamp_via_api(handoff_path, output_dir)
    else:
        console.print(
            "\n  [dim]claude CLI not found and no ANTHROPIC_API_KEY "
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
            env={**os.environ, "PYTHONUTF8": "1"},
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
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=ORCHESTRATOR_MODEL,
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": context}],
        )
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

    total = sum(len(p) for p in parts)
    for path in sorted(output_dir.rglob("*")):
        if path.is_dir() or any(d in path.parts for d in _SKIP_DIRS):
            continue
        if path.name in _SKIP_FILES or path.suffix.lower() not in _EXTS:
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
