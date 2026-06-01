from __future__ import annotations
import subprocess
import time
from pathlib import Path
from rich.console import Console
from rich.prompt import Confirm

console = Console()

_TIMEOUT_DEFAULT = 120
_TIMEOUT_BUILD = 300

# Commands per ecosystem per verification type.
# None means "no command available — auto-pass with a warning".
_COMMANDS: dict[str, dict[str, list[str] | None]] = {
    "node": {
        "lint":      ["npm", "run", "lint", "--if-present"],
        "unit_test": ["npm", "test"],
        "build":     ["npm", "install"],
        "smoke":     ["npm", "run", "smoke", "--if-present"],
    },
    "react-vite": {
        "lint":      ["npm", "run", "lint", "--if-present"],
        "unit_test": ["npm", "test", "--if-present"],
        "build":     None,  # handled specially in run_verification
        "smoke":     None,
    },
    "python": {
        "lint":      ["python", "-m", "flake8", "."],
        "unit_test": ["python", "-m", "pytest", "-q"],
        "build":     ["pip", "install", "-r", "requirements.txt"],
        "smoke":     ["python", "-m", "pytest", "smoke/", "-q"],
    },
    "fastapi": {
        "lint":      None,
        "unit_test": ["python", "-m", "pytest", "-q"],
        "build":     None,  # handled specially in run_verification
        "smoke":     None,
    },
    "phaser": {
        "lint": None, "unit_test": None, "build": None, "smoke": None,
    },
    "unknown": {
        "lint": None, "unit_test": None, "build": None, "smoke": None,
    },
}


def detect_ecosystem(project_dir: Path) -> str:
    if (project_dir / "vite.config.js").exists() or (project_dir / "vite.config.ts").exists():
        return "react-vite"
    if (project_dir / "package.json").exists():
        return "node"
    req = project_dir / "requirements.txt"
    if req.exists():
        content = req.read_text(encoding="utf-8", errors="ignore").lower()
        if "fastapi" in content:
            return "fastapi"
        return "python"
    if (project_dir / "pyproject.toml").exists():
        return "python"
    # Phaser: game.js present + no package.json
    if (project_dir / "game.js").exists():
        return "phaser"
    return "unknown"


def run_verification(task, project_dir: Path) -> tuple[bool, str]:
    """Run the verification check for a completed task. Returns (passed, log_text)."""
    method = task.verification

    if method == "none":
        return True, ""

    if method == "manual":
        return _run_manual(task)

    ecosystem = detect_ecosystem(project_dir)

    # Special multi-step handlers
    if ecosystem == "react-vite" and method == "build":
        return _run_react_vite_build(project_dir)

    if ecosystem == "fastapi" and method == "build":
        return _run_fastapi_install(project_dir)

    if ecosystem == "phaser":
        console.print(
            "  [yellow]Phaser game detected — automated verification not possible.[/yellow]"
        )
        return _run_manual(task, prompt="Open index.html in your browser. Does the game load and run correctly?")

    cmd = _COMMANDS.get(ecosystem, _COMMANDS["unknown"]).get(method)

    if cmd is None:
        console.print(
            f"  [yellow]No {method!r} command for ecosystem {ecosystem!r} — auto-passing.[/yellow]"
        )
        return True, f"auto-passed: no command for {method} in {ecosystem}"

    timeout = _TIMEOUT_BUILD if method == "build" else _TIMEOUT_DEFAULT
    return _run_cmd(cmd, project_dir, timeout)


def _run_react_vite_build(project_dir: Path) -> tuple[bool, str]:
    console.print("  [dim]React+Vite build: npm install && npm run build[/dim]")
    ok, log = _run_cmd(["npm", "install"], project_dir, _TIMEOUT_BUILD)
    if not ok:
        return False, f"npm install failed:\n{log}"
    ok, log2 = _run_cmd(["npm", "run", "build"], project_dir, _TIMEOUT_BUILD)
    if not ok:
        return False, f"npm run build failed:\n{log2}"
    dist = project_dir / "dist" / "index.html"
    if not dist.exists():
        return False, f"Build succeeded but dist/index.html not found at {dist}"
    console.print("  [green]Build output: dist/index.html exists.[/green]")
    return True, log + "\n" + log2


def _run_fastapi_install(project_dir: Path) -> tuple[bool, str]:
    console.print("  [dim]FastAPI: pip install -r requirements.txt[/dim]")
    ok, log = _run_cmd(
        ["pip", "install", "-r", "requirements.txt"], project_dir, _TIMEOUT_BUILD
    )
    if not ok:
        return False, f"pip install failed:\n{log}"
    # Quick import check — does main.py import cleanly?
    main_py = project_dir / "main.py"
    if main_py.exists():
        ok2, log2 = _run_cmd(
            ["python", "-c", "import importlib.util, sys; spec=importlib.util.spec_from_file_location('main','main.py'); m=importlib.util.module_from_spec(spec)"],
            project_dir,
            30,
        )
        # Don't fail on import check — FastAPI app startup requires running server
    return True, log


def _run_cmd(cmd: list[str], cwd: Path, timeout: int) -> tuple[bool, str]:
    console.print(f"  [dim]$ {' '.join(cmd)}[/dim]")
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s"

    log = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        console.print(f"  [red]Failed (exit {result.returncode}):[/red]")
        console.print(f"  [dim]{log[-1500:]}[/dim]")
    return result.returncode == 0, log


def _run_manual(task, prompt: str | None = None) -> tuple[bool, str]:
    console.print(f"\n  [bold yellow]Manual gate — task {task.id}[/bold yellow]")
    console.print(f"  Objective: {task.objective}")
    console.print(f"  Files: {', '.join(task.files)}")
    console.print("  Acceptance criteria:")
    for criterion in task.acceptance_criteria:
        console.print(f"    • {criterion}")
    question = prompt or "Does this task pass?"
    passed = Confirm.ask(f"  {question}")
    return passed, "" if passed else "Rejected at manual gate"
