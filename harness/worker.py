from __future__ import annotations
import json
import ollama
from rich.console import Console

from config import (
    WORKER_MODEL, OLLAMA_HOST, WORKER_PROVIDER,
    WORKER_FALLBACKS, ANTHROPIC_API_KEY, OPENROUTER_API_KEY,
)

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
- Load Phaser 3 via CDN in index.html.
- Multi-file projects are supported: each JS file is a plain <script src="js/filename.js"> (NOT type="module").
- No ES module syntax (no import/export). All classes and functions must be assigned to window.* so other files can access them.
- Scenes: define classes that extend Phaser.Scene with constructor() calling super('SceneName'), and implement create() and update().
- Use this.add.text(), this.add.graphics(), this.input.keyboard.addKeys() etc. — all Phaser built-in APIs.
- Game logic as class properties on the scene. Access shared state via window.CONFIG or globals defined in earlier scripts.
- Scene transitions: use this.scene.start('SceneName', data) to switch scenes.
- Graphics: use scene.add.graphics() then fillStyle()/fillRect()/fillCircle()/fillTriangle() — no textures unless preloaded.
""",

    "web3": """\
Stack: Solidity smart contracts + Hardhat + ethers.js
- Project layout: contracts/*.sol, scripts/deploy.js, test/*.js, hardhat.config.js, package.json
- package.json: devDependencies must include hardhat, @nomicfoundation/hardhat-toolbox, ethers; scripts: "compile": "hardhat compile", "test": "hardhat test", "deploy": "hardhat run scripts/deploy.js --network localhost"
- hardhat.config.js: require("@nomicfoundation/hardhat-toolbox"); solidity version "0.8.24"; networks.localhost = { url: "http://127.0.0.1:8545" }
- Contracts: every .sol file must start with // SPDX-License-Identifier: MIT and pragma solidity ^0.8.24;
- For ERC-20 tokens: install @openzeppelin/contracts and extend ERC20; for NFTs extend ERC721
- Deployment script: scripts/deploy.js — use ethers from hardhat; deploy with await ContractFactory.deploy(...); console.log("Deployed to:", contract.target)
- Tests: test/*.js — use chai expect; import { ethers } from "hardhat"; deploy fresh instance in beforeEach; cover constructor, state reads, state writes, error cases
- Frontend (if requested): frontend/index.html + frontend/app.js — load ethers UMD via CDN: <script src="https://cdnjs.cloudflare.com/ajax/libs/ethers/6.7.0/ethers.umd.min.js"></script>
- Frontend JS: use window.ethers (UMD global, NOT import); connect via window.ethereum (MetaMask); hardcode contract ABI as a JS const; use placeholder "DEPLOYED_CONTRACT_ADDRESS" for user to fill after deploy
- NEVER use ES modules in the frontend (no import/export, no type="module") — use UMD CDN only
- README.md: include setup steps: npm install, npx hardhat compile, npx hardhat test, npx hardhat node (terminal 1), npx hardhat run scripts/deploy.js --network localhost (terminal 2)
""",

    "react-native": """\
Stack: React Native + Expo (mobile app, no native build required)
- Init pattern: write app.json, App.js (or App.tsx), package.json for Expo managed workflow.
- package.json: dependencies must include expo, react, react-native, react-native-safe-area-context; scripts: "start": "expo start", "android": "expo run:android", "ios": "expo run:ios"
- app.json: include expo.name, expo.slug, expo.version ("1.0.0"), expo.platforms (["ios","android","web"]), expo.sdkVersion ("51.0.0")
- Styling: use React Native StyleSheet.create() — NO Tailwind, NO CSS files, NO className props.
- Navigation: if multiple screens needed, use @react-navigation/native + @react-navigation/native-stack. Include NavigationContainer from @react-navigation/native and createNativeStackNavigator.
- State: useState / useEffect hooks only — no Redux, no Zustand unless explicitly requested.
- Components: use View, Text, TextInput, TouchableOpacity, FlatList, ScrollView, Image from react-native.
- Never use <div>, <p>, <span>, <button>, <input> — those are web HTML, not React Native.
- AsyncStorage: use @react-native-async-storage/async-storage for local persistence.
- Entry point: App.js must export default a single React component (functional).
- Include README.md with: npm install, npx expo start, how to run on iOS/Android via Expo Go app.
""",

    "socket-io": """\
Stack: Socket.io real-time multiplayer (Node.js server + vanilla browser client)
- Server: server.js using express + socket.io; listen on PORT (default 3000).
- package.json: dependencies = express, socket.io; scripts: "start": "node server.js", "dev": "nodemon server.js"
- Client: public/index.html loading Socket.io client via CDN <script src="/socket.io/socket.io.js"></script>
- Client JS: public/app.js — use io() to connect; NO ES module syntax (no import/export, no type="module").
- All client JS must be plain <script src="app.js"> (no bundler, no module system).
- Server emits events to all connected clients via io.emit('event', data) or to one via socket.emit('event', data).
- Client listens with socket.on('event', callback) and sends with socket.emit('event', data).
- Game loop (if applicable): run on the server side with setInterval; broadcast state to all clients each tick.
- Player management: track connected players in a Map keyed by socket.id; remove on 'disconnect'.
- Static files: serve public/ via express.static so index.html and app.js are reachable at /.
- CORS: if needed, configure cors origin in the Socket.io server constructor.
- Include README.md: npm install, node server.js, open http://localhost:3000 in multiple tabs.
""",

    "three-js": """\
Stack: Three.js 3D browser app/game (vanilla HTML + JS, no build step, CDN)
- Load Three.js via CDN in index.html: <script src="https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js"></script>
- NEVER use ES module syntax (no import/export, no type="module") — Three.js UMD global is window.THREE.
- Renderer: create a THREE.WebGLRenderer({antialias:true}), set size to window.innerWidth/Height, append renderer.domElement to document.body.
- Scene graph: new THREE.Scene(), new THREE.PerspectiveCamera(75, aspect, 0.1, 1000), position camera back (camera.position.z = 5).
- Lighting: always add at least one THREE.AmbientLight and one THREE.DirectionalLight or THREE.PointLight.
- Geometry + material: use THREE.BoxGeometry, SphereGeometry, PlaneGeometry, etc. with THREE.MeshStandardMaterial or MeshPhongMaterial.
- Animation loop: use requestAnimationFrame for a self-calling animate() function; call renderer.render(scene, camera) every frame.
- Resize handling: listen to window 'resize' event and update camera.aspect + camera.updateProjectionMatrix() + renderer.setSize().
- Controls: if user interaction needed, load OrbitControls from CDN: <script src="https://cdn.jsdelivr.net/npm/three@0.160.0/examples/js/controls/OrbitControls.js"></script>
- Multiple files: each JS file is a plain <script src="js/filename.js"> — globals only, assigned to window.* if shared.
- Colors: use hex colors with 0x prefix (e.g. 0xff6600) in material color properties.
- Include a dark background: document.body.style.margin='0'; document.body.style.background='#000';
""",
}


# ── Public entry point ────────────────────────────────────────────────────────

def execute_task(task, spec: dict, dependency_files: dict[str, dict[str, str]]) -> dict:
    """
    Ask the worker model to implement a task.
    Returns {"files": [...], "model_used": "<provider>/<model>"}.

    Tries WORKER_PROVIDER first, then each entry in WORKER_FALLBACKS on provider failure.
    Raises ValueError immediately on bad JSON format so the scheduler can send EXECUTION_ERROR.
    """
    arch  = spec.get("architecture", {})
    stack = arch.get("stack", "vanilla")
    # For full-stack projects, pick the sub-stack based on task type
    if stack == "full-stack":
        task_type = getattr(task, "type", "") or ""
        if task_type in ("backend", "api", "database", "auth", "config"):
            effective_stack = "fastapi"
        else:
            effective_stack = "react-vite"
    else:
        effective_stack = stack
    system_prompt = _SYSTEM_PROMPT + "\n" + _STACK_PROMPTS.get(effective_stack, _STACK_PROMPTS["vanilla"])
    user_message = _build_user_message(task, spec, dependency_files)

    # Build attempt chain: primary first, then fallbacks
    attempts: list[tuple[str, str]] = [(WORKER_PROVIDER, WORKER_MODEL)] + list(WORKER_FALLBACKS)

    last_err: Exception | None = None
    for provider, model in attempts:
        try:
            raw = _call_provider(provider, model, system_prompt, user_message)
            parsed = _parse_and_validate(raw)
            label = model if provider == "ollama" else f"{provider}/{model}"
            parsed["model_used"] = label
            return parsed

        except ValueError:
            raise  # Bad output format — let scheduler handle via EXECUTION_ERROR

        except Exception as exc:  # noqa: BLE001
            last_err = exc
            console.print(
                f"  [yellow]Worker {provider}/{model} error: {exc!r} — trying fallback…[/yellow]"
            )
            continue

    raise RuntimeError(
        f"All worker providers exhausted. Last error: {last_err}"
    ) from last_err


# ── Provider dispatch ─────────────────────────────────────────────────────────

def _call_provider(provider: str, model: str, system: str, user: str) -> str:
    if provider == "ollama":
        return _call_ollama(model, system, user)
    if provider == "anthropic":
        return _call_anthropic(model, system, user)
    if provider == "openrouter":
        return _call_openrouter(model, system, user)
    raise ValueError(f"Unknown worker provider: {provider!r}")


def _call_ollama(model: str, system: str, user: str) -> str:
    client = ollama.Client(host=OLLAMA_HOST)
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        format="json",
        options={"temperature": 0.15, "num_predict": 8192},
    )
    return response.message.content.strip()


def _call_anthropic(model: str, system: str, user: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot use anthropic worker provider")
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text.strip()


def _call_openrouter(model: str, system: str, user: str) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set — cannot use openrouter worker provider")
    from openai import OpenAI
    client = OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        default_headers={"X-Title": "J-Claw"},
    )
    response = client.chat.completions.create(
        model=model,
        max_tokens=8192,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content.strip()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_user_message(task, spec: dict, dependency_files: dict) -> str:
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
            "stack": spec.get("architecture", {}).get("stack", "vanilla"),
            "architecture": spec.get("architecture", {}),
        },
        "existing_dependency_files": {
            tid: files for tid, files in dependency_files.items()
        },
    }
    return json.dumps(payload, indent=2)


def _parse_and_validate(raw: str) -> dict:
    raw = _strip_fences(raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Worker returned invalid JSON: {exc}\n--- raw (first 600 chars) ---\n{raw[:600]}"
        ) from exc

    if not isinstance(parsed.get("files"), list):
        raise ValueError(
            f"Worker output missing 'files' list. Got keys: {list(parsed.keys())}"
        )

    for entry in parsed["files"]:
        if not isinstance(entry.get("path"), str) or not isinstance(entry.get("content"), str):
            raise ValueError(f"Worker file entry missing 'path' or 'content': {entry}")
        entry["content"] = _fix_literal_newlines(entry["path"], entry["content"])
        _warn_if_truncated(entry["path"], entry["content"])

    return parsed


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` wrapping that API models sometimes add."""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    inner = lines[1:]
    if inner and inner[-1].strip() == "```":
        inner = inner[:-1]
    return "\n".join(inner).strip()


_TRUNCATION_MARKERS = ("...", "// TODO", "# TODO", "[truncated]", "/* ... */", "// ...")


def _warn_if_truncated(path: str, content: str) -> None:
    stripped = content.rstrip()
    if len(stripped) < 40:
        console.print(
            f"  [yellow]⚠ Worker file '{path}' is suspiciously short ({len(stripped)} chars)[/yellow]"
        )
        return
    tail = stripped[-80:]
    for marker in _TRUNCATION_MARKERS:
        if marker in tail:
            console.print(
                f"  [yellow]⚠ Worker file '{path}' may be truncated — ends near: {marker!r}[/yellow]"
            )


def _fix_literal_newlines(path: str, content: str) -> str:
    """Replace literal \\n / \\t sequences with real whitespace."""
    ext = path.rsplit(".", 1)[-1].lower()
    if ext not in ("js", "ts", "jsx", "tsx", "py", "html", "css"):
        return content
    if r"\n" not in content and r"\t" not in content:
        return content
    fixed = content.replace(r"\n", "\n").replace(r"\t", "\t")
    if fixed != content:
        console.print(f"  [yellow]⚠ Fixed literal \\\\n/\\\\t sequences in '{path}'[/yellow]")
    return fixed
