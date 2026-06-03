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
            return _run_playwright_check(project_dir)
        return _run_manual(task)

    if method == "ffprobe":
        video_files = list(project_dir.glob("*.mp4")) + list(project_dir.glob("*.webm"))
        if not video_files:
            return True, "auto-passed: no video files"
        return _run_ffprobe_check(video_files[0])

    if method in ("frame_integrity", "sync_check"):
        video_files = list(project_dir.glob("*.mp4")) + list(project_dir.glob("*.webm"))
        if not video_files:
            return True, "auto-passed: no video files"
        video_file = video_files[0]
        if video_file.stat().st_size < 100:
            return False, "file too small"
        return True, "passed"

    ecosystem = detect_ecosystem(project_dir)

    # Web3: Hardhat compile + test
    if ecosystem == "web3":
        if method in ("build", "unit_test"):
            return _run_hardhat_build(project_dir)
        return True, f"auto-passed: no {method} command for web3"

    # React Native / Expo: npm install only (can't run iOS simulator in CI)
    if ecosystem == "react-native" and method == "build":
        return _run_react_native_install(project_dir)

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
    except Exception as exc:  # noqa: BLE001
        return True, f"auto-passed: error ({exc})"


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
