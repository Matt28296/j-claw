"""Write HANDOFF.md and optionally invoke the claude CLI for a final verdict."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from pathlib import Path

from rich.console import Console

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
    """Invoke the claude CLI for a final autonomous verdict, if available."""
    if not shutil.which("claude"):
        console.print(
            "\n  [dim]claude CLI not found on PATH — skipping autonomous stamp.[/dim]\n"
            f"  To run OpenClaw manually: cd \"{output_dir}\" && claude\n"
            f"  Then ask Claude to review HANDOFF.md."
        )
        return

    console.print("\n[bold]Running OpenClaw final stamp (claude CLI)…[/bold]")
    prompt = (
        "You are doing a final autonomous quality check for a software project. "
        "Read HANDOFF.md first to understand what was built and what (if anything) is flagged. "
        "Then read the relevant source files. "
        "Give a concise one-paragraph verdict: does the project meet its stated goal? "
        "If there are remaining issues, list them briefly. "
        "End with either OPENCLAW: APPROVED or OPENCLAW: ISSUES FOUND."
    )
    try:
        result = subprocess.run(
            ["claude", "--print", prompt],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=output_dir,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        verdict = result.stdout.strip() or result.stderr.strip()
        if not verdict:
            console.print("  [yellow]claude returned no output — skipping stamp.[/yellow]")
            return
    except subprocess.TimeoutExpired:
        console.print("  [yellow]claude CLI timed out — skipping stamp.[/yellow]")
        return
    except FileNotFoundError:
        console.print("  [yellow]claude CLI not found — skipping stamp.[/yellow]")
        return

    # Determine result
    approved = "OPENCLAW: APPROVED" in verdict
    color = "green" if approved else "yellow"
    summary = verdict.splitlines()[-1] if verdict else "No verdict"
    console.print(f"\n  OpenClaw: [bold {color}]{summary}[/bold {color}]")

    # Append verdict to HANDOFF.md
    existing = handoff_path.read_text(encoding="utf-8")
    handoff_path.write_text(
        existing + f"\n## Claude Code Verdict\n\n{verdict}\n",
        encoding="utf-8",
    )
    console.print(f"  Verdict appended to: [dim]{handoff_path}[/dim]")
