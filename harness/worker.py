from __future__ import annotations
import json
import re
import shutil
import subprocess
import time
import ollama
from rich.console import Console

from config import WORKER_MODEL, WORKER_PROVIDER, WORKER_FALLBACKS, LOCAL_FIRST_TASK_TYPES, OLLAMA_HOST, GROQ_API_KEY, OPENROUTER_API_KEY

console = Console()

_SYSTEM_PROMPT = """\
You are a precise code-writing assistant in an automated pipeline.
You receive a single engineering task and write the exact file contents it requires.

CRITICAL — JSON STRING ESCAPING (violations cause pipeline failures):
Inside every "content" value you MUST escape these characters:
  "  →  \"     (double quote — most common mistake)
  \  →  \\     (backslash)
  newline → \n  (the two-char sequence backslash-n, NOT a real newline)
  tab → \t
Example of correct output for a Python file:
  {"files": [{"path": "main.py", "content": "def greet(name):\n    return {\"hello\": name}\n"}]}
Example of WRONG output (bare newlines and unescaped quotes break JSON):
  {"files": [{"path": "main.py", "content": "def greet(name):
    return {"hello": name}
"}]}

Rules:
- Output ONLY a valid JSON object — no markdown, no prose, no explanation.
- The JSON must match this schema exactly:
  {"files": [{"path": "relative/path.ext", "content": "complete file content"}]}
- Every file listed in the task's "files" array must appear in your output.
- Write complete, working file contents. Never truncate, never use placeholders.
- Dependency files show what already exists on disk — do not re-emit them.
- If "previous_attempt_error" is present in the payload, a prior attempt FAILED with that
  error. Study it carefully and fix the root cause — do not repeat the same mistake.
  Common causes: invalid JSON escaping (unescaped " or \n in content), missing imports,
  wrong file paths, syntax errors.
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

═══ CRITICAL FILE STRUCTURE — Vite requires these exact files ═══
index.html        → MUST be at project ROOT (not inside src/)
src/main.jsx      → entry point imported by index.html
src/App.jsx       → root component
src/index.css     → global styles with Tailwind directives

═══ REQUIRED index.html (root level) ═══
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>App Title</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>

═══ REQUIRED src/main.jsx ═══
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
ReactDOM.createRoot(document.getElementById('root')).render(<React.StrictMode><App /></React.StrictMode>)

═══ REQUIRED vite.config.js ═══
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
export default defineConfig({ plugins: [react()] })

═══ REQUIRED package.json scripts ═══
"scripts": { "dev": "vite", "build": "vite build", "preview": "vite preview" }

═══ REQUIRED tailwind.config.js ═══
export default { content: ['./index.html', './src/**/*.{js,jsx}'], theme: { extend: {} }, plugins: [] }

═══ REQUIRED postcss.config.js ═══
export default { plugins: { tailwindcss: {}, autoprefixer: {} } }

═══ REQUIRED src/index.css ═══
@tailwind base;
@tailwind components;
@tailwind utilities;

═══ CODING RULES ═══
- Functional components with hooks (useState, useEffect, useCallback)
- Always destructure props. Arrow functions for event handlers.
- All styling via Tailwind utility classes only — no inline styles
- Keep components under 80 lines. Extract repeated UI into components.
- ES modules (import/export) are fully allowed — this is NOT a plain script tag environment
- Use react-router-dom for routing when multiple pages are needed
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

═══ CRITICAL FILE STRUCTURE — breaking this order breaks the game ═══
Write game.js in this exact order:
  1. All plain functions and constructor-function classes (helpers, character logic, etc.)
  2. All Phaser.Scene subclasses (class TitleScene extends Phaser.Scene { ... })
  3. LAST: the config object and new Phaser.Game(config) call

NEVER put `var config = {...}` or `new Phaser.Game(config)` before the scene classes —
class declarations are not hoisted and will cause ReferenceError.

═══ PHYSICS — creating shapes with physics bodies ═══
// Correct pattern for a physics rectangle (platform or player):
var obj = this.add.rectangle(x, y, width, height, colorHex);
this.physics.add.existing(obj, isStatic);  // isStatic=true for platforms, false for players
obj.body.setGravityY(300);                 // extra gravity on top of world gravity (dynamic only)
obj.body.setCollideWorldBounds(false);     // true to prevent leaving screen

// NEVER use this.physics.add.rectangle() — it does not exist in Phaser 3.

Arcade physics config (always include):
  physics: { default: 'arcade', arcade: { gravity: { y: 500 }, debug: false } }

Colliders:
  this.physics.add.collider(dynamicObj, staticObj);

Check if touching ground:
  obj.body.blocked.down  // true when resting on something below

═══ KEYBOARD INPUT ═══
// Named key bindings (preferred over createCursorKeys for multi-player):
var keys = this.input.keyboard.addKeys({ left: 'A', right: 'D', up: 'W', atk: 'F' });
keys.left.isDown          // held
Phaser.Input.Keyboard.JustDown(keys.up)   // single press this frame

═══ GRAPHICS (drawing each frame) ═══
var gfx = this.add.graphics();   // create once in create()
// In update(), clear and redraw:
gfx.clear();
gfx.fillStyle(0xff0000, 1.0);
gfx.fillRect(x - w/2, y - h/2, w, h);   // rectangle centred on x,y
gfx.fillCircle(cx, cy, radius);

═══ SCENE TRANSITIONS ═══
this.scene.start('SceneName');               // no data
this.scene.start('WinScene', { winner: 'P1' });  // with data
// Receiving data: create(data) { var w = data.winner; }

═══ SYNTAX RULES — these mistakes crash the game ═══
- NEVER write obj..method() — double dot is a syntax error
- NEVER use import / export — no ES modules
- NEVER use type="module" on script tags
- Every opening { must have a matching closing }
- Check all method chains for accidental double dots before finishing

═══ REQUIRED index.html STRUCTURE ═══
<script src="https://cdn.jsdelivr.net/npm/phaser@3/dist/phaser.min.js"></script>
<script src="game.js"></script>
<div id="game-container"></div>
Both scripts as plain tags — NOT type="module".

═══ COMPLETE game.js SKELETON (fill in the bodies) ═══
// --- helper functions / classes here ---

class TitleScene extends Phaser.Scene {
  constructor() { super('TitleScene'); }
  create() { /* title UI, keyboard listener to start game */ }
}
class GameScene extends Phaser.Scene {
  constructor() { super('GameScene'); }
  create() { /* physics, players, platform, HUD */ }
  update(time, delta) { /* input, physics checks, blast zones, HUD refresh */ }
}
class WinScene extends Phaser.Scene {
  constructor() { super('WinScene'); }
  create(data) { /* show winner, restart listener */ }
}
// config and game instantiation ALWAYS LAST:
var config = {
  type: Phaser.AUTO, width: 800, height: 500, parent: 'game-container',
  physics: { default: 'arcade', arcade: { gravity: { y: 500 }, debug: false } },
  scene: [TitleScene, GameScene, WinScene]
};
var game = new Phaser.Game(config);
""",
}


# ── Ollama (local) ────────────────────────────────────────────────────────────

_CUDA_ERROR_PHRASES = (
    "cuda error",
    "llama runner process has terminated",
    "shared object initialization failed",
)
_CUDA_RETRIES = ((5, False), (15, True), (10, False))  # (delay_s, restart_ollama)


def _is_cuda_crash(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _CUDA_ERROR_PHRASES)


def _restart_ollama() -> None:
    console.print("  [yellow]  Restarting Ollama…[/yellow]")
    subprocess.run(["taskkill", "/f", "/im", "ollama.exe"], capture_output=True)
    time.sleep(2)
    exe = shutil.which("ollama") or "ollama"
    subprocess.Popen(
        [exe, "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _call_ollama(system_prompt: str, payload_json: str) -> str:
    client = ollama.Client(host=OLLAMA_HOST)
    last_exc: Exception | None = None
    retries = ((0, False),) + _CUDA_RETRIES
    for attempt, (delay, do_restart) in enumerate(retries):
        if delay:
            if do_restart:
                _restart_ollama()
            console.print(
                f"  [yellow]⚡ CUDA crash — waiting {delay}s before retry "
                f"({attempt}/{len(_CUDA_RETRIES)})…[/yellow]"
            )
            time.sleep(delay)
        try:
            response = client.chat(
                model=WORKER_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": payload_json},
                ],
                format="json",
                options={"temperature": 0.15, "num_predict": 8192},
            )
            return response.message.content.strip(), f"ollama:{WORKER_MODEL}"
        except Exception as exc:
            if _is_cuda_crash(exc):
                last_exc = exc
                continue
            raise
    raise RuntimeError(
        f"Ollama CUDA crash persisted after {len(_CUDA_RETRIES)} retries — "
        f"check GPU drivers or run: ollama serve\n{last_exc}"
    ) from last_exc


# ── Groq ──────────────────────────────────────────────────────────────────────

def _call_groq(system_prompt: str, payload_json: str) -> str:
    from groq import Groq, RateLimitError
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set. Add it to harness/.env.")
    client = Groq(api_key=GROQ_API_KEY)
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=WORKER_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": payload_json},
                ],
                response_format={"type": "json_object"},
                temperature=0.15,
                max_tokens=8192,
            )
            return response.choices[0].message.content.strip(), f"groq:{response.model}"
        except RateLimitError:
            if attempt < 2:
                wait = 30 * (attempt + 1)
                console.print(f"  [yellow]Groq rate limit — waiting {wait}s…[/yellow]")
                time.sleep(wait)
            else:
                raise


# ── OpenRouter ───────────────────────────────────────────────────────────────

def _call_openrouter(system_prompt: str, payload_json: str) -> str:
    from openai import OpenAI, RateLimitError
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set. Add it to harness/.env.")
    client = OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        default_headers={"X-Title": "J-Claw"},
    )
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=WORKER_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": payload_json},
                ],
                response_format={"type": "json_object"},
                temperature=0.15,
                max_tokens=8192,
            )
            return response.choices[0].message.content.strip(), f"openrouter:{response.model}"
        except RateLimitError:
            if attempt < 2:
                wait = 30 * (attempt + 1)
                console.print(f"  [yellow]OpenRouter rate limit — waiting {wait}s…[/yellow]")
                time.sleep(wait)
            else:
                raise


# ── Provider dispatch + fallback chain ───────────────────────────────────────

_RETRYABLE_PHRASES = ("rate limit", "429", "503", "overloaded", "temporarily", "null content", "empty choices")

def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _RETRYABLE_PHRASES)


def _call_provider(provider: str, model: str, system_prompt: str, payload_json: str) -> tuple[str, str]:
    """Dispatch a single call to the given provider+model. Returns (raw, model_used)."""
    if provider == "groq":
        # Temporarily override WORKER_MODEL for this call
        import groq as _groq
        from groq import RateLimitError
        client = _groq.Groq(api_key=GROQ_API_KEY)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": payload_json}],
            response_format={"type": "json_object"},
            temperature=0.15,
            max_tokens=8192,
        )
        return resp.choices[0].message.content.strip(), f"groq:{resp.model}"
    elif provider == "openrouter":
        from openai import OpenAI
        client = OpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1",
                        default_headers={"X-Title": "J-Claw"})
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": payload_json}],
            response_format={"type": "json_object"},
            temperature=0.15,
            max_tokens=8192,
        )
        if not resp.choices:
            raise ValueError("Model returned empty choices — likely hit rate limit or content filter")
        content = resp.choices[0].message.content
        if content is None:
            raise ValueError("Model returned null content — likely hit token limit or content filter")
        return content.strip(), f"openrouter:{resp.model}"
    else:
        return _call_ollama(system_prompt, payload_json)


def _build_chain(task_type: str) -> list[tuple[str, str]]:
    """Build provider chain based on task complexity."""
    primary = (WORKER_PROVIDER, WORKER_MODEL)
    fallbacks = list(WORKER_FALLBACKS)
    all_providers = [primary] + fallbacks

    if task_type in LOCAL_FIRST_TASK_TYPES:
        local = [(p, m) for p, m in all_providers if p == "ollama"]
        cloud = [(p, m) for p, m in all_providers if p != "ollama"]
        chain = local + cloud
        if local:
            console.print(f"  [dim]task type '{task_type}' → local-first routing[/dim]")
        return chain if chain else all_providers
    return all_providers


def _call_with_fallback(system_prompt: str, payload_json: str, task_type: str = "") -> tuple[str, str]:
    """Try primary provider then each fallback in order on retryable errors."""
    chain = _build_chain(task_type)
    last_exc: Exception | None = None
    for provider, model in chain:
        try:
            console.print(f"  [dim]→ trying {provider}:{model}[/dim]")
            return _call_provider(provider, model, system_prompt, payload_json)
        except Exception as exc:
            if _is_retryable(exc):
                last_exc = exc
                console.print(f"  [yellow]  {provider}:{model} unavailable — trying next fallback…[/yellow]")
                continue
            raise  # non-retryable errors propagate immediately
    raise RuntimeError(
        f"All worker providers exhausted.\n"
        f"Chain tried: {[f'{p}:{m}' for p, m in chain]}\n"
        f"Last error: {last_exc}"
    ) from last_exc


# ── JSON repair ──────────────────────────────────────────────────────────────

def _escape_ctrl_in_strings(raw: str) -> str:
    """Single-pass: escape raw control chars (\n \r \t and others) inside JSON string literals."""
    result = []
    in_string = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '\\' and in_string and i + 1 < len(raw):
            # Already-escaped sequence — keep both chars as-is
            result.append(ch)
            result.append(raw[i + 1])
            i += 2
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
        elif in_string:
            if ch == '\n':
                result.append('\\n')
            elif ch == '\r':
                result.append('\\r')
            elif ch == '\t':
                result.append('\\t')
            elif ord(ch) < 0x20:
                result.append(' ')
            else:
                result.append(ch)
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def _repair_json(raw: str) -> str:
    """Best-effort repair of common worker JSON formatting mistakes."""
    # Strip markdown code fences (some models wrap output in ```json ... ```)
    raw = raw.strip()
    raw = re.sub(r'^```(?:json)?\s*\n?', '', raw)
    raw = re.sub(r'\n?```\s*$', '', raw)
    raw = raw.strip()

    # Pattern 1: {"files':[ — apostrophe instead of quote
    raw = re.sub(r'\{"files\'\s*:\s*\[', '{"files":[', raw)
    # Pattern 2: top-level key with apostrophe — {'key': → {"key":
    raw = re.sub(r"^\{'(\w+)':", r'{"\1":', raw)
    # Pattern 3: escaped quote on key — {"path\": → {"path":
    raw = re.sub(r'"(\w+)\\":', r'"\1":', raw)
    # Pattern 4: non-whitespace control chars outside strings
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', raw)
    # Pattern 4b: escape raw \n \r \t inside JSON string values (single pass, handles many at once)
    raw = _escape_ctrl_in_strings(raw)
    # Pattern 5: missing comma between object entries — "value"\n  "key" → "value",\n  "key"
    raw = re.sub(r'("|\d|true|false|null)(\s*\n\s+)"', r'\1,\2"', raw)
    # Pattern 6: missing comma between array elements — }\n  { → },\n  {
    raw = re.sub(r'(\})\s*\n(\s+\{)', r'},\n\2', raw)
    # Pattern 7: leading comma in array — [  , → [
    raw = re.sub(r'\[\s*,\s*', '[', raw)
    # Pattern 8: trailing comma before } or ]
    raw = re.sub(r',\s*([}\]])', r'\1', raw)
    # Pattern 11: double-escaped value delimiters — model wraps values in \" instead of "
    # e.g. {"path":\"file.json\"} → {"path":"file.json"}
    if ':\\"' in raw or '[\\"' in raw or '{\\"' in raw:
        raw = re.sub(r'([:\[,{])\\"', r'\1"', raw)  # opening: :\" {\" → :" {"
        raw = re.sub(r'\\"([,}\]])', r'"\1', raw)    # closing: \", → ",
    # Pattern 9: unescaped closing quote on embedded JSON keys inside content strings
    # Model outputs: \"name": instead of \"name\":
    # This makes the outer JSON string close early, causing 'Expecting , delimiter'
    raw = re.sub(r'(\\"[^"\\:,\[\]{}\s]+)":', r'\1\":', raw)

    # Pattern 10: Python dict literal syntax — try ast.literal_eval as fallback
    # Model sometimes outputs {'key': 'value'} (single quotes) instead of JSON
    if not _is_valid_json(raw):
        try:
            import ast
            parsed = ast.literal_eval(raw)
            raw = json.dumps(parsed)
        except (ValueError, SyntaxError):
            pass

    # Iterative position-based repair: use Python's exact error location
    # to fix any remaining structural issues the regex pass missed.
    raw = _repair_json_iterative(raw)
    return raw


def _is_valid_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except json.JSONDecodeError:
        return False


def _repair_json_iterative(raw: str) -> str:
    """Fix remaining JSON errors by inserting/removing chars at the exact parse position."""
    if not raw.lstrip().startswith(('{', '[')):
        return raw  # not JSON-shaped, don't corrupt it further
    for _ in range(30):
        try:
            json.loads(raw)
            return raw
        except json.JSONDecodeError as exc:
            msg = str(exc)
            pos = exc.pos
            if pos is None or pos >= len(raw):
                break

            if "Expecting ':' delimiter" in msg:
                # Missing colon between key and value — insert it
                insert_at = pos
                while insert_at > 0 and raw[insert_at - 1] in ' \t\n\r':
                    insert_at -= 1
                raw = raw[:insert_at] + ':' + raw[insert_at:]

            elif "Expecting ',' delimiter" in msg:
                # Python found a key/value where a comma was expected.
                # Insert comma after the last non-whitespace char before pos.
                insert_at = pos
                while insert_at > 0 and raw[insert_at - 1] in ' \t\n\r':
                    insert_at -= 1
                raw = raw[:insert_at] + ',' + raw[insert_at:]

            elif "Expecting value" in msg:
                # Likely a leading comma: [  , { → check prev non-ws is ','
                check = pos - 1
                while check >= 0 and raw[check] in ' \t\n\r':
                    check -= 1
                if check >= 0 and raw[check] == ',':
                    raw = raw[:check] + raw[check + 1:]
                else:
                    break  # unknown cause, stop

            elif "Expecting property name enclosed in double quotes" in msg:
                ch = raw[pos] if pos < len(raw) else ''
                if ch == "'":
                    # Single-quoted key — convert 'key' → "key"
                    end = raw.find("'", pos + 1)
                    if end > pos:
                        raw = raw[:pos] + '"' + raw[pos + 1:end] + '"' + raw[end + 1:]
                    else:
                        break
                elif ch == '/':
                    # JS-style // comment outside a string — strip to end of line
                    end = raw.find('\n', pos)
                    raw = raw[:pos] + (raw[end:] if end >= 0 else '')
                elif ch in ('}', ']'):
                    # Trailing comma before closing bracket — strip it
                    check = pos - 1
                    while check >= 0 and raw[check] in ' \t\n\r':
                        check -= 1
                    if check >= 0 and raw[check] == ',':
                        raw = raw[:check] + raw[check + 1:]
                    else:
                        break
                elif ch.isalpha() or ch == '_':
                    # Unquoted identifier used as key — wrap in double quotes
                    end = pos
                    while end < len(raw) and (raw[end].isalnum() or raw[end] == '_'):
                        end += 1
                    raw = raw[:pos] + '"' + raw[pos:end] + '"' + raw[end:]
                else:
                    # Fallback: replace all 'word': patterns with "word":
                    fixed = re.sub(r"'([^'\\]+)'(\s*:)", r'"\1"\2', raw)
                    if fixed != raw:
                        raw = fixed
                    else:
                        break

            elif "Invalid \\escape" in msg:
                # Bare backslash + non-special char (e.g. \d \w \s in regex)
                # Double the backslash so JSON sees it as a literal backslash
                if pos < len(raw) and raw[pos] == '\\':
                    raw = raw[:pos] + '\\\\' + raw[pos + 1:]
                else:
                    break

            elif "Invalid control character" in msg:
                # Raw control character inside a string value — escape it properly
                ch = raw[pos] if pos < len(raw) else ''
                if ch == '\n':
                    raw = raw[:pos] + '\\n' + raw[pos + 1:]
                elif ch == '\r':
                    raw = raw[:pos] + '\\r' + raw[pos + 1:]
                elif ch == '\t':
                    raw = raw[:pos] + '\\t' + raw[pos + 1:]
                else:
                    raw = raw[:pos] + ' ' + raw[pos + 1:]

            elif "Extra data" in msg:
                # JSON ended but more chars follow — truncate
                raw = raw[:pos]

            elif "Unterminated string" in msg:
                # Close the open string and stop repair (content is incomplete)
                raw = raw[:pos] + '"}'
                break

            else:
                break  # unknown error type, stop trying
    return raw


# ── Public entry point ────────────────────────────────────────────────────────

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

    # On retry: give the worker the previous failure so it can self-correct
    if task.retry_count > 0 and getattr(task, "error_log", None):
        payload["previous_attempt_error"] = task.error_log[:2000]
        payload["retry_attempt"] = task.retry_count

    payload_json = json.dumps(payload, indent=2)

    raw, model_used = _call_with_fallback(system_prompt, payload_json, task.type)
    console.print(f"  [dim]worker: {model_used}[/dim]")

    raw = _repair_json(raw)
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
        if stack == "phaser":
            issues = _validate_phaser_file(entry["path"], entry["content"])
            if issues:
                raise ValueError(
                    f"Phaser validation failed for '{entry['path']}':\n" +
                    "\n".join(f"  • {i}" for i in issues)
                )

    parsed["_model_used"] = model_used
    return parsed


def _validate_phaser_file(path: str, content: str) -> list[str]:
    """Return a list of Phaser-specific issues found in the generated file."""
    if not path.endswith(".js"):
        return []
    issues = []
    # Double-dot syntax error (e.g. obj..method)
    import re
    if re.search(r'\w\.\.\w', content):
        issues.append("double-dot syntax error (obj..method) detected")
    # Invalid API usage
    if "physics.add.rectangle(" in content:
        issues.append("this.physics.add.rectangle() does not exist — use this.add.rectangle() + this.physics.add.existing()")
    # Config before class declarations
    config_pos = content.find("new Phaser.Game(")
    class_pos  = content.find("class ")
    if config_pos != -1 and class_pos != -1 and config_pos < class_pos:
        issues.append("new Phaser.Game() appears before class declarations — classes must come first")
    # ES module syntax
    if re.search(r'^\s*(import|export)\s', content, re.MULTILINE):
        issues.append("ES module syntax (import/export) detected — not allowed in plain script tags")
    return issues


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
    fixed = content.replace(r"\n", "\n").replace(r"\t", "\t")
    if fixed != content:
        console.print(f"  [yellow]⚠ Fixed literal \\\\n/\\\\t sequences in '{path}'[/yellow]")
    return fixed
