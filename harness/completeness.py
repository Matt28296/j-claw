#!/usr/bin/env python3
"""Static completeness gate for generated source.

Deterministic, dependency-free structural checks that catch the failure class the
local worker reliably produces: scaffolding + data written, but the integration
sections (render / input / game-loop) left as empty stubs. Used by scheduler.py
(per task) and main.py (whole project) to turn a silent half-build into precise,
actionable failures that the existing retry/escalation + heal loop can act on.

Pure (file I/O only) and self-testing:
    PYTHONUTF8=1 python harness/completeness.py

Design bias: FALSE NEGATIVES over FALSE POSITIVES. A check only fires when it is
highly confident, so the gate never blocks legitimate code. Stacks/files it does
not understand return a clean pass.
"""
from __future__ import annotations

import re
from pathlib import Path

# Ecosystems whose primary output is plain HTML+JS that these checks understand.
# react-vite (.jsx/.tsx), python, etc. are intentionally skipped to avoid false
# positives on syntax these heuristics weren't written for.
_WEB_ECOSYSTEMS = {"vanilla", "phaser", "three-js", "unknown", None}

_JS_SUFFIXES = {".js", ".mjs"}
_HTML_SUFFIXES = {".html", ".htm"}

# A "banner" section comment, e.g.  // === RENDER ===   or   // --- INPUT ---
# Requires a named section (a bare rule of only ===/--- is not a banner).
_BANNER_RE = re.compile(
    r"""^[ \t]*
        (?://|/\*)                      # // or /* opener
        [ \t]*
        [=\-]{2,}                       # ==== or ----
        [ \t]*
        (?P<name>[A-Za-z][\w /\-]*?)    # section name
        [ \t]*
        (?:[=\-]{2,})?                  # optional closing rule
        [ \t]*
        (?:\*/)?                        # optional */
        [ \t]*$
    """,
    re.VERBOSE,
)

# function NAME( ... ) {
_FUNC_DECL_RE = re.compile(r"\bfunction\s+(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{")

# leading const/let/var NAME
_DECL_RE = re.compile(r"^[ \t]*(?P<kw>const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\b")

# A bare function call `name(` not preceded by a '.' (so method calls are excluded).
_CALL_RE = re.compile(r"(?<![.\w$])(?P<name>[a-z_$][\w$]*)\s*\(")

# Local asset references in HTML/JS (script/link/fetch/cache-lists).
_HTML_REF_RE = re.compile(
    r"""(?:src|href)\s*=\s*['"](?P<path>[^'"]+)['"]""", re.IGNORECASE
)
_STR_LITERAL_RE = re.compile(r"""['"](?P<path>[^'"\n]+\.(?:js|css|html|json|wav|mp3|ico|webmanifest))['"]""")

# JS keywords / builtins / DOM globals that are legitimately called as bare functions.
_CALL_ALLOWLIST = {
    # keywords that read like calls
    "if", "for", "while", "switch", "catch", "return", "function", "typeof",
    "instanceof", "new", "delete", "void", "do", "else", "await", "yield",
    "case", "in", "of", "throw", "super",
    # global functions
    "parseint", "parsefloat", "isnan", "isfinite", "settimeout", "setinterval",
    "cleartimeout", "clearinterval", "requestanimationframe",
    "cancelanimationframe", "fetch", "alert", "confirm", "prompt", "btoa",
    "atob", "encodeuricomponent", "decodeuricomponent", "structuredclone",
    "queuemicrotask", "addeventlistener", "removeeventlistener",
    # constructors / namespaces commonly used bare
    "array", "object", "string", "number", "boolean", "json", "math", "date",
    "map", "set", "weakmap", "weakset", "promise", "regexp", "symbol", "error",
    "float32array", "uint8array", "image", "audio", "path2d",
}


def check_completeness(
    files: dict[str, str] | None = None,
    project_dir: Path | None = None,
    ecosystem: str | None = None,
) -> tuple[bool, list[str]]:
    """Return (passed, issues) for the completeness of generated web/game source.

    Scope is either an in-memory {path: content} map (per-task, from the worker
    output) or a project directory (whole-project). Only HTML/JS for web/game
    ecosystems is analysed; everything else passes clean.
    """
    if ecosystem is not None and ecosystem not in _WEB_ECOSYSTEMS:
        return True, []

    sources = _collect_sources(files, project_dir)
    if not sources:
        return True, []

    issues: list[str] = []

    # Build the set of files that physically exist (for asset-reference checks).
    # Stored as lowercased project-relative posix paths so the asset check is
    # EXACT (a path mismatch like root 'AudioManager.js' vs 'js/AudioManager.js'
    # is caught) but case-insensitive (Windows filesystems are).
    existing_rel: set[str] = set()
    if project_dir is not None and project_dir.exists():
        for p in project_dir.rglob("*"):
            if p.is_file():
                existing_rel.add(p.relative_to(project_dir).as_posix().lower())

    for fname, content in sources.items():
        suffix = Path(fname).suffix.lower()
        if suffix in _HTML_SUFFIXES:
            blocks = _inline_scripts(content)
            combined = "\n".join(blocks)
            for block in blocks:
                issues += _empty_sections(block, fname)
                issues += _empty_functions(block, fname)
            issues += _duplicate_decls(combined, fname)
            issues += _called_but_undefined(combined, fname)
            issues += _missing_html_assets(content, fname, existing_rel, project_dir)
        elif suffix in _JS_SUFFIXES:
            issues += _empty_sections(content, fname)
            issues += _empty_functions(content, fname)
            issues += _duplicate_decls(content, fname)
            issues += _called_but_undefined(content, fname)
            issues += _missing_js_assets(content, fname, existing_rel)

    # De-dupe while preserving order.
    seen: set[str] = set()
    unique = [i for i in issues if not (i in seen or seen.add(i))]
    return (not unique), unique


# ── source collection ─────────────────────────────────────────────────────────

def _collect_sources(
    files: dict[str, str] | None, project_dir: Path | None
) -> dict[str, str]:
    out: dict[str, str] = {}
    if files:
        for path, content in files.items():
            if Path(path).suffix.lower() in _HTML_SUFFIXES | _JS_SUFFIXES:
                out[path] = content
    elif project_dir is not None and project_dir.exists():
        for suffix in _HTML_SUFFIXES | _JS_SUFFIXES:
            for p in project_dir.rglob(f"*{suffix}"):
                # Skip dependency/build dirs.
                if any(part in ("node_modules", "dist", "build", ".git") for part in p.parts):
                    continue
                try:
                    out[p.relative_to(project_dir).as_posix()] = p.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except Exception:
                    pass
    return out


def _inline_scripts(html: str) -> list[str]:
    """Return the bodies of inline <script> blocks (external src= scripts skipped)."""
    blocks: list[str] = []
    for m in re.finditer(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
                         html, re.DOTALL | re.IGNORECASE):
        if "src=" in m.group("attrs").lower():
            continue
        blocks.append(m.group("body"))
    return blocks


# ── checks ─────────────────────────────────────────────────────────────────────

def _is_all_noncode(body: list[str]) -> bool:
    """True when every line is blank or a // line-comment (i.e. no code)."""
    for ln in body:
        s = ln.strip()
        if s == "" or s.startswith("//"):
            continue
        return False
    return True


def _empty_sections(block: str, fname: str) -> list[str]:
    """Flag a section banner followed by no code before the next banner / EOF."""
    lines = block.splitlines()
    banners: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        m = _BANNER_RE.match(ln)
        if m:
            banners.append((i, m.group("name").strip()))

    issues: list[str] = []
    for idx, (ln_i, name) in enumerate(banners):
        start = ln_i + 1
        end = banners[idx + 1][0] if idx + 1 < len(banners) else len(lines)
        if _is_all_noncode(lines[start:end]):
            issues.append(
                f'{fname}: section "{name}" is an empty stub — no code after the banner'
            )
    return issues


def _body_is_empty(body: str) -> bool:
    b = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    b = re.sub(r"//[^\n]*", "", b)
    return b.strip() == ""


def _empty_functions(js: str, fname: str) -> list[str]:
    """Flag `function NAME(...) { }` with an empty (flat) body. Nested-brace bodies
    are skipped (can't determine emptiness without a real parser)."""
    issues: list[str] = []
    for m in _FUNC_DECL_RE.finditer(js):
        name = m.group("name")
        open_idx = m.end() - 1  # position of '{'
        close = js.find("}", open_idx + 1)
        nxt_open = js.find("{", open_idx + 1)
        if close == -1:
            continue
        if nxt_open != -1 and nxt_open < close:
            continue  # nested body — skip
        if _body_is_empty(js[open_idx + 1:close]):
            issues.append(f"{fname}: function {name}() has an empty body")
    return issues


def _duplicate_decls(js: str, fname: str) -> list[str]:
    """Flag the same const/let name declared 2+ times at brace-depth 0 (SyntaxError)."""
    depth = 0
    counts: dict[str, int] = {}
    for line in js.splitlines():
        if depth == 0:
            m = _DECL_RE.match(line)
            if m and m.group("kw") in ("const", "let"):
                counts[m.group("name")] = counts.get(m.group("name"), 0) + 1
        depth += line.count("{") - line.count("}")
        if depth < 0:
            depth = 0
    return [
        f'{fname}: top-level "{n}" declared {c} times (duplicate const/let → SyntaxError)'
        for n, c in counts.items() if c > 1
    ]


def _strip_comments_strings(js: str) -> str:
    """Blank out comments and string/template literals so prose inside them can't
    be mistaken for code (e.g. a JSDoc line `world X position (pixels)` must not
    look like a call to `position(`). Best-effort, regex-based."""
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.DOTALL)   # block comments
    js = re.sub(r"//[^\n]*", " ", js)                       # line comments
    js = re.sub(r"'(?:\\.|[^'\\])*'", "''", js)             # single-quoted strings
    js = re.sub(r'"(?:\\.|[^"\\])*"', '""', js)             # double-quoted strings
    js = re.sub(r"`(?:\\.|[^`\\])*`", "``", js)             # template literals
    return js


def _called_but_undefined(js: str, fname: str) -> list[str]:
    """Conservatively flag bare camelCase calls that are never defined anywhere.

    Comments and string literals are stripped FIRST — prose like `position (px)`
    in a JSDoc block must never be read as a call (this exact false positive
    permanently failed a valid Player.js task and cascade-blocked a whole build)."""
    code = _strip_comments_strings(js)
    defined: set[str] = set()
    for m in _FUNC_DECL_RE.finditer(code):
        defined.add(m.group("name").lower())
    # const/let/var NAME = (... arrow or function)
    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", code):
        defined.add(m.group(1).lower())
    # object-method shorthand:  NAME(...) {   and   NAME: function
    for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*:\s*(?:function|\()", code):
        defined.add(m.group(1).lower())
    for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", code):
        defined.add(m.group(1).lower())

    missing: list[str] = []
    seen: set[str] = set()
    for m in _CALL_RE.finditer(code):
        name = m.group("name")
        low = name.lower()
        if low in _CALL_ALLOWLIST or low in defined or low in seen:
            continue
        # Only flag names that look like user functions (have a lowercase start and
        # at least one interior uppercase or are multi-char identifiers used as calls).
        if len(name) < 3:
            continue
        seen.add(low)
        missing.append(name)
    # Keep the report tight — too many means our heuristic is probably wrong.
    if 1 <= len(missing) <= 6:
        return [
            f"{fname}: function {n}() is called but never defined" for n in missing
        ]
    return []


def _ref_is_local(path: str) -> bool:
    p = path.strip()
    if not p or p.startswith(("http://", "https://", "//", "data:", "#", "mailto:")):
        return False
    return True


def _asset_exists(path: str, existing_rel: set[str]) -> bool:
    """EXACT (case-insensitive) project-relative match. No basename fallback, so a
    reference to a file that exists only under a different directory (a 404 in the
    browser) is correctly reported as missing."""
    norm = path.split("?")[0].split("#")[0].lstrip("/")
    if norm.startswith("./"):
        norm = norm[2:]
    return norm.lower() in existing_rel


def _missing_html_assets(
    html: str, fname: str, existing_rel: set[str], project_dir: Path | None,
) -> list[str]:
    if project_dir is None:
        return []  # can't verify existence without a project dir
    issues: list[str] = []
    for m in _HTML_REF_RE.finditer(html):
        ref = m.group("path")
        if _ref_is_local(ref) and not _asset_exists(ref, existing_rel):
            issues.append(f"{fname}: references missing local file '{ref}'")
    return issues


def _missing_js_assets(
    js: str, fname: str, existing_rel: set[str],
) -> list[str]:
    if not existing_rel:
        return []  # per-task scope without a project dir — skip
    issues: list[str] = []
    seen: set[str] = set()
    for m in _STR_LITERAL_RE.finditer(js):
        ref = m.group("path")
        if ref in seen:
            continue
        seen.add(ref)
        if _ref_is_local(ref) and not _asset_exists(ref, existing_rel):
            issues.append(f"{fname}: references missing local file '{ref}'")
    return issues


# ── self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 1. Empty section banner (the Pac-Man failure) → FAIL.
    broken = {
        "index.html": (
            "<html><body><script>\n"
            "const W = 10;\n"
            "function makeGhost(n){ return {n}; }\n"
            "// === RENDER ===\n"
            "\n"
            "// === GAME LOOP ===\n"
            "</script></body></html>"
        )
    }
    ok, issues = check_completeness(files=broken, ecosystem="vanilla")
    assert not ok, "should fail on empty sections"
    assert any("RENDER" in i for i in issues), issues
    assert any("GAME LOOP" in i for i in issues), issues

    # 2. Complete file → PASS (no false positives).
    good = {
        "index.html": (
            "<html><body><canvas></canvas><script>\n"
            "const W = 10;\n"
            "let score = 0;\n"
            "function draw(){ ctx.fillRect(0,0,W,W); }\n"
            "// === GAME LOOP ===\n"
            "function loop(){ draw(); requestAnimationFrame(loop); }\n"
            "loop();\n"
            "</script></body></html>"
        )
    }
    ok, issues = check_completeness(files=good, ecosystem="vanilla")
    assert ok, f"clean file should pass, got: {issues}"

    # 3. Empty function body → FAIL.
    ok, issues = check_completeness(
        files={"g.js": "function drawPacman(ctx) {  }\n"}, ecosystem="vanilla"
    )
    assert not ok and any("drawPacman" in i for i in issues), issues

    # 4. Duplicate top-level const/let → FAIL.
    ok, issues = check_completeness(
        files={"d.js": "let comboCounter = 0;\nfunction f(){ let i=0; }\nconst comboCounter = 1;\n"},
        ecosystem="vanilla",
    )
    assert not ok and any("comboCounter" in i for i in issues), issues

    # 5. Loop var reused across functions must NOT false-positive.
    ok, issues = check_completeness(
        files={"l.js": "function a(){ let i=0; }\nfunction b(){ let i=1; }\n"},
        ecosystem="vanilla",
    )
    assert ok, f"per-function loop vars should not be duplicates, got: {issues}"

    # 6. Non-web ecosystem → always pass.
    ok, issues = check_completeness(files=broken, ecosystem="fastapi")
    assert ok and not issues

    # 7. Called-but-undefined → FAIL.
    ok, issues = check_completeness(
        files={"c.js": "function eatPellet(){ activatePower(); }\n"}, ecosystem="vanilla"
    )
    assert not ok and any("activatePower" in i for i in issues), issues

    # 8. Prose inside comments must NOT be read as calls — regression for the JSDoc
    #    "world X position (pixels)" false positive that cascade-failed a real build.
    ok, issues = check_completeness(
        files={"e.js": (
            "/** Initial world X position (pixels). Texture before instantiation (or blank).\n"
            " * Time since last frame (milliseconds). */\n"
            "function update(){ return 1; }\n"
        )},
        ecosystem="vanilla",
    )
    assert ok, f"comment prose must not be flagged as calls, got: {issues}"

    print("completeness self-test passed ✓")
