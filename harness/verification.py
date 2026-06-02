from __future__ import annotations
import shutil
import subprocess
import sys
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
    # Python takes priority — a FastAPI project with a React frontend
    # subfolder should still be detected as Python/FastAPI, not Node
    req = project_dir / "requirements.txt"
    if req.exists():
        content = req.read_text(encoding="utf-8", errors="ignore").lower()
        if "fastapi" in content:
            return "fastapi"
        return "python"
    if (project_dir / "pyproject.toml").exists():
        content = (project_dir / "pyproject.toml").read_text(encoding="utf-8", errors="ignore").lower()
        if "fastapi" in content:
            return "fastapi"
        return "python"
    # Phaser: game.js present + no package.json at root
    if (project_dir / "game.js").exists() and not (project_dir / "package.json").exists():
        return "phaser"
    if (project_dir / "vite.config.js").exists() or (project_dir / "vite.config.ts").exists():
        return "react-vite"
    if (project_dir / "package.json").exists():
        return "node"
    return "unknown"


def run_verification(task, project_dir: Path) -> tuple[bool, str]:
    """Run the verification check for a completed task. Returns (passed, log_text)."""
    method = task.verification

    if method == "none":
        return True, ""

    if method == "manual":
        # Auto-handle bare HTML tasks — no package.json means no npm runner.
        # final_review.py still does a full content check at the end.
        if _is_bare_html_task(task, project_dir):
            return _run_html_auto(project_dir)
        return _run_manual(task)

    ecosystem = detect_ecosystem(project_dir)

    # Special multi-step handlers
    if ecosystem == "react-vite" and method == "build":
        return _run_react_vite_build(project_dir)

    if ecosystem == "react-vite" and method == "unit_test":
        return _run_react_vite_test(project_dir)

    if ecosystem in ("fastapi", "python") and method == "unit_test":
        return _run_python_test(project_dir)

    if ecosystem == "fastapi" and method == "build":
        return _run_fastapi_install(project_dir)

    if ecosystem == "phaser":
        # If package.json exists, treat as node so npm scripts (including Playwright) run.
        # Falls back to auto HTML check if still no package.json present.
        pkg = project_dir / "package.json"
        if pkg.exists():
            ecosystem = "node"
        else:
            return _run_html_auto(project_dir)

    cmd = _COMMANDS.get(ecosystem, _COMMANDS["unknown"]).get(method)

    if cmd is None:
        console.print(
            f"  [yellow]No {method!r} command for ecosystem {ecosystem!r} — auto-passing.[/yellow]"
        )
        return True, f"auto-passed: no command for {method} in {ecosystem}"

    timeout = _TIMEOUT_BUILD if method == "build" else _TIMEOUT_DEFAULT
    return _run_cmd(cmd, project_dir, timeout)


def _npm_cmd() -> str | None:
    """Return the npm executable name for this platform, or None if not found."""
    candidates = ["npm.cmd", "npm"] if sys.platform == "win32" else ["npm"]
    for name in candidates:
        if shutil.which(name):
            return name
    return None


def _run_python_test(project_dir: Path) -> tuple[bool, str]:
    """Run pytest if installed; auto-pass if pytest is not importable."""
    ok, _ = _run_cmd(["python", "-c", "import pytest"], project_dir, 10)
    if not ok:
        console.print("  [yellow]pytest not installed — auto-passing unit_test.[/yellow]")
        return True, "auto-passed: pytest not installed"
    return _run_cmd(["python", "-m", "pytest", "-q"], project_dir, _TIMEOUT_DEFAULT)


def _run_react_vite_test(project_dir: Path) -> tuple[bool, str]:
    """Run vitest unit tests — skip gracefully if node_modules not yet installed."""
    npm = _npm_cmd()
    if npm is None:
        return True, "auto-passed: npm not available"
    node_modules = project_dir / "node_modules"
    if not node_modules.exists():
        console.print(
            "  [yellow]unit_test skipped — node_modules not installed yet. "
            "Auto-passing (build step will install).[/yellow]"
        )
        return True, "auto-passed: node_modules not installed yet"
    return _run_cmd([npm, "test", "--if-present"], project_dir, _TIMEOUT_DEFAULT)


def _run_react_vite_build(project_dir: Path) -> tuple[bool, str]:
    npm = _npm_cmd()
    if npm is None:
        console.print(
            "  [yellow]npm not found — skipping build verification. "
            "Install Node.js to enable builds.[/yellow]"
        )
        # Still check that the key source files exist
        missing = [f for f in ["package.json", "index.html"] if not (project_dir / f).exists()]
        if missing:
            return False, f"Missing required files: {missing}"
        return True, "auto-passed: npm not available, files present"

    # Skip build if source entry point isn't written yet (scaffold phase)
    has_index = (project_dir / "index.html").exists()
    has_entry = any((project_dir / f).exists() for f in [
        "src/main.tsx", "src/main.jsx", "src/main.ts", "src/main.js"
    ])
    if not has_index or not has_entry:
        missing = ([" index.html"] if not has_index else []) + (["src/main.*"] if not has_entry else [])
        console.print(f"  [yellow]Build skipped — source files not yet written:{','.join(missing)}. Auto-passing scaffold.[/yellow]")
        return True, "auto-passed: scaffold phase, source files pending"

    console.print("  [dim]React+Vite build: npm install && npm run build[/dim]")
    ok, log = _run_cmd([npm, "install"], project_dir, _TIMEOUT_BUILD)
    if not ok:
        return False, f"npm install failed:\n{log}"
    ok, log2 = _run_cmd([npm, "run", "build"], project_dir, _TIMEOUT_BUILD)
    if not ok:
        return False, f"npm run build failed:\n{log2}"
    dist = project_dir / "dist" / "index.html"
    if not dist.exists():
        return False, f"Build succeeded but dist/index.html not found at {dist}"
    console.print("  [green]Build output: dist/index.html exists.[/green]")
    return True, log + "\n" + log2


def _run_fastapi_install(project_dir: Path) -> tuple[bool, str]:
    req = project_dir / "requirements.txt"
    pyproj = project_dir / "pyproject.toml"
    if req.exists():
        console.print("  [dim]FastAPI: pip install -r requirements.txt[/dim]")
        ok, log = _run_cmd(["pip", "install", "-r", "requirements.txt"], project_dir, _TIMEOUT_BUILD)
        if not ok:
            return False, f"pip install failed:\n{log}"
        return True, log
    elif pyproj.exists():
        console.print(
            "  [yellow]pyproject.toml detected (Poetry project) — skipping pip install. Auto-passing build.[/yellow]"
        )
        return True, "auto-passed: Poetry project, pip install skipped"
    else:
        console.print("  [yellow]No requirements.txt or pyproject.toml found — auto-passing build.[/yellow]")
        return True, "auto-passed: no requirements file found"


def _run_cmd(cmd: list[str], cwd: Path, timeout: int) -> tuple[bool, str]:
    # Resolve npm to platform-specific executable on Windows
    if cmd[0] == "npm":
        npm = _npm_cmd()
        if npm is None:
            return False, "npm not found — install Node.js to enable this verification"
        cmd = [npm] + cmd[1:]
    console.print(f"  [dim]$ {' '.join(cmd)}[/dim]")
    # On Windows, .cmd wrappers (npm.cmd, npx.cmd) require shell=True to be found
    use_shell = sys.platform == "win32"
    cmd_arg = " ".join(cmd) if use_shell else cmd
    try:
        result = subprocess.run(
            cmd_arg,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=use_shell,
        )
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s"
    except FileNotFoundError:
        return False, f"Command not found: {cmd[0]} — is it installed and on PATH?"

    log = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        console.print(f"  [red]Failed (exit {result.returncode}):[/red]")
        console.print(f"  [dim]{log[-1500:]}[/dim]")
    return result.returncode == 0, log


_STATIC_EXTS = {".html", ".css", ".js", ".ts", ".jsx", ".tsx", ".svg", ".ico"}


def _is_bare_html_task(task, project_dir: Path) -> bool:
    """True when the task only touches static web files and there is no npm runner."""
    if (project_dir / "package.json").exists():
        return False
    return all(Path(f).suffix.lower() in _STATIC_EXTS for f in task.files)


def _run_html_auto(project_dir: Path) -> tuple[bool, str]:
    """Headless structure check for bare HTML projects (no package.json)."""
    html_files = list(project_dir.glob("*.html"))
    if not html_files:
        return False, "No .html files found in project directory"
    issues = []
    for f in html_files:
        content = f.read_text(encoding="utf-8", errors="replace").lower()
        for tag in ("<html", "<body"):
            if tag not in content:
                issues.append(f"{f.name}: missing {tag} tag")
    if issues:
        return False, "; ".join(issues)
    console.print("  [green]HTML structure check passed (headless).[/green]")
    return True, "html structure valid"


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
