from __future__ import annotations
import json
import ollama
from rich.console import Console

from config import WORKER_MODEL, OLLAMA_HOST

console = Console()

_SYSTEM_PROMPT = """\
You are a precise code-writing assistant in an automated pipeline.
You receive a single engineering task and write the exact file contents it requires.

Rules:
- Output ONLY a valid JSON object — no markdown, no prose, no explanation.
- The JSON must match this schema exactly:
  {"files": [{"path": "relative/path.ext", "content": "complete file content"}]}
- Every file listed in the task's "files" array must appear in your output.
- Write complete, working file contents. Never truncate, never use placeholders.
- Dependency files show what already exists on disk — do not re-emit them.
"""

_STACK_PROMPTS: dict[str, str] = {
    "vanilla": """\
Stack: vanilla HTML/CSS/JS (no build step)
- NEVER use ES module syntax: no import, no export, no type="module".
- All JavaScript must be a single plain <script src="..."> compatible file.
- Put all functions and logic in one file using var/let/const and regular functions.
- Use localStorage, fetch, and DOM APIs directly — no module bundling assumed.
- Styling: use Tailwind CSS via CDN — add <script src="https://cdn.tailwindcss.com"></script> to <head>.
- Apply Tailwind utility classes directly on HTML elements; avoid writing custom CSS files.
- Layout: use flex/grid utilities (flex, grid, gap-*, items-*, justify-*).
- Color: pick a coherent palette (e.g. slate + indigo, or zinc + emerald) and apply it consistently.
- All interactive states: include hover:, focus:, active: variants on buttons and inputs.
- Mobile-first: add sm: md: lg: breakpoints on layout-changing elements.
- Buttons must look like buttons: rounded corners, padding, background color, hover color change.
- Inputs must have visible borders, padding, focus ring (focus:ring-2 focus:ring-indigo-500).
""",

    "react-vite": """\
Stack: React + Vite + Tailwind CSS (npm build required)
- Use functional components with hooks (useState, useEffect, useCallback).
- File structure: src/App.jsx as root, src/components/ for reusable components, src/main.jsx as entry.
- Include vite.config.js (minimal: defineConfig with react plugin).
- Include package.json with react, react-dom, vite, @vitejs/plugin-react, tailwindcss, autoprefixer, postcss.
- Include tailwind.config.js (content: ["./index.html", "./src/**/*.{js,jsx}"]) and postcss.config.js.
- Include src/index.css with @tailwind base/components/utilities directives.
- All styling via Tailwind utility classes — no inline styles, no separate .css files (except index.css).
- ES modules ARE allowed: use import/export freely.
- Props: always destructure. Events: always use arrow functions in handlers.
- Keep components small (< 80 lines each). Extract repeated UI into a component file.
""",

    "fastapi": """\
Stack: Python + FastAPI + SQLite (uvicorn server required)
- Entry point: main.py with a FastAPI() app instance and uvicorn.run() guard.
- Database: use Python's built-in sqlite3 module, database file named app.db.
- Schema init: call a init_db() function at module level to CREATE TABLE IF NOT EXISTS.
- SQL strings: ALWAYS use double-quoted Python strings for SQL so single quotes inside (e.g. datetime('now')) don't break the string. Example: cursor.execute("CREATE TABLE ... DEFAULT (datetime('now'))")
- Routes: use @app.get/post/put/delete decorators. Return dicts (FastAPI auto-serializes).
- CORS: use app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]). NEVER instantiate CORSMiddleware directly as a variable.
- requirements.txt: fastapi, uvicorn[standard]. Nothing else unless strictly needed.
- Pydantic models: define a BaseModel for every POST/PUT request body.
- Error handling: raise HTTPException(status_code=..., detail="...") for known error cases.
- No ORM, no migrations — raw sqlite3 with parameterized queries (use ? placeholders, never f-strings for SQL).
- get_conn() dependency: must use yield (not return) so FastAPI closes the connection after each request.
""",

    "phaser": """\
Stack: Phaser 3 browser game (vanilla HTML + JS, no build step)
- Load Phaser 3 via CDN in index.html: <script src="https://cdn.jsdelivr.net/npm/phaser@3/dist/phaser.min.js"></script>
- Single game.js file loaded as a plain <script src="game.js"> (NOT type="module").
- Define a Phaser.Game config at the top of game.js: { type: Phaser.AUTO, width, height, scene: [SceneClass], parent: 'game-container' }.
- Scenes: define classes that extend Phaser.Scene with constructor() calling super('SceneName'), and implement preload(), create(), update().
- Use this.add.text(), this.add.graphics(), this.input.keyboard.createCursorKeys() etc. — all Phaser built-in APIs.
- No ES module syntax. No external JS files beyond game.js.
- index.html: minimal — a <div id="game-container"> and the two script tags.
- Game logic (score, lives, level) as class properties on the scene.
- Game over / restart: use this.scene.restart() or this.scene.start('GameOver').
""",
}


def execute_task(task, spec: dict, dependency_files: dict[str, dict[str, str]]) -> dict:
    """
    Ask the worker model to implement a task.
    Returns {"files": [{"path": ..., "content": ...}]}.
    Raises ValueError on malformed worker output.
    """
    arch = spec.get("architecture", {})
    stack = arch.get("stack", "vanilla")
    stack_instructions = _STACK_PROMPTS.get(stack, _STACK_PROMPTS["vanilla"])
    system_prompt = _SYSTEM_PROMPT + "\n" + stack_instructions

    payload = {
        "task": {
            "id": task.id,
            "type": task.type,
            "objective": task.objective,
            "files": task.files,
            "acceptance_criteria": task.acceptance_criteria,
        },
        "project_context": {
            "goal": spec.get("goal", ""),
            "stack": stack,
            "architecture": arch,
        },
        "existing_dependency_files": {
            tid: files for tid, files in dependency_files.items()
        },
    }

    client = ollama.Client(host=OLLAMA_HOST)

    response = client.chat(
        model=WORKER_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, indent=2)},
        ],
        format="json",
        options={"temperature": 0.15, "num_predict": 8192},
    )

    raw = response.message.content.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Worker returned invalid JSON: {exc}\n--- raw (first 600 chars) ---\n{raw[:600]}") from exc

    if not isinstance(parsed.get("files"), list):
        raise ValueError(f"Worker output is missing a 'files' list. Got keys: {list(parsed.keys())}")

    for entry in parsed["files"]:
        if not isinstance(entry.get("path"), str) or not isinstance(entry.get("content"), str):
            raise ValueError(f"Worker file entry missing 'path' or 'content': {entry}")
        entry["content"] = _fix_literal_newlines(entry["path"], entry["content"])
        _warn_if_truncated(entry["path"], entry["content"])

    return parsed


_TRUNCATION_MARKERS = ("...", "// TODO", "# TODO", "[truncated]", "/* ... */", "// ...")

def _warn_if_truncated(path: str, content: str) -> None:
    stripped = content.rstrip()
    if len(stripped) < 40:
        console.print(f"  [yellow]⚠ Worker file '{path}' is suspiciously short ({len(stripped)} chars)[/yellow]")
        return
    tail = stripped[-80:]
    for marker in _TRUNCATION_MARKERS:
        if marker in tail:
            console.print(f"  [yellow]⚠ Worker file '{path}' may be truncated — ends near: {marker!r}[/yellow]")


def _fix_literal_newlines(path: str, content: str) -> str:
    """Replace literal \\n and \\t sequences in code files with real whitespace.
    Workers sometimes emit backslash-n instead of a real newline inside generated code."""
    ext = path.rsplit(".", 1)[-1].lower()
    if ext not in ("js", "ts", "jsx", "tsx", "py", "html", "css"):
        return content
    if r"\n" not in content and r"\t" not in content:
        return content
    # Only fix \n and \t that appear outside of string literals (best-effort: replace all,
    # since a literal backslash-n in code is always a bug for these file types).
    fixed = content.replace(r"\n", "\n").replace(r"\t", "\t")
    if fixed != content:
        console.print(f"  [yellow]⚠ Fixed literal \\\\n/\\\\t sequences in '{path}'[/yellow]")
    return fixed
