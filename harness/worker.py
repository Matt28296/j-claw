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
PWA support (include in every vanilla project that has a UI):
- Create manifest.json at the project root with: name, short_name, start_url ("/"), display ("standalone"), background_color ("#ffffff"), theme_color ("#4f46e5"), and an icons array with two entries — { "src": "icons/icon-192.png", "sizes": "192x192", "type": "image/png" } and { "src": "icons/icon-512.png", "sizes": "512x512", "type": "image/png" }.
- Create sw.js at the project root implementing a cache-first service worker: on "install" event, open a cache named "app-shell-v1" and cache ["./", "./index.html", "./app.js"] (adjust paths to match actual JS file names); on "fetch" event, respond from cache if found, otherwise fetch from network.
- In index.html <head>: add <link rel="manifest" href="manifest.json"> and <meta name="theme-color" content="#4f46e5">.
- In index.html before </body>: add a <script> block that registers the service worker — check "serviceWorker" in navigator, then call navigator.serviceWorker.register("./sw.js") inside a DOMContentLoaded listener.
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
PWA support (include in every react-vite project that has a UI):
- Create public/manifest.json (Vite copies public/ to dist/ automatically) with: name, short_name, start_url ("/"), display ("standalone"), background_color ("#ffffff"), theme_color ("#4f46e5"), and an icons array with two entries — { "src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png" } and { "src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png" }.
- Create public/sw.js implementing a cache-first service worker: on "install" event, open a cache named "app-shell-v1" and cache ["/", "/index.html"] (the built assets); on "fetch" event, respond from cache if found, otherwise fetch from network and cache the response.
- In index.html <head>: add <link rel="manifest" href="/manifest.json"> and <meta name="theme-color" content="#4f46e5">.
- In src/main.jsx, after the ReactDOM.createRoot call, add a service worker registration block: check "serviceWorker" in navigator, then call navigator.serviceWorker.register("/sw.js").catch(console.error).
- Do NOT add vite-plugin-pwa — use the manual manifest + sw.js approach above to keep the dependency surface minimal.
""",

    "fastapi": """\
Stack: Python + FastAPI + SQLAlchemy ORM + SQLite + Alembic migrations (uvicorn server required)
- Entry point: main.py with a FastAPI() app instance, a lifespan context manager, and uvicorn.run() guard.
- Database: use SQLAlchemy with a SQLite file named app.db. Define all models in models.py inheriting from Base (declared in database.py).
- database.py: create engine with `create_engine("sqlite:///./app.db", connect_args={"check_same_thread": False})`, declare `Base = declarative_base()`, create `SessionLocal = sessionmaker(...)`. Do NOT call `Base.metadata.create_all()` anywhere — Alembic handles schema creation.
- get_db() dependency: use yield to provide a SessionLocal() session and close it in finally.
- Routes: use @app.get/post/put/delete decorators. Return dicts or Pydantic schemas (FastAPI auto-serializes).
- CORS: use app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]). NEVER instantiate CORSMiddleware directly as a variable.
- requirements.txt: fastapi, uvicorn[standard], sqlalchemy, alembic. Nothing else unless strictly needed.
- Pydantic models: define a BaseModel for every POST/PUT request body (keep in schemas.py or inline in main.py).
- Error handling: raise HTTPException(status_code=..., detail="...") for known error cases.
- NEVER use raw sqlite3, NEVER use CREATE TABLE IF NOT EXISTS, NEVER call Base.metadata.create_all() — always use Alembic migrations.
- Alembic setup (REQUIRED — generate all these files):
  * alembic.ini at project root: standard Alembic config with script_location = migrations and sqlalchemy.url = sqlite:///./app.db
  * migrations/env.py: import Base from database and set target_metadata = Base.metadata so autogenerate works
  * migrations/versions/ directory: include a .gitkeep placeholder file
  * migrations/versions/0001_initial.py: a real Alembic migration with upgrade() using op.create_table() for every table defined in models.py, and a matching downgrade() that calls op.drop_table() in reverse order. Use proper Column() calls matching your SQLAlchemy model definitions.
- main.py lifespan: on startup, run `subprocess.run(["alembic", "upgrade", "head"], check=True)` so the DB schema is always up to date when the server starts. Import subprocess at the top of main.py.
- alembic.ini format (write this exactly):
  [alembic]
  script_location = migrations
  sqlalchemy.url = sqlite:///./app.db
  [loggers]
  keys = root,sqlalchemy,alembic
  [handlers]
  keys = console
  [formatters]
  keys = generic
  [logger_root]
  level = WARN
  handlers = console
  qualname =
  [logger_sqlalchemy]
  level = WARN
  handlers =
  qualname = sqlalchemy.engine
  [logger_alembic]
  level = INFO
  handlers =
  qualname = alembic
  [handler_console]
  class = StreamHandler
  args = (sys.stderr,)
  level = NOTSET
  formatter = generic
  [formatter_generic]
  format = %(levelname)-5.5s [%(name)s] %(message)s
  datefmt = %H:%M:%S
- migrations/env.py format (write this exactly, substituting your actual Base import):
  from logging.config import fileConfig
  from sqlalchemy import engine_from_config, pool
  from alembic import context
  from database import Base
  config = context.config
  if config.config_file_name is not None:
      fileConfig(config.config_file_name)
  target_metadata = Base.metadata
  def run_migrations_offline():
      url = config.get_main_option("sqlalchemy.url")
      context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
      with context.begin_transaction():
          context.run_migrations()
  def run_migrations_online():
      connectable = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
      with connectable.connect() as connection:
          context.configure(connection=connection, target_metadata=target_metadata)
          with context.begin_transaction():
              context.run_migrations()
  if context.is_offline_mode():
      run_migrations_offline()
  else:
      run_migrations_online()
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

    "electron": """\
Stack: Electron desktop app (Node.js + Chromium, cross-platform)
- Project layout: main.js (main process), preload.js (preload script), renderer/ (HTML+JS+CSS for UI).
- package.json: dependencies = electron; scripts: "start": "electron .", "build": "electron-builder --dir"; main field = "main.js"
- main.js: create BrowserWindow with webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false }; load renderer/index.html via loadFile(); handle app.whenReady(), app.on('window-all-closed'), app.on('activate').
- preload.js: use contextBridge.exposeInMainWorld('api', { ... }) to expose safe IPC methods. Use ipcRenderer.invoke/send/on for main↔renderer communication.
- renderer/index.html: normal HTML page; reference renderer/style.css and renderer/app.js via relative paths; NO node require() calls in renderer (contextIsolation is on).
- renderer/app.js: plain browser JS — use window.api.* (exposed via preload) for all IPC; no ES module syntax.
- renderer/style.css: use CSS variables for theming; dark mode friendly (prefers-color-scheme: dark).
- IPC handlers: define ipcMain.handle('channel', handler) in main.js for each method exposed via preload.
- File dialogs: use dialog.showOpenDialog / showSaveDialog from electron (main process only, via IPC).
- Window size: default 1200×800 with a minimum of 800×600. Set title in BrowserWindow options.
- Do NOT use require() in renderer code — all Node.js access must go through the preload contextBridge.
- Verification: "build" runs npm install. Cannot run full GUI in CI.
""",

    "tauri": """\
Stack: Tauri 2.x desktop app (Rust backend + WebView frontend)
- Project layout: src-tauri/src/main.rs, src-tauri/Cargo.toml, src-tauri/tauri.conf.json, src/index.html, src/main.js, package.json
- src-tauri/Cargo.toml: [package] name, version = "0.1.0", edition = "2021"; [dependencies] tauri = { version = "2", features = [] }, serde = { version = "1", features = ["derive"] }, serde_json = "1"; [[bin]] name = "app", path = "src/main.rs"
- src-tauri/src/main.rs: use #![cfg_attr(not(debug_assertions), windows_subsystem = "windows")] at the top; define Rust commands with #[tauri::command] attribute; register commands with .invoke_handler(tauri::generate_handler![cmd1, cmd2, ...]); use tauri::Builder::default().run(tauri::generate_context!()) in main(); return Result<T, String> from all commands (never panic).
- src-tauri/tauri.conf.json: must include "productName", "identifier" (e.g. "com.example.app"), "version", and a "windows" array with at least one window entry containing "title", "width", "height"; set "bundle": { "active": true, "identifier": "com.example.app" }.
- src/index.html: normal HTML page loading src/main.js via <script src="main.js"></script>; no ES module syntax in main.js.
- src/main.js: import invoke from Tauri using window.__TAURI__.core.invoke (UMD global available at runtime); call invoke("command_name", { arg: value }) which returns a Promise; handle .then()/.catch() or use async/await.
- package.json: scripts "dev": "tauri dev", "build": "tauri build"; devDependencies: "@tauri-apps/cli": "^2"
- Do NOT use ipcRenderer or contextBridge — Tauri uses invoke() exclusively for frontend↔backend calls.
- All Rust structs passed to/from frontend must derive Serialize + Deserialize from serde.
- serde import in main.rs: use serde::{Deserialize, Serialize};
""",

    "godot": """\
Stack: Godot 4.x game (GDScript, text-based scene files)
- Project layout: project.godot, scenes/Main.tscn, scripts/Main.gd, assets/ directory
- Use GDScript exclusively — do NOT use C# or any other language.
- Use Godot 4 API only — never use Godot 3 API (e.g., use CharacterBody2D not KinematicBody2D, use Input.get_vector() not Input.is_action_pressed() for movement).
- project.godot: must have config_version=5 at the top, [application] section with config/name and run/main_scene pointing to the main scene (e.g. "res://scenes/Main.tscn"), and [rendering] section.
- GDScript syntax rules: use @export for exported variables, func _ready() for initialization, func _process(delta: float) for per-frame logic, $NodeName shorthand for get_node("NodeName"), signal declarations with the "signal" keyword.
- .tscn files: use Godot 4 text scene format with [gd_scene] header, [node] entries including name, type, and script path (ExtResource); parent node must use parent="." with no explicit parent field.
- Do NOT generate .import files, .godot/ cache files, or binary resources — only write source files.
- For 2D games: root node type Node2D or Control; use Sprite2D (not Sprite), CollisionShape2D, Area2D, CharacterBody2D.
- For signals: use signal_name.connect(callable) syntax (Godot 4), not .connect(target, "method_string") (Godot 3).
- Resource paths: always use res:// prefix for scene and script references in .tscn and project.godot.
""",

    "websocket-sse": """\
Stack: Node.js real-time dashboard (Express + WebSocket or SSE, no build step)
- Project layout: server.js, public/index.html, public/client.js, package.json
- package.json: dependencies = express, ws; scripts: "start": "node server.js", "dev": "nodemon server.js"; no TypeScript, no build step.
- server.js: use Express for HTTP routes; serve public/ via express.static; listen on PORT 3000; include GET /health endpoint that returns JSON { status: "ok", timestamp: Date.now() }.
- WebSocket support: attach a "ws" WebSocket server to the same HTTP server using new WebSocketServer({ server }); broadcast updates to all connected clients with ws.clients.forEach(client => { if (client.readyState === WebSocket.OPEN) client.send(JSON.stringify(data)); }).
- SSE support: for SSE routes, set headers res.setHeader("Content-Type", "text/event-stream"), res.setHeader("Cache-Control", "no-cache"), res.setHeader("Connection", "keep-alive"); send data with res.write("data: " + JSON.stringify(payload) + "\\n\\n").
- Dashboard (public/index.html): dark theme using Tailwind CSS CDN (<script src="https://cdn.tailwindcss.com"></script>); show live-updating data in a clean layout; no custom CSS files needed (Tailwind utilities only).
- public/client.js: plain browser JS — no ES module syntax (no import/export, no type="module"); connect to WebSocket with new WebSocket("ws://" + location.host); include client-side reconnect logic: on "close" event, use setTimeout(() => reconnect(), 3000) with exponential backoff up to 30 seconds.
- SSE client reconnect: use EventSource with built-in browser reconnect; explicitly handle EventSource onerror to log and let browser auto-reconnect.
- Live data: server must push data updates at a regular interval (e.g. setInterval every 1-2 seconds) so the dashboard updates in real time without user interaction.
- Do NOT use TypeScript, webpack, Vite, or any build tooling — pure Node.js + browser JS only.
""",

    "auth": """\
Stack: JWT Authentication layer for FastAPI backend + React frontend (full-stack auth module)
This stack prompt applies to auth tasks within a full-stack project. Write COMPLETE file contents.

--- BACKEND (FastAPI) ---
- Dependencies: add `python-jose[cryptography]` and `passlib[bcrypt]` to requirements.txt.
- auth.py: import os, datetime, jose.jwt, passlib.context.CryptContext.
  - JWT_SECRET = os.getenv("JWT_SECRET", "<long-random-fallback-string>")
  - ALGORITHM = "HS256"
  - ACCESS_TOKEN_EXPIRE_MINUTES = 60
  - pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
  - hash_password(plain: str) -> str  — returns pwd_context.hash(plain)
  - verify_password(plain: str, hashed: str) -> bool  — returns pwd_context.verify(plain, hashed)
  - create_access_token(data: dict) -> str  — encodes JWT with exp claim using JWT_SECRET + ALGORITHM
  - decode_access_token(token: str) -> dict | None  — decodes and returns payload; returns None on JWTError
- models.py additions: users table with columns id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, hashed_password TEXT NOT NULL. Add init_users_table() called at startup.
- schemas.py: define Pydantic models — UserCreate(username: str, password: str), UserResponse(id: int, username: str), Token(access_token: str, token_type: str = "bearer"), TokenData(username: str | None = None).
- routes/auth_routes.py (or inline in main.py if project is small):
  - POST /auth/register — accepts UserCreate, checks username not taken (409 if duplicate), hashes password with hash_password(), inserts into users table, returns UserResponse.
  - POST /auth/login — accepts UserCreate (username + password), looks up user, verifies password with verify_password(), raises 401 if invalid, returns Token with create_access_token({"sub": username}).
- NEVER store plaintext passwords. Always call hash_password() before INSERT.
- get_current_user() dependency: reads Authorization header, calls decode_access_token(), raises 401 if None.

--- FRONTEND (React) ---
- src/api/axiosInstance.js: create axios instance with baseURL from import.meta.env.VITE_API_URL. Add request interceptor that reads token from localStorage.getItem("token") and sets Authorization: "Bearer <token>" header if present.
- src/components/LoginForm.jsx: controlled form with username + password fields; on submit POST /auth/login, store response access_token in localStorage under key "token", then call onSuccess() prop.
- src/components/RegisterForm.jsx: controlled form with username + password; on submit POST /auth/register, on success show success message or redirect to login.
- src/components/PrivateRoute.jsx: reads localStorage.getItem("token"); if falsy, returns <Navigate to="/login" replace />; otherwise returns <Outlet />.
- Use react-router-dom v6: wrap protected routes with <Route element={<PrivateRoute />}>.
- Logout: remove "token" from localStorage and navigate to "/login".
- Never store the JWT in a non-httpOnly cookie or expose it to third-party scripts.
- All API calls must use the axiosInstance (not raw fetch) so the interceptor attaches the token automatically.
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
        if task_type == "auth":
            # Auth tasks get the dedicated auth prompt that covers both backend JWT
            # helpers (python-jose + passlib) and frontend React patterns (axiosInstance,
            # LoginForm, RegisterForm, PrivateRoute).
            effective_stack = "auth"
        elif task_type in ("backend", "api", "database", "config"):
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
