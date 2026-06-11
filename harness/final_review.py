"""Final code review step — runs after PROJECT_REVIEW passes.

Collects all project output files, sends them to Claude for a code review,
writes the verdict to REVIEW.md, and returns True (pass) or False (issues found).
"""
from __future__ import annotations
import anthropic
from pathlib import Path
from rich.console import Console

from config import ANTHROPIC_API_KEY, ORCHESTRATOR_MODEL
from cache_telemetry import log_cache_usage
from cost import record_usage

console = Console()

_SKIP_DIRS = {"node_modules", ".venv", "__pycache__", ".git", "dist", ".playwright"}
_REVIEW_EXTS = {".js", ".py", ".html", ".css", ".ts", ".jsx", ".tsx", ".json", ".md"}
_SKIP_FILES = {"REVIEW.md", "HANDOFF.md", "package-lock.json", "yarn.lock"}
_MAX_TOTAL_CHARS = 120_000  # stay well under token limits

_SYSTEM = """\
You are a senior code reviewer performing a final quality gate on an AI-generated project.
You will receive the project goal and all output files. Your job is to check for:

1. Stub placeholders — comments like "// Existing logic", "// Implementation unchanged", "// TODO: implement"
2. Empty or hollow functions/classes — defined but doing nothing
3. Broken imports or missing dependencies referenced in code but not declared
4. Obvious runtime errors — syntax issues, undefined variables, wrong API calls
5. Files that are missing entirely relative to what the goal requires

Be concise. Report only real problems. If the project looks correct and complete, say so clearly.

Respond in this exact format:

VERDICT: PASS   (or VERDICT: ISSUES FOUND)

SUMMARY:
<1-3 sentences on overall quality>

ISSUES:
- <issue description with file name>  (omit this section entirely if no issues)
"""


def run_final_review(output_dir: Path, spec: dict) -> bool:
    """
    Review all files in output_dir against the project spec.
    Returns True if review passes, False if issues found.
    Writes output_dir/REVIEW.md regardless of result.
    """
    if not ANTHROPIC_API_KEY:
        console.print("  [yellow]No ANTHROPIC_API_KEY — skipping final review.[/yellow]")
        return True

    goal = spec.get("goal", spec.get("description", "Unknown project goal"))
    files = _collect_files(output_dir)

    if not files:
        console.print("  [yellow]No output files found — skipping final review.[/yellow]")
        return True

    console.print(f"\n[bold]Running final Claude Code review ({len(files)} files)…[/bold]")

    user_message = f"Project goal: {goal}\n\n"
    total = 0
    included = []
    for rel, content in files:
        chunk = f"=== {rel} ===\n{content}\n\n"
        if total + len(chunk) > _MAX_TOTAL_CHARS:
            user_message += f"[{len(files) - len(included)} more files omitted — token limit]\n"
            break
        user_message += chunk
        total += len(chunk)
        included.append(rel)

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=ORCHESTRATOR_MODEL,
            max_tokens=1024,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
        )
        log_cache_usage(response.usage, "review")
        record_usage(response.usage, ORCHESTRATOR_MODEL, "review")
        review_text = response.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]Final review API call failed: {exc}[/red]")
        return True  # don't block the pipeline on review failure

    passed = review_text.startswith("VERDICT: PASS")
    color = "green" if passed else "red"
    verdict_line = review_text.splitlines()[0] if review_text else "VERDICT: UNKNOWN"
    console.print(f"\n  Final review: [bold {color}]{verdict_line}[/bold {color}]")

    review_path = output_dir / "REVIEW.md"
    review_path.write_text(
        f"# Final Code Review\n\n"
        f"**Project:** {goal}\n\n"
        f"**Files reviewed:** {', '.join(included)}\n\n"
        f"---\n\n{review_text}\n",
        encoding="utf-8",
    )
    console.print(f"  Review written to: {review_path}")

    if not passed:
        console.print(
            "\n  [bold red]Final review flagged issues.[/bold red] "
            "See REVIEW.md for details. Fix and re-run, or inspect manually."
        )

    return passed


def parse_review_issues(review_path: Path) -> list[str]:
    """Extract the bullet-point issue lines from a REVIEW.md ISSUES section."""
    if not review_path.exists():
        return []
    issues: list[str] = []
    in_issues = False
    for line in review_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "ISSUES:":
            in_issues = True
            continue
        if in_issues:
            if stripped.startswith("- "):
                issues.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("-"):
                break  # hit a non-bullet line — end of section
    return issues


def _collect_files(output_dir: Path) -> list[tuple[str, str]]:
    """Return [(relative_path, content)] for all reviewable files, sorted by path."""
    BINARY_EXTS = {".mp4", ".webm", ".mov", ".wav", ".mp3", ".flac", ".ogg",
                   ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot"}
    results = []
    for path in sorted(output_dir.rglob("*")):
        if path.is_dir():
            continue
        # Skip unwanted directories
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.name in _SKIP_FILES:
            continue
        if path.suffix.lower() not in _REVIEW_EXTS:
            continue
        if path.suffix.lower() in BINARY_EXTS:
            continue
        if path.stat().st_size > 120_000:
            continue
        rel = str(path.relative_to(output_dir)).replace("\\", "/")
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        results.append((rel, content))
    return results
