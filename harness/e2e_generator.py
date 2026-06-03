"""Automatic Playwright E2E test generator for J-Claw projects.

Called after all code tasks complete (before final review) for web ecosystems.
Uses the same Ollama worker that generates code to write tests/e2e.spec.ts.
"""
from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

from config import WORKER_MODEL, WORKER_PROVIDER, WORKER_FALLBACKS, OLLAMA_HOST

console = Console()

# File extensions to collect for context, in priority order.
_EXT_PRIORITY = [".html", ".jsx", ".tsx", ".js", ".ts", ".py"]
# Max characters to include per file.
_MAX_FILE_CHARS = 4000
# Max number of files to include.
_MAX_FILES = 8

# Playwright system prompt — instructs the worker to write test files only.
_E2E_SYSTEM = """\
You are a precise test-writing assistant in an automated pipeline.
You receive source files for a web project and write a Playwright E2E test suite.

Rules:
- Output ONLY a valid JSON object — no markdown, no prose, no explanation.
- The JSON must match this schema exactly:
  {"files": [{"path": "relative/path.ext", "content": "complete file content"}]}
- Write only tests/e2e.spec.ts (TypeScript Playwright).
- Tests must import from "@playwright/test" and use test.describe blocks.
- Always use page.goto('http://localhost:3000') as the base URL.
- Use data-testid attributes if present in the source; otherwise use role/text selectors.
- Do not include placeholder comments. Write complete, working test content.
"""

# Stack-specific guidance injected into the user prompt.
_STACK_GUIDANCE: dict[str, str] = {
    "phaser": """\
Stack guidance (Phaser/vanilla canvas game):
- Test that the page loads without uncaught JS errors (use page.on('pageerror')).
- Verify a <canvas> element is present: await expect(page.locator('canvas')).toBeVisible().
- Check that the page title is set and non-empty.
- Do NOT attempt to interact with canvas pixels — Phaser games are not DOM-testable beyond presence checks.
""",
    "three-js": """\
Stack guidance (Three.js/vanilla 3D app):
- Test that the page loads without uncaught JS errors (use page.on('pageerror')).
- Verify a <canvas> element is present: await expect(page.locator('canvas')).toBeVisible().
- Check that the page title is set and non-empty.
- Do NOT attempt to interact with canvas pixels — Three.js scenes are not DOM-testable beyond presence checks.
""",
    "vanilla": """\
Stack guidance (vanilla HTML/CSS/JS):
- Test that the page title is set and non-empty.
- Test that the main heading or primary content element is visible.
- Test at least one key interactive element (button click, form submit, or link navigation).
- Check for no uncaught JS errors using page.on('pageerror', ...).
- If a form is present, fill fields and submit; assert expected output appears.
""",
    "react-vite": """\
Stack guidance (React + Vite):
- Test that the root #root element mounts and main content is visible.
- Test that the primary UI component renders (use role or text selectors).
- Test at least one interactive action: button click, form input, or navigation.
- Check for no uncaught JS errors using page.on('pageerror', ...).
- If routing is present, test at least the default route.
""",
}


def _collect_source_files(project_dir: Path) -> list[tuple[str, str]]:
    """Return up to _MAX_FILES (path, content) pairs, prioritised by extension."""
    candidates: list[Path] = []

    # Collect files in extension priority order, skipping node_modules / __pycache__ / tests/
    for ext in _EXT_PRIORITY:
        for p in sorted(project_dir.rglob(f"*{ext}")):
            if any(part in {"node_modules", "__pycache__", "tests", ".git", "dist", "build"}
                   for part in p.parts):
                continue
            # index.html / main.* files float to the top within their extension group
            if p.stem in {"index", "main", "App", "app"}:
                candidates.insert(0, p)
            else:
                candidates.append(p)

    seen: set[Path] = set()
    result: list[tuple[str, str]] = []
    for p in candidates:
        if p in seen or len(result) >= _MAX_FILES:
            break
        seen.add(p)
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            if len(content) > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS] + "\n... (truncated)"
            result.append((str(p.relative_to(project_dir)), content))
        except OSError:
            pass

    return result


def _build_prompt(project_dir: Path, spec: dict, ecosystem: str, tasks_done: list) -> str:
    """Build the user message sent to the worker."""
    goal = spec.get("goal", "unknown project")
    source_files = _collect_source_files(project_dir)

    stack_hint = _STACK_GUIDANCE.get(ecosystem, _STACK_GUIDANCE["vanilla"])

    task_objectives = [
        t.get("objective", "") if isinstance(t, dict) else getattr(t, "objective", "")
        for t in tasks_done
    ]

    payload = {
        "task": {
            "id": "e2e_tests",
            "type": "qa_playwright",
            "objective": (
                f"Write a Playwright E2E test suite for this {ecosystem} project. "
                "Test the golden path: page loads, key elements exist, main interaction works."
            ),
            "files": ["tests/e2e.spec.ts"],
            "acceptance_criteria": [
                "tests/e2e.spec.ts uses @playwright/test imports",
                "tests use test.describe and test() blocks",
                "page.goto('http://localhost:3000') is the entry URL",
                "at least 3 meaningful assertions",
                "no uncaught JS error check is included",
            ],
        },
        "project_context": {
            "goal": goal,
            "stack": ecosystem,
            "completed_task_objectives": task_objectives[:10],
        },
        "stack_guidance": stack_hint,
        "source_files": {path: content for path, content in source_files},
    }

    return json.dumps(payload, indent=2)


def _call_worker(system: str, user: str) -> str | None:
    """Call the worker using the same provider chain as worker.py.

    Returns the raw string response, or None on total failure.
    """
    attempts: list[tuple[str, str]] = [(WORKER_PROVIDER, WORKER_MODEL)] + list(WORKER_FALLBACKS)

    for provider, model in attempts:
        try:
            if provider == "ollama":
                import ollama as _ollama
                client = _ollama.Client(host=OLLAMA_HOST)
                response = client.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    format="json",
                    options={"temperature": 0.1, "num_predict": 4096},
                )
                return response.message.content.strip()

            elif provider == "anthropic":
                from config import ANTHROPIC_API_KEY
                if not ANTHROPIC_API_KEY:
                    continue
                import anthropic as _anthropic
                client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                resp = client.messages.create(
                    model=model,
                    max_tokens=4096,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return resp.content[0].text.strip()

            elif provider == "openrouter":
                from config import OPENROUTER_API_KEY
                if not OPENROUTER_API_KEY:
                    continue
                from openai import OpenAI
                client = OpenAI(
                    api_key=OPENROUTER_API_KEY,
                    base_url="https://openrouter.ai/api/v1",
                    default_headers={"X-Title": "J-Claw"},
                )
                resp = client.chat.completions.create(
                    model=model,
                    max_tokens=4096,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    response_format={"type": "json_object"},
                )
                return resp.choices[0].message.content.strip()

        except Exception as exc:  # noqa: BLE001
            console.print(f"  [yellow]E2E generator: {provider}/{model} error: {exc!r} — trying next…[/yellow]")
            continue

    return None


def _parse_files(raw: str) -> list[dict] | None:
    """Parse a worker JSON response and return the files list, or None on error."""
    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        raw = "\n".join(inner).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        console.print(f"  [yellow]E2E generator: invalid JSON from worker: {exc}[/yellow]")
        return None

    files = parsed.get("files")
    if not isinstance(files, list):
        console.print(f"  [yellow]E2E generator: worker response missing 'files' list[/yellow]")
        return None

    return files


def generate_e2e_tests(
    project_dir: Path,
    spec: dict,
    tasks_done: list,
    ecosystem: str = "vanilla",
) -> bool:
    """Generate a Playwright E2E test file for the completed project.

    Args:
        project_dir: Root directory of the generated project.
        spec: The project spec dict (used for goal, stack).
        tasks_done: List of completed task dicts (for context).
        ecosystem: Detected ecosystem string (vanilla/react-vite/phaser/three-js).

    Returns:
        True if tests/e2e.spec.ts was written successfully, False otherwise.
    """
    console.print("\n[bold]Generating Playwright E2E tests…[/bold]")

    user_message = _build_prompt(project_dir, spec, ecosystem, tasks_done)

    raw = _call_worker(_E2E_SYSTEM, user_message)
    if raw is None:
        console.print("  [yellow]E2E generator: all worker providers failed — skipping tests.[/yellow]")
        return False

    files = _parse_files(raw)
    if files is None:
        return False

    written = False
    for entry in files:
        path_str = entry.get("path", "")
        content = entry.get("content", "")
        if not isinstance(path_str, str) or not isinstance(content, str):
            continue

        # Only write files under tests/ to avoid clobbering generated code
        norm = path_str.replace("\\", "/")
        if not norm.startswith("tests/"):
            console.print(f"  [yellow]E2E generator: skipping unexpected path '{path_str}'[/yellow]")
            continue

        dest = project_dir / Path(norm)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        console.print(f"  [green]E2E tests written: {dest.relative_to(project_dir)}[/green]")
        written = True

    if not written:
        console.print("  [yellow]E2E generator: no test files in worker response — skipping.[/yellow]")

    return written
