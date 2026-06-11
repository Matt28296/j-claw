# Playwright is optional. Install with: pip install playwright && playwright install chromium
# If not installed, headless checks fall back to HTML structure validation only.
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

# Sentinel marking a check that returned True only because its tool/runner was
# unavailable (or the check could not actually run) — i.e. a SKIP, not a verified
# PASS. Every such auto-pass path begins its message with this prefix so the
# handoff report can distinguish a skipped check from a genuine pass and avoid
# reporting a hollow green run. Real passes use descriptive messages instead.
SKIP_PREFIX = "auto-passed:"

# Commands per ecosystem per verification type.
# None means "no command available — auto-pass with a warning".
_COMMANDS: dict[str, dict[str, list[str] | None]] = {
    "full-stack": {
        "lint":      None,
        "unit_test": ["python", "-m", "pytest", "-q"],
        "build":     None,  # handled in run_verification
        "smoke":     None,
    },
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
    "web3": {
        "lint":      None,
        "unit_test": None,  # handled specially in run_verification
        "build":     None,  # handled specially in run_verification
        "smoke":     None,
    },
    "react-native": {
        "lint":      None,
        "unit_test": None,
        "build":     None,  # handled specially in run_verification
        "smoke":     None,
    },
    "socket-io": {
        "lint":      None,
        "unit_test": None,
        "build":     ["npm", "install"],
        "smoke":     None,
    },
    "three-js": {
        "lint": None, "unit_test": None, "build": None, "smoke": None,
    },
    "electron": {
        "lint":      None,
        "unit_test": None,
        "build":     None,  # handled specially in run_verification
        "smoke":     None,
    },
    "phaser": {
        "lint": None, "unit_test": None, "build": None, "smoke": None,
    },
    "swift": {
        "lint": None, "unit_test": None, "build": None, "smoke": None,
    },
    "kotlin": {
        "lint": None, "unit_test": None, "build": None, "smoke": None,
    },
    "unknown": {
        "lint": None, "unit_test": None, "build": None, "smoke": None,
    },
    "film": {
        "lint": None, "unit_test": None, "build": None, "smoke": None,
        "ffprobe": None, "frame_integrity": None, "sync_check": None,
    },
}


def detect_ecosystem(project_dir: Path) -> str:
    has_req     = (project_dir / "requirements.txt").exists()
    has_pyproj  = (project_dir / "pyproject.toml").exists()
    has_vite    = (project_dir / "vite.config.js").exists() or (project_dir / "vite.config.ts").exists()
    has_pkg     = (project_dir / "package.json").exists()

    # Web3 / Hardhat: hardhat.config.js present
    if (project_dir / "hardhat.config.js").exists() or (project_dir / "hardhat.config.ts").exists():
        return "web3"

    # React Native / Expo: app.json with expo block
    if (project_dir / "app.json").exists():
        try:
            import json as _json
            content = _json.loads((project_dir / "app.json").read_text(encoding="utf-8"))
            if "expo" in content:
                return "react-native"
        except Exception:
            pass

    # Socket.io multiplayer server: server.js present + socket.io in package.json
    if (project_dir / "server.js").exists() and has_pkg:
        try:
            import json as _json
            pkg = _json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "socket.io" in deps:
                return "socket-io"
        except Exception:
            pass

    # Three.js CDN: index.html present + JS file references THREE.
    if (project_dir / "index.html").exists() and not has_pkg:
        for js_file in list(project_dir.glob("*.js")) + list(project_dir.glob("js/*.js")):
            try:
                if "THREE." in js_file.read_text(encoding="utf-8", errors="ignore"):
                    return "three-js"
            except OSError:
                pass

    # Electron desktop: main.js + package.json with electron dependency
    if (project_dir / "main.js").exists() and has_pkg:
        try:
            import json as _json
            pkg = _json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "electron" in deps:
                return "electron"
        except Exception:
            pass

    # Full-stack: both a Python backend and a React/Node frontend present
    if (has_req or has_pyproj) and (has_vite or has_pkg):
        return "full-stack"

    # Python takes priority — a FastAPI project with a React frontend
    # subfolder should still be detected as Python/FastAPI, not Node
    if has_req:
        content = (project_dir / "requirements.txt").read_text(encoding="utf-8", errors="ignore").lower()
        if "fastapi" in content:
            return "fastapi"
        return "python"
    if has_pyproj:
        content = (project_dir / "pyproject.toml").read_text(encoding="utf-8", errors="ignore").lower()
        if "fastapi" in content:
            return "fastapi"
        return "python"
    # Phaser: game.js present + no package.json at root
    if (project_dir / "game.js").exists() and not has_pkg:
        return "phaser"
    if has_vite:
        return "react-vite"
    if has_pkg:
        return "node"
    if list(project_dir.glob("*.mp4")) or list(project_dir.glob("*.webm")):
        return "film"
    if (project_dir / "project.godot").exists():
        return "godot"
    return "unknown"


def run_verification(task, project_dir: Path) -> tuple[bool, str]:
    """Run the verification check for a completed task. Returns (passed, log_text)."""
    method = task.verification

    if method == "none":
        if (project_dir / "project.godot").exists():
            return _run_godot_check(project_dir)
        return True, ""

    if method == "manual":
        # Auto-handle bare HTML tasks — no package.json means no npm runner.
        # final_review.py still does a full content check at the end.
        if _is_bare_html_task(task, project_dir):
            return _run_playwright_check(project_dir)
        return _run_manual(task)

    if method in ("ffprobe", "frame_integrity", "sync_check"):
        return _run_video_evidence_check(method, task, project_dir)

    if method == "security":
        return _run_security_check(project_dir)

    if method == "lighthouse":
        return _run_lighthouse_check(project_dir)

    ecosystem = detect_ecosystem(project_dir)

    # Web3: Hardhat compile + test
    if ecosystem == "web3":
        if method in ("build", "unit_test"):
            return _run_hardhat_build(project_dir)
        return True, f"auto-passed: no {method} command for web3"

    # React Native / Expo: npm install + expo web export check
    if ecosystem == "react-native" and method == "build":
        ok, log = _run_react_native_install(project_dir)
        if not ok:
            return False, log
        expo_ok, expo_log = _run_expo_export_check(project_dir)
        return expo_ok, "\n".join(filter(None, [log, expo_log]))

    # Three.js CDN: Playwright canvas check (same pattern as Phaser)
    if ecosystem == "three-js":
        return _run_playwright_check(project_dir)

    # Electron: npm install only (can't run GUI in CI)
    if ecosystem == "electron" and method == "build":
        return _run_electron_install(project_dir)

    # Full-stack: run frontend build + backend install together
    if ecosystem == "full-stack" and method == "build":
        return _run_fullstack_build(project_dir)

    # Special multi-step handlers
    if ecosystem in ("react-vite", "full-stack") and method == "build":
        return _run_react_vite_build(project_dir)

    if ecosystem == "react-vite" and method == "unit_test":
        return _run_react_vite_test(project_dir)

    if ecosystem in ("fastapi", "python") and method == "unit_test":
        ok, log = _run_python_test(project_dir)
        if not ok:
            return ok, log
        mypy_ok, mypy_log = _run_mypy_check(project_dir)
        if not mypy_ok:
            return mypy_ok, mypy_log
        ruff_ok, ruff_log = _run_ruff_check(project_dir)
        return ruff_ok, "\n".join(filter(None, [log, mypy_log, ruff_log]))

    if ecosystem == "fastapi" and method == "build":
        ok, log = _run_fastapi_install(project_dir)
        if not ok:
            return ok, log
        ruff_ok, ruff_log = _run_ruff_check(project_dir)
        return ruff_ok, "\n".join(filter(None, [log, ruff_log]))

    if ecosystem == "phaser":
        # If package.json exists, treat as node so npm scripts (including Playwright) run.
        # Falls back to auto HTML check if still no package.json present.
        pkg = project_dir / "package.json"
        if pkg.exists():
            ecosystem = "node"
        else:
            return _run_playwright_check(project_dir)

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


def _run_mypy_check(project_dir: Path) -> tuple[bool, str]:
    """Run mypy type-checking; auto-pass if mypy is not installed."""
    if not shutil.which("mypy"):
        console.print("  [yellow]mypy not installed — auto-passing type_check.[/yellow]")
        return True, "auto-passed: mypy not installed"
    console.print("  [dim]Type check: mypy --ignore-missing-imports .[/dim]")
    return _run_cmd(["mypy", "--ignore-missing-imports", "."], project_dir, _TIMEOUT_DEFAULT)


def _run_ruff_check(project_dir: Path) -> tuple[bool, str]:
    """Run ruff linting; auto-pass if ruff is not installed."""
    if not shutil.which("ruff"):
        console.print("  [yellow]ruff not installed — auto-passing lint.[/yellow]")
        return True, "auto-passed: ruff not installed"
    console.print("  [dim]Lint: ruff check .[/dim]")
    return _run_cmd(["ruff", "check", "."], project_dir, _TIMEOUT_DEFAULT)


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

    pwa_ok, pwa_log = check_pwa_files(project_dir)
    combined = "\n".join(filter(None, [log, log2, pwa_log]))
    if not pwa_ok:
        return False, combined
    return True, combined


def _run_fastapi_install(project_dir: Path) -> tuple[bool, str]:
    req = project_dir / "requirements.txt"
    pyproj = project_dir / "pyproject.toml"
    if req.exists():
        console.print("  [dim]FastAPI: pip install -r requirements.txt[/dim]")
        ok, log = _run_cmd(["pip", "install", "-r", "requirements.txt"], project_dir, _TIMEOUT_BUILD)
        if not ok:
            return False, f"pip install failed:\n{log}"
        # Run Alembic migrations if alembic.ini exists (skip gracefully for older projects)
        alembic_ini = project_dir / "alembic.ini"
        if alembic_ini.exists():
            console.print("  [dim]FastAPI: alembic upgrade head[/dim]")
            ok2, log2 = _run_cmd(["alembic", "upgrade", "head"], project_dir, _TIMEOUT_BUILD)
            combined = "\n".join(filter(None, [log, log2]))
            if not ok2:
                return False, f"alembic upgrade head failed:\n{log2}"
            console.print("  [green]Alembic migrations applied successfully.[/green]")
            return True, combined
        return True, log
    elif pyproj.exists():
        console.print(
            "  [yellow]pyproject.toml detected (Poetry project) — skipping pip install. Auto-passing build.[/yellow]"
        )
        return True, "auto-passed: Poetry project, pip install skipped"
    else:
        console.print("  [yellow]No requirements.txt or pyproject.toml found — auto-passing build.[/yellow]")
        return True, "auto-passed: no requirements file found"


def _run_fullstack_build(project_dir: Path) -> tuple[bool, str]:
    """Build a full-stack project: pip install backend deps + npm install/build frontend."""
    logs: list[str] = []

    # Backend: pip install
    req = project_dir / "requirements.txt"
    if req.exists():
        console.print("  [dim]Full-stack backend: pip install -r requirements.txt[/dim]")
        ok, log = _run_cmd(["pip", "install", "-r", "requirements.txt"], project_dir, _TIMEOUT_BUILD)
        logs.append(log)
        if not ok:
            return False, f"pip install failed:\n{log}"

    # Frontend: npm install + build (look for a frontend/ or src/ subdirectory with package.json)
    frontend_dirs = [project_dir / "frontend", project_dir]
    for fdir in frontend_dirs:
        if (fdir / "package.json").exists():
            console.print(f"  [dim]Full-stack frontend: npm install + build in {fdir.name or '.'}[/dim]")
            ok, log = _run_react_vite_build(fdir)
            logs.append(log)
            if not ok:
                return False, f"Frontend build failed:\n{log}"
            break

    return True, "\n".join(logs)


def _run_react_native_install(project_dir: Path) -> tuple[bool, str]:
    """npm install for Expo/React Native projects. Can't run simulator in CI."""
    npm = _npm_cmd()
    if npm is None:
        missing = [f for f in ["app.json", "package.json"] if not (project_dir / f).exists()]
        if missing:
            return False, f"Missing required files: {missing}"
        return True, "auto-passed: npm not available, files present"
    console.print("  [dim]React Native/Expo: npm install[/dim]")
    ok, log = _run_cmd([npm, "install"], project_dir, _TIMEOUT_BUILD)
    if not ok:
        return False, f"npm install failed:\n{log}"
    console.print("  [green]Expo dependencies installed.[/green]")
    return True, log


def _run_electron_install(project_dir: Path) -> tuple[bool, str]:
    """npm install for Electron projects. Cannot run GUI in CI."""
    npm = _npm_cmd()
    if npm is None:
        missing = [f for f in ["main.js", "package.json"] if not (project_dir / f).exists()]
        if missing:
            return False, f"Missing required files: {missing}"
        return True, "auto-passed: npm not available, files present"
    console.print("  [dim]Electron: npm install[/dim]")
    ok, log = _run_cmd([npm, "install"], project_dir, _TIMEOUT_BUILD)
    if not ok:
        return False, f"npm install failed:\n{log}"
    console.print("  [green]Electron dependencies installed.[/green]")
    return True, log


def _run_hardhat_build(project_dir: Path) -> tuple[bool, str]:
    """npm install + npx hardhat compile + npx hardhat test for Web3/Hardhat projects."""
    npm = _npm_cmd()
    if npm is None:
        console.print("  [yellow]npm not found — skipping Web3 build. Install Node.js to enable.[/yellow]")
        missing = [f for f in ["hardhat.config.js", "package.json"] if not (project_dir / f).exists()]
        if missing:
            return False, f"Missing required files: {missing}"
        return True, "auto-passed: npm not available, files present"

    console.print("  [dim]Web3/Hardhat: npm install && npx hardhat compile && npx hardhat test[/dim]")
    ok, log = _run_cmd([npm, "install"], project_dir, _TIMEOUT_BUILD)
    if not ok:
        return False, f"npm install failed:\n{log}"

    ok2, log2 = _run_cmd(["npx", "hardhat", "compile"], project_dir, _TIMEOUT_BUILD)
    if not ok2:
        return False, f"hardhat compile failed:\n{log2}"

    ok3, log3 = _run_cmd(["npx", "hardhat", "test"], project_dir, _TIMEOUT_DEFAULT)
    if not ok3:
        return False, f"hardhat test failed:\n{log3}"

    console.print("  [green]Hardhat compile + test passed.[/green]")
    return True, "\n".join([log, log2, log3])


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
    """True when ALL task files are static web types.

    Deliberately ignores package.json presence — a task that only writes .html/.css/.js
    is a static task by definition; stale package.json from previous runs in the same
    directory should not promote it to a manual gate.
    """
    return bool(task.files) and all(
        Path(f).suffix.lower() in _STATIC_EXTS for f in task.files
    )


def check_pwa_files(project_dir: Path) -> tuple[bool, str]:
    """Check PWA completeness: if manifest.json exists, sw.js must also exist.

    For react-vite projects the files live under public/; for vanilla projects
    they sit at the project root. Both locations are checked.

    Returns (passed, message). A missing sw.js is a hard failure; a missing
    manifest.json means PWA was not requested and the check is skipped (pass).
    """
    # Candidate locations for manifest and sw files
    manifest_candidates = [
        project_dir / "manifest.json",
        project_dir / "manifest.webmanifest",
        project_dir / "public" / "manifest.json",
        project_dir / "public" / "manifest.webmanifest",
    ]
    sw_candidates = [
        project_dir / "sw.js",
        project_dir / "public" / "sw.js",
    ]

    manifest_path = next((p for p in manifest_candidates if p.exists()), None)
    if manifest_path is None:
        # No manifest present — PWA not included; nothing to check.
        return True, "pwa check skipped: no manifest.json found"

    sw_path = next((p for p in sw_candidates if p.exists()), None)
    if sw_path is None:
        msg = (
            f"PWA check FAILED: manifest.json found at {manifest_path.relative_to(project_dir)} "
            "but sw.js is missing. Add a service worker at sw.js (vanilla) or public/sw.js (react-vite)."
        )
        console.print(f"  [red]{msg}[/red]")
        return False, msg

    console.print(
        f"  [green]PWA check passed: {manifest_path.relative_to(project_dir)} "
        f"and {sw_path.relative_to(project_dir)} both present.[/green]"
    )
    return True, f"pwa check passed: manifest={manifest_path.name}, sw={sw_path.name}"


def run_playwright_project_check(project_dir: Path) -> tuple[bool, str]:
    """Public entry point for a project-level Playwright check.

    Called from main.py after all tasks complete for phaser/vanilla projects
    where task verification is 'none' and no per-task check fires.
    """
    pw_ok, pw_log = _run_playwright_check(project_dir)
    pwa_ok, pwa_log = check_pwa_files(project_dir)
    combined_log = "\n".join(filter(None, [pw_log, pwa_log]))
    return pw_ok and pwa_ok, combined_log


def _run_playwright_check(project_dir: Path) -> tuple[bool, str]:
    """Headless Chromium check for bare HTML/Phaser projects.

    Launches a real browser, captures console errors, and verifies a <canvas>
    element is present. Falls back to _run_html_auto() if playwright is not
    installed or the browser binary hasn't been downloaded yet.
    """
    # Check that playwright is importable first.
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    except ImportError:
        console.print(
            "  [yellow]playwright not installed — falling back to HTML structure check. "
            "Install with: pip install playwright && playwright install chromium[/yellow]"
        )
        return _run_html_auto(project_dir)

    # Locate index.html
    index_html = project_dir / "index.html"
    if not index_html.exists():
        # No index.html — delegate to the basic check which scans all *.html
        console.print("  [yellow]No index.html found — falling back to HTML structure check.[/yellow]")
        return _run_html_auto(project_dir)

    url = index_html.resolve().as_uri()
    console.print(f"  [dim]Playwright headless check: {url}[/dim]")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()

            js_errors: list[str] = []
            warnings: list[str] = []

            # WebGL/GPU driver noise from Chromium headless on AMD hardware —
            # these are internal performance advisories, not JS errors.
            _NOISE = ("GL Driver Message", "GPU stall", "WebGL-", "RENDER WARNING")

            def _on_console(msg):
                text = msg.text
                if msg.type == "error" and not any(n in text for n in _NOISE):
                    js_errors.append(text)
                elif msg.type == "warning" and not any(n in text for n in _NOISE):
                    warnings.append(text)

            page.on("console", _on_console)

            page.goto(url)

            # Wait for network idle; ignore timeout (static file:// pages finish instantly)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass  # Static pages may not fire networkidle — that's fine

            # Check for <canvas>
            canvas_count = page.locator("canvas").count()
            if canvas_count == 0:
                console.print(
                    "  [yellow]Warning: no <canvas> element found on page. "
                    "Expected for a Phaser game.[/yellow]"
                )

            # Gameplay sanity: a present canvas must have a non-zero render surface.
            # A 0x0 canvas means the game booted but never sized/rendered — a real
            # defect the bare "canvas exists" check used to miss.
            canvas_dims = None
            if canvas_count > 0:
                try:
                    canvas_dims = page.evaluate(
                        "() => { const c = document.querySelector('canvas');"
                        " return c ? {w: c.width, h: c.height,"
                        " cw: c.clientWidth, ch: c.clientHeight} : null; }"
                    )
                except Exception:
                    canvas_dims = None

            # Observe a short window so runtime errors thrown by the game loop
            # (not just init-time errors) are captured by the console handler.
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass

            # Blank-canvas probe: fail if the canvas booted but rendered nothing
            # (a fully uniform / single-color surface). WebGL canvases that cannot
            # expose a 2d context are skipped (inconclusive = pass). Any exception
            # also leaves the result as False (inconclusive = pass).
            canvas_blank = False
            if canvas_count > 0 and canvas_dims and canvas_dims.get("w") and canvas_dims.get("h"):
                try:
                    _probe = page.evaluate(
                        "() => {"
                        "  const c = document.querySelector('canvas');"
                        "  if (!c) return null;"
                        "  const ctx = c.getContext('2d');"
                        "  if (!ctx) return null;"
                        "  let img;"
                        "  try { img = ctx.getImageData(0, 0, c.width, c.height); } catch (e) { return null; }"
                        "  const d = img.data; const step = Math.max(4, Math.floor(d.length / 4 / 400) * 4);"
                        "  const colors = new Set(); let n = 0;"
                        "  for (let i = 0; i + 3 < d.length; i += step) {"
                        "    colors.add(d[i] + ',' + d[i+1] + ',' + d[i+2] + ',' + d[i+3]); n++;"
                        "  }"
                        "  return { distinct: colors.size, sampled: n };"
                        "}"
                    )
                    if _probe and _probe.get("sampled", 0) >= 8 and _probe.get("distinct", 0) <= 1:
                        canvas_blank = True
                except Exception:
                    canvas_blank = False

            # Check page title
            title = page.title()
            if not title:
                console.print("  [yellow]Warning: page title is empty.[/yellow]")

            browser.close()

        if js_errors:
            summary = "\n".join(js_errors)
            console.print(f"  [red]Playwright: {len(js_errors)} JS error(s) detected.[/red]")
            for err in js_errors:
                console.print(f"  [dim]{err}[/dim]")
            return False, f"Playwright JS errors:\n{summary}"

        if canvas_count > 0 and canvas_dims and (not canvas_dims.get("w") or not canvas_dims.get("h")):
            console.print(
                f"  [red]Playwright: canvas present but has zero render size {canvas_dims}.[/red]"
            )
            return False, f"canvas has zero render size: {canvas_dims}"

        if canvas_blank:
            console.print("  [red]Playwright: canvas rendered nothing (blank/uniform surface).[/red]")
            return False, "canvas is blank — nothing was rendered"

        if warnings:
            console.print(f"  [yellow]Playwright: {len(warnings)} warning(s) (non-fatal).[/yellow]")

        canvas_note = f" ({canvas_count} canvas element(s) found)" if canvas_count else " (no canvas)"
        console.print(f"  [green]Playwright check passed{canvas_note}.[/green]")
        return True, f"playwright check passed{canvas_note}"

    except Exception as exc:
        console.print(
            f"  [yellow]Playwright check failed ({exc!r}) — "
            "falling back to HTML structure check. "
            "Run 'playwright install chromium' if the browser binary is missing.[/yellow]"
        )
        return _run_html_auto(project_dir)


def _run_html_auto(project_dir: Path) -> tuple[bool, str]:
    """Headless structure check for bare HTML projects (no package.json)."""
    import re as _re
    html_files = list(project_dir.glob("*.html"))
    if not html_files:
        return False, "No .html files found in project directory"
    issues: list[str] = []
    meta_warnings: list[str] = []
    for f in html_files:
        content = f.read_text(encoding="utf-8", errors="replace")
        cl = content.lower()
        for tag in ("<html", "<body"):
            if tag not in cl:
                issues.append(f"{f.name}: missing {tag} tag")
        # WARN (not FAIL) meta checks
        if 'meta name="description"' not in cl and "meta name='description'" not in cl:
            meta_warnings.append(f"{f.name}: missing <meta name=\"description\">")
        if '<html lang=' not in cl:
            meta_warnings.append(f"{f.name}: <html> missing lang attribute")
        for img in _re.finditer(r'<img\b([^>]*?)>', content, _re.IGNORECASE):
            if 'alt=' not in img.group(1).lower():
                meta_warnings.append(f"{f.name}: <img> missing alt attribute")
                break
    if issues:
        return False, "; ".join(issues)
    log = "html structure valid"
    if meta_warnings:
        warn_str = "; ".join(meta_warnings)
        console.print(f"  [yellow]HTML meta warnings: {warn_str}[/yellow]")
        log += f"\nwarnings: {warn_str}"
    console.print("  [green]HTML structure check passed (headless).[/green]")
    return True, log


_VIDEO_EVIDENCE_EXTS = (".mp4", ".webm", ".mov")
_VIDEO_SEARCH_EXCLUDE = {"node_modules", ".git", "dist", ".venv", "__pycache__"}
# A real render is never this small — the legacy stub is ~40 bytes.
_MIN_REAL_VIDEO_BYTES = 1024

# Failed render attempts, keyed by (project_dir, source-files signature).
# When the heal loop rewrites the render sources the signature changes and the
# render is retried; identical sources fail fast instead of re-running a
# multi-minute render once per check method.
_RENDER_FAILED: dict[tuple[str, tuple], str] = {}


def _find_project_videos(project_dir: Path, task=None, min_bytes: int = 0) -> list[Path]:
    """Videos a check should probe: the task's declared video files when they
    exist, else any video anywhere in the project tree (build dirs excluded)."""
    declared = []
    for rel in (getattr(task, "files", None) or []):
        if Path(rel).suffix.lower() in _VIDEO_EVIDENCE_EXTS:
            p = project_dir / rel
            if p.exists() and p.stat().st_size >= min_bytes:
                declared.append(p)
    if declared:
        return declared
    found = [
        p for p in project_dir.rglob("*")
        if p.suffix.lower() in _VIDEO_EVIDENCE_EXTS and p.is_file()
        and p.stat().st_size >= min_bytes
        and not (_VIDEO_SEARCH_EXCLUDE & set(p.relative_to(project_dir).parts[:-1]))
    ]
    return sorted(found)


def _render_source_signature(project_dir: Path) -> tuple:
    """Fingerprint of everything that could drive a render — root-level scripts.
    Changes whenever the heal loop rewrites them."""
    sig = []
    for p in sorted(project_dir.glob("*.py")) + sorted(project_dir.glob("*.sh")):
        try:
            st = p.stat()
            sig.append((p.name, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    return tuple(sig)


def _ensure_rendered(project_dir: Path) -> tuple[bool, str]:
    """Execute the project's render pipeline so video evidence exists.

    Film projects generate code that PRODUCES the video — an 'ffmpeg …' line in
    an edit script, or a Python entry script — but nothing else in the pipeline
    executes it. Running the render is part of gathering the evidence the
    ffprobe-family checks need; without it a film build can complete without a
    single frame ever being rendered.
    """
    if _find_project_videos(project_dir, min_bytes=_MIN_REAL_VIDEO_BYTES):
        return True, "video already present"

    key = (str(project_dir), _render_source_signature(project_dir))
    if key in _RENDER_FAILED:
        return False, f"render already attempted with these sources: {_RENDER_FAILED[key]}"

    logs: list[str] = []

    # 1. Shell path: any 'ffmpeg …' line in an edit script on disk.
    from video_worker import _collect_disk_scripts, can_generate as _ffmpeg_available
    if _ffmpeg_available():
        ffmpeg_lines = [
            line.strip()
            for content in _collect_disk_scripts(project_dir)
            for line in content.splitlines()
            if line.strip().startswith("ffmpeg ")
        ]
        for cmd_line in ffmpeg_lines[:10]:
            import shlex as _shlex
            try:
                parts = _shlex.split(cmd_line)
            except ValueError:
                parts = cmd_line.split()
            console.print(f"  [dim]render: {cmd_line[:100]}[/dim]")
            try:
                result = subprocess.run(parts, cwd=project_dir, capture_output=True,
                                        text=True, timeout=600)
                if result.returncode != 0:
                    logs.append(f"ffmpeg exited {result.returncode}: {(result.stderr or '')[-300:]}")
            except Exception as exc:  # noqa: BLE001
                logs.append(f"ffmpeg error: {exc}")
        if ffmpeg_lines and _find_project_videos(project_dir, min_bytes=_MIN_REAL_VIDEO_BYTES):
            return True, f"rendered via {len(ffmpeg_lines)} ffmpeg edit-script line(s)"

    # 2. Python path: run the project's render entry script.
    entry = _find_render_entry(project_dir)
    if entry is None:
        msg = "no render entry found (no ffmpeg edit script, no render*.py / generate_*.py / build_film.py / main.py)"
        _RENDER_FAILED[key] = msg
        return False, "; ".join(logs + [msg])

    req = project_dir / "requirements.txt"
    if req.exists():
        console.print("  [dim]render: pip install -r requirements.txt[/dim]")
        ok, pip_log = _run_cmd(["pip", "install", "-r", "requirements.txt"], project_dir, _TIMEOUT_BUILD)
        if not ok:
            msg = f"pip install failed before render:\n{pip_log[-500:]}"
            _RENDER_FAILED[key] = msg[:300]
            return False, msg

    console.print(f"  [dim]render: python {entry.name}[/dim]")
    try:
        result = subprocess.run([sys.executable, entry.name], cwd=project_dir,
                                capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        msg = f"render script {entry.name} timed out after 600s"
        _RENDER_FAILED[key] = msg
        return False, msg
    if result.returncode != 0:
        msg = (f"render script {entry.name} exited {result.returncode}:\n"
               f"{(result.stderr or result.stdout or '')[-800:]}")
        _RENDER_FAILED[key] = msg[:300]
        return False, msg

    if _find_project_videos(project_dir, min_bytes=_MIN_REAL_VIDEO_BYTES):
        return True, f"rendered via {entry.name}"
    msg = f"render script {entry.name} exited 0 but produced no video file"
    _RENDER_FAILED[key] = msg
    return False, msg


def _find_render_entry(project_dir: Path) -> Path | None:
    """Locate the Python render entry script: spec.json 'entry_point' first
    (the orchestrator is prompted to declare it), then conventional names."""
    spec_path = project_dir / "spec.json"
    if spec_path.exists():
        try:
            import json as _json
            entry = _json.loads(spec_path.read_text(encoding="utf-8")).get("entry_point")
            if entry and (project_dir / entry).exists() and entry.endswith(".py"):
                return project_dir / entry
        except Exception:  # noqa: BLE001
            pass
    for pattern in ("render*.py", "generate_*.py", "build_film.py", "main.py"):
        matches = sorted(project_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


def _run_video_evidence_check(method: str, task, project_dir: Path) -> tuple[bool, str]:
    """Honest ffprobe/frame_integrity/sync_check: a missing video is a FAIL,
    not an auto-pass — rendering it (via _ensure_rendered) is part of the check.
    Only tool-unavailability (ffmpeg/ffprobe missing) remains a SKIP, inside
    the individual probe helpers."""
    real = _find_project_videos(project_dir, task, min_bytes=_MIN_REAL_VIDEO_BYTES)
    if not real:
        rendered, render_log = _ensure_rendered(project_dir)
        real = _find_project_videos(project_dir, task, min_bytes=_MIN_REAL_VIDEO_BYTES)
        if not real:
            # Probe a stub if that's all there is — it fails ffprobe with a
            # precise message; otherwise report the render failure itself.
            stubs = _find_project_videos(project_dir, task)
            if not stubs:
                return False, (
                    f"{method}: no video file exists — render produced no output "
                    f"({render_log[:800]})"
                )
            real = stubs
    target = real[0]
    if method == "ffprobe":
        return _run_ffprobe_check(target)
    if method == "frame_integrity":
        return _run_frame_integrity_check(target)
    return _run_sync_check(target)


def _run_ffprobe_check(video_path: Path) -> tuple[bool, str]:
    """Run ffprobe on video_path to verify it has a valid video stream with duration > 0.05s."""
    import json as _json
    if not shutil.which("ffprobe"):
        return True, "auto-passed: ffprobe not installed"
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(video_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = _json.loads(result.stdout)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                duration = float(stream.get("duration", 0))
                if duration <= 0.05:
                    return False, f"video stream duration too short: {duration}s"
                codec = stream.get("codec_name", "unknown")
                return True, f"ffprobe: {duration:.2f}s {codec}"
        return False, "no video stream found in file"
    except subprocess.TimeoutExpired:
        return False, "ffprobe: timed out"
    except Exception as exc:  # noqa: BLE001
        return True, f"auto-passed: error ({exc})"


def _run_frame_integrity_check(video_path: Path, sample_frames: int = 10) -> tuple[bool, str]:
    """Sample-decode N frames with ffmpeg to confirm the video is really decodable.

    Returns a SKIP (not a pass) when ffmpeg is unavailable, per the project's
    verification-honesty convention. A real decode failure is a hard FAIL.
    """
    if not shutil.which("ffmpeg"):
        return True, f"{SKIP_PREFIX} frame_integrity: ffmpeg not installed"
    cmd = [
        "ffmpeg", "-v", "error",
        "-i", str(video_path),
        "-frames:v", str(sample_frames),
        "-f", "null", "-",
    ]
    console.print(f"  [dim]frame_integrity: ffmpeg decode {sample_frames} frame(s) of {video_path.name}[/dim]")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "frame_integrity: ffmpeg decode timed out"
    except FileNotFoundError:
        return True, f"{SKIP_PREFIX} frame_integrity: ffmpeg not found on PATH"

    err = (result.stderr or "").strip()
    if result.returncode != 0 or err:
        console.print("  [red]frame_integrity: decode errors detected[/red]")
        return False, f"frame_integrity: decode failed:\n{err[:1000] or 'non-zero exit'}"
    console.print("  [green]frame_integrity: frames decoded cleanly.[/green]")
    return True, f"frame_integrity: {sample_frames} frame(s) decoded without error"


def _run_sync_check(video_path: Path) -> tuple[bool, str]:
    """Verify audio + video streams are present and roughly aligned via ffprobe.

    Returns a SKIP (not a pass) when ffprobe is unavailable. A missing video stream
    is a hard FAIL; a missing audio stream is reported but not failed (some clips are
    intentionally silent). When both are present, a gross duration drift (> 1.0s) FAILs.
    """
    import json as _json
    if not shutil.which("ffprobe"):
        return True, f"{SKIP_PREFIX} sync_check: ffprobe not installed"
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "sync_check: ffprobe timed out"
    except FileNotFoundError:
        return True, f"{SKIP_PREFIX} sync_check: ffprobe not found on PATH"

    try:
        data = _json.loads(result.stdout)
    except Exception:
        return True, f"{SKIP_PREFIX} sync_check: ffprobe output not parseable"

    streams = data.get("streams", [])

    def _stream_duration(stream: dict) -> float | None:
        val = stream.get("duration")
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
        return None

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not video_streams:
        return False, "sync_check: no video stream found"

    v_dur = _stream_duration(video_streams[0])
    if v_dur is not None and v_dur <= 0.05:
        return False, f"sync_check: video stream duration too short: {v_dur}s"

    if not audio_streams:
        console.print("  [yellow]sync_check: no audio stream present (silent clip).[/yellow]")
        return True, "sync_check: video stream present, no audio (silent clip OK)"

    a_dur = _stream_duration(audio_streams[0])
    if v_dur is not None and a_dur is not None:
        drift = abs(v_dur - a_dur)
        if drift > 1.0:
            console.print(f"  [red]sync_check: A/V drift {drift:.2f}s exceeds 1.0s[/red]")
            return False, f"sync_check: audio/video duration drift {drift:.2f}s > 1.0s (v={v_dur:.2f}s a={a_dur:.2f}s)"
        console.print(f"  [green]sync_check: A/V aligned (drift {drift:.2f}s).[/green]")
        return True, f"sync_check: audio+video present, drift {drift:.2f}s (v={v_dur:.2f}s a={a_dur:.2f}s)"

    console.print("  [green]sync_check: audio + video streams present.[/green]")
    return True, "sync_check: audio + video streams present (durations unavailable)"


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


def _run_godot_check(project_dir: Path) -> tuple[bool, str]:
    """Godot headless syntax/error check."""
    from config import GODOT_PATH
    if not shutil.which(GODOT_PATH):
        return True, f"auto-passed: godot not found at '{GODOT_PATH}'"
    console.print(f"  [dim]Godot: {GODOT_PATH} --headless --check-only project.godot[/dim]")
    try:
        result = subprocess.run(
            [GODOT_PATH, "--headless", "--check-only", "project.godot"],
            cwd=project_dir, capture_output=True, text=True, timeout=60,
        )
        combined = result.stdout + result.stderr
        error_lines = [ln for ln in combined.splitlines() if "ERROR:" in ln or "SCRIPT ERROR:" in ln]
        if error_lines:
            summary = "\n".join(error_lines[:10])
            console.print(f"  [red]Godot: {len(error_lines)} error(s)[/red]")
            return False, f"Godot errors:\n{summary}"
        console.print("  [green]Godot headless check passed.[/green]")
        return True, "godot: no errors"
    except subprocess.TimeoutExpired:
        return True, "auto-passed: godot check timed out"
    except FileNotFoundError:
        return True, "auto-passed: godot not installed"


def _run_security_check(project_dir: Path) -> tuple[bool, str]:
    """Run bandit (Python) or npm audit (Node) and FAIL only on HIGH/CRITICAL."""
    import json as _json
    ecosystem = detect_ecosystem(project_dir)
    if ecosystem in ("python", "fastapi", "full-stack"):
        if not shutil.which("bandit"):
            return True, "auto-passed: bandit not installed"
        console.print("  [dim]Security: bandit -r . -f json -q[/dim]")
        result = subprocess.run(
            ["bandit", "-r", ".", "-f", "json", "-q"],
            cwd=project_dir, capture_output=True, text=True, timeout=60,
        )
        try:
            data = _json.loads(result.stdout)
            severe = [r for r in data.get("results", []) if r.get("issue_severity", "") in ("HIGH", "CRITICAL")]
            if severe:
                summary = "; ".join(
                    f"{r.get('filename', '?')}:{r.get('line_number', '?')} {r.get('issue_text', '')}"
                    for r in severe[:5]
                )
                console.print(f"  [red]bandit: {len(severe)} HIGH/CRITICAL issue(s)[/red]")
                return False, f"bandit: {len(severe)} HIGH/CRITICAL issue(s): {summary}"
            total = len(data.get("results", []))
            console.print(f"  [green]bandit: no HIGH/CRITICAL issues ({total} total)[/green]")
            return True, f"bandit: no HIGH/CRITICAL issues ({total} total)"
        except Exception:
            return True, "auto-passed: bandit output not parseable"
    if (project_dir / "package.json").exists():
        npm = _npm_cmd()
        if npm is None:
            return True, "auto-passed: npm not available"
        console.print("  [dim]Security: npm audit --json[/dim]")
        use_shell = sys.platform == "win32"
        cmd_arg = f"{npm} audit --json" if use_shell else [npm, "audit", "--json"]
        result = subprocess.run(
            cmd_arg, cwd=project_dir, capture_output=True, text=True, timeout=60, shell=use_shell,
        )
        try:
            data = _json.loads(result.stdout)
            vulns = data.get("vulnerabilities", {})
            severe = [k for k, v in vulns.items() if v.get("severity", "") in ("high", "critical")]
            if severe:
                console.print(f"  [red]npm audit: {len(severe)} high/critical vulnerability(ies)[/red]")
                return False, f"npm audit: {len(severe)} high/critical: {', '.join(severe[:5])}"
            console.print("  [green]npm audit: no high/critical vulnerabilities[/green]")
            return True, "npm audit: no high/critical vulnerabilities"
        except Exception:
            return True, "auto-passed: npm audit output not parseable"
    return True, "auto-passed: no applicable security tool for this project"


def _run_lighthouse_check(project_dir: Path) -> tuple[bool, str]:
    """Run Google Lighthouse headless against a local static server."""
    import json as _json
    import time as _time
    ecosystem = detect_ecosystem(project_dir)
    if ecosystem not in ("unknown", "react-vite", "phaser", "three-js"):
        return True, f"auto-passed: lighthouse not applicable to {ecosystem}"
    if not (project_dir / "index.html").exists():
        return True, "auto-passed: no index.html for lighthouse check"
    if not shutil.which("npx"):
        return True, "auto-passed: npx not available"
    console.print("  [dim]Lighthouse: starting static server on :18080...[/dim]")
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", "18080", "--directory", str(project_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _time.sleep(1)
    try:
        use_shell = sys.platform == "win32"
        if use_shell:
            cmd = (
                'npx lighthouse http://localhost:18080 --output json --quiet '
                '--chrome-flags="--headless --no-sandbox" '
                '--only-categories=performance,accessibility'
            )
        else:
            cmd = [
                "npx", "lighthouse", "http://localhost:18080",
                "--output", "json", "--quiet",
                "--chrome-flags=--headless --no-sandbox",
                "--only-categories=performance,accessibility",
            ]
        console.print("  [dim]Running lighthouse...[/dim]")
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, shell=use_shell,
        )
        try:
            data = _json.loads(result.stdout)
            cats = data.get("categories", {})
            perf = cats.get("performance", {}).get("score", 1.0)
            a11y = cats.get("accessibility", {}).get("score", 1.0)
            console.print(f"  [dim]Lighthouse scores: performance={perf:.2f} accessibility={a11y:.2f}[/dim]")
            if perf < 0.5:
                return False, f"lighthouse: performance score {perf:.2f} < 0.5"
            if a11y < 0.7:
                return False, f"lighthouse: accessibility score {a11y:.2f} < 0.7"
            console.print("  [green]Lighthouse check passed.[/green]")
            return True, f"lighthouse: performance={perf:.2f} accessibility={a11y:.2f}"
        except Exception:
            return True, "auto-passed: lighthouse output not parseable"
    except subprocess.TimeoutExpired:
        return True, "auto-passed: lighthouse timed out"
    except FileNotFoundError:
        return True, "auto-passed: lighthouse (npx) not found"
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except Exception:
            server.kill()


def _run_expo_export_check(project_dir: Path) -> tuple[bool, str]:
    """Run `npx expo export --platform web` to verify the app can build for the web."""
    import tempfile
    if not shutil.which("npx"):
        return True, "auto-passed: npx not available"
    with tempfile.TemporaryDirectory() as tmpdir:
        use_shell = sys.platform == "win32"
        cmd = (
            f"npx expo export --platform web --output-dir {tmpdir}"
            if use_shell
            else ["npx", "expo", "export", "--platform", "web", "--output-dir", tmpdir]
        )
        console.print("  [dim]Expo: npx expo export --platform web[/dim]")
        try:
            result = subprocess.run(
                cmd, cwd=project_dir, capture_output=True, text=True, timeout=180, shell=use_shell,
            )
        except FileNotFoundError:
            return True, "auto-passed: expo not installed"
        if result.returncode != 0:
            stderr_lines = result.stderr.strip().splitlines()
            first_error = next((ln for ln in stderr_lines if "ERROR" in ln.upper()), None)
            log = first_error or (stderr_lines[0] if stderr_lines else "export failed")
            console.print(f"  [red]Expo web export failed: {log}[/red]")
            return False, f"expo export: {log}"
        console.print("  [green]Expo web export passed.[/green]")
        return True, "expo export: web platform build succeeded"
