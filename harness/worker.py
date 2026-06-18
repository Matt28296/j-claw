from __future__ import annotations
import json
import re
import os
import shutil
import subprocess
import tempfile
import time
import ollama
import httpx
from rich.console import Console

import threading

from config import (
    WORKER_MODEL, OLLAMA_HOST, WORKER_PROVIDER,
    WORKER_FALLBACKS, ANTHROPIC_API_KEY, OPENROUTER_API_KEY,
    WORKER_LADDER, LOCAL_FIRST_TASK_TYPES, MAX_PAID_WORKER_CALLS,
    WORKER_TASK_TIMEOUT,
    CODEX_CLI_ENABLED, CODEX_HOME, CODEX_MODEL, CODEX_EFFORT, OPUS_MODEL,
    CODEX_CLI_MAX_CALLS, CODEX_TIMEOUT, OAUTH_PROVIDERS, METERED_PROVIDERS,
    GROK_CLI_ENABLED, GROK_HOME, GROK_MODEL, GROK_MAX_CALLS, GROK_TIMEOUT,
    CLAUDE_CLI_ENABLED, CLAUDE_CLI_HOME, CLAUDE_CLI_MODEL, CLAUDE_CLI_MAX_CALLS, CLAUDE_CLI_TIMEOUT,
    CODEX_WORKER_RESERVE,
)
from experience_log import get_worker_hints, log_escalation
from cost import record_role_event

console = Console()

# Thread-local storage for per-call token counts.  Each _call_* function writes
# the tokens it observed; execute_task reads and resets it after every attempt.
_call_tokens = threading.local()


def _set_call_tokens(inp: int, out: int) -> None:
    """Store token counts from the most recent provider call (thread-safe)."""
    _call_tokens.input = inp
    _call_tokens.output = out


def _get_call_tokens() -> dict:
    """Return and clear the stored token counts for this thread."""
    tok = {"input": getattr(_call_tokens, "input", 0),
           "output": getattr(_call_tokens, "output", 0)}
    _call_tokens.input = 0
    _call_tokens.output = 0
    return tok

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
- OPTIONAL: you MAY add a top-level "lesson" object as a SIBLING of "files" (never inside a file
  entry) capturing the single key technique or gotcha for this task, e.g.
  {"files":[...], "lesson":{"solution_technique":"...","prompt_hint":"one-sentence rule","anti_pattern":"the mistake to avoid"}}.
  It is metadata for the build's memory, NOT a file. Omit it if you have nothing useful to add.
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
- Create sw.js at the project root implementing a cache-first service worker: on "install" event, open a cache named "app-shell-v1" and cache ["./", "./index.html"] plus every JS file declared in the project's js/ directory (e.g. "./js/scroll.js", "./js/nav.js", "./js/form.js" — use the actual filenames from the task DAG, never the placeholder "./app.js"); on "fetch" event, respond from cache if found, otherwise fetch from network.
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
State management rules (follow strictly — do not invent patterns):
- Component-local state: useState only.
- State shared between 2-3 siblings: lift to nearest common parent, pass as props.
- State needed by 4+ components or anywhere deep in the tree: React Context + useContext.
  Create one context file per domain in src/context/ (e.g. src/context/AuthContext.jsx).
  Each context file exports: the context object, a Provider component, and a useXxx hook.
- App-wide async state (server data, loading, errors): useEffect + useState in a custom hook
  in src/hooks/ (e.g. src/hooks/useTodos.js). No Redux, no Zustand unless explicitly requested.
- Never prop-drill beyond 2 levels. If a prop passes through 3+ components untouched, use Context.
- Naming: XxxContext.jsx for context files, useXxx.js for custom hooks.
PWA support (include in every react-vite project that has a UI):
- Create public/manifest.json (Vite copies public/ to dist/ automatically) with: name, short_name, start_url ("/"), display ("standalone"), background_color ("#ffffff"), theme_color ("#4f46e5"), and an icons array with two entries — { "src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png" } and { "src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png" }.
- Create public/sw.js implementing a cache-first service worker: on "install" event, open a cache named "app-shell-v1" and cache ["/", "/index.html"] (the built assets); on "fetch" event, respond from cache if found, otherwise fetch from network and cache the response.
- In index.html <head>: add <link rel="manifest" href="/manifest.json"> and <meta name="theme-color" content="#4f46e5">.
- In src/main.jsx, after the ReactDOM.createRoot call, add a service worker registration block: check "serviceWorker" in navigator, then call navigator.serviceWorker.register("/sw.js").catch(console.error).
- Do NOT add vite-plugin-pwa — use the manual manifest + sw.js approach above to keep the dependency surface minimal.
Full-stack wiring (use when memory_context.related_files contains wiring.json):
- If wiring.json is listed in related_files, read api_base_url from it and use that value as
  the baseURL in src/api/axiosInstance.js instead of hardcoding http://localhost:8000.
- Example: import wiring from '../../wiring.json'; const API = axios.create({ baseURL: wiring.api_base_url })
  OR use import.meta.env.VITE_API_URL with a fallback: axios.create({ baseURL: import.meta.env.VITE_API_URL || 'http://localhost:8000' })
Stripe frontend integration (include when project requires payments/checkout):
- Load Stripe.js via script tag in index.html: <script src="https://js.stripe.com/v3/"></script>
- In checkout component: const stripe = window.Stripe(import.meta.env.VITE_STRIPE_PUBLISHABLE_KEY)
- POST to /payments/create-checkout-session with cart data → get {url} → stripe.redirectToCheckout is deprecated; use window.location.href = url (Stripe Checkout redirects)
- Add VITE_STRIPE_PUBLISHABLE_KEY to .env.example (note: VITE_ prefix is required for Vite to expose env vars to frontend)
- Add a <SuccessPage /> component for success_url and a <CancelPage /> for cancel_url
LemonSqueezy checkout (when the project involves LemonSqueezy payments):
- Add Lemon.js CDN to index.html: <script src="https://assets.lemonsqueezy.com/lemon.js" defer></script>
- Initialize: window.createLemonSqueezy() after DOM load
- Overlay checkout: <a class="lemonsqueezy-button" href="{checkout_url}">Buy Now</a> — Lemon.js intercepts and shows overlay
- Get checkout_url: fetch from backend POST /checkout/lemonsqueezy, then set as href or call LemonSqueezy.Url.Open(url)
- Success/cancel: use LemonSqueezy event listeners: window.addEventListener("LP:checkout:close", handler)

Stripe Connect frontend (when the project involves a marketplace or platform payments):
- Seller onboarding button: onClick → POST /connect/onboard → window.location.href = data.url (redirects to Stripe)
- Buyer payment: fetch POST /payments/create-intent → get {clientSecret, publishableKey}
  then: const stripe = window.Stripe(publishableKey); const elements = stripe.elements({clientSecret});
  mount PaymentElement, confirm with stripe.confirmPayment({elements, confirmParams: {return_url: ...}})
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
Full-stack wiring (include when this is the backend half of a full-stack FORMAT 5 project):
- Write a wiring.json file at the project root alongside the code files:
  {"api_base_url": "http://localhost:8000", "cors_origin": "http://localhost:5173"}
- This file is read by the frontend sub-project to configure its API base URL automatically.
Stripe payment integration (include when project requires payments/checkout/subscriptions):
- Add stripe to requirements.txt: stripe>=7.0.0
- Create payments.py: import stripe; stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
  - POST /payments/create-checkout-session: create stripe.checkout.Session with line_items, mode='payment', success_url, cancel_url
  - POST /payments/webhook: verify stripe.Webhook.construct_event(payload, sig_header, os.getenv("STRIPE_WEBHOOK_SECRET")); handle checkout.session.completed
  - Use async def with request: Request to read raw body for webhook
- Add STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET to .env.example
- Never hardcode keys. Return {"url": session.url} from checkout endpoint.
LemonSqueezy integration (when the project involves LemonSqueezy payments):
- Install: pip install httpx; use REST API directly (no official Python SDK needed)
- Create checkout: POST https://api.lemonsqueezy.com/v1/checkouts
  Headers: Authorization: Bearer {LEMONSQUEEZY_API_KEY}, Accept: application/vnd.api+json, Content-Type: application/vnd.api+json
  Body: {"data": {"type": "checkouts", "attributes": {"checkout_data": {"custom": {"user_id": "..."}}, "product_options": {}}, "relationships": {"store": {"data": {"type": "stores", "id": "{LEMONSQUEEZY_STORE_ID}"}}, "variant": {"data": {"type": "variants", "id": "{LEMONSQUEEZY_VARIANT_ID}"}}}}}
- Webhook: POST /webhooks/lemonsqueezy
  Verify: HMAC-SHA256 of raw request body with LEMONSQUEEZY_WEBHOOK_SECRET, compare to X-Signature header
  Handle: order_created event → grant access
- .env.example keys: LEMONSQUEEZY_API_KEY, LEMONSQUEEZY_WEBHOOK_SECRET, LEMONSQUEEZY_STORE_ID, LEMONSQUEEZY_VARIANT_ID

Stripe Connect multi-vendor (when the project involves a marketplace or platform payments):
- Platform collects fee: stripe.PaymentIntent.create(amount=..., currency="usd", transfer_data={"destination": connected_account_id}, application_fee_amount=fee_cents)
- Onboard seller: POST /connect/onboard → stripe.Account.create(type="express") → stripe.AccountLink.create(account=acct_id, refresh_url=..., return_url=..., type="account_onboarding") → return {"url": link.url}
- Payout: stripe automatically transfers to connected account after PaymentIntent succeeds
- .env.example keys: STRIPE_PLATFORM_SECRET_KEY, STRIPE_CONNECT_WEBHOOK_SECRET
- Endpoints to generate: POST /connect/onboard, POST /payments/create-intent, POST /webhooks/stripe-connect
""",

    "phaser": """\
Stack: Phaser 3 browser game (vanilla HTML + JS, no build step)
- Load Phaser 3 via CDN in index.html.
- Multi-file projects are supported: each JS file is a plain <script src="js/filename.js"> (NOT type="module").
- No ES module syntax (no import/export). All classes and functions must be assigned to window.* so other files can access them.
- Scenes: define classes that extend Phaser.Scene with constructor() calling super('SceneName'), and implement create() and update().
- Use this.add.text(), this.add.graphics(), this.input.keyboard.addKeys() etc. — all Phaser built-in APIs.
- Scene transitions: use this.scene.start('SceneName', data) to switch scenes.
- Graphics: use scene.add.graphics() then fillStyle()/fillRect()/fillCircle()/fillTriangle() — no textures unless preloaded.
Cross-scene state (REQUIRED — missing this causes scene-transition bugs):
- Define window.GAME_STATE = {} in the FIRST script loaded (index.html script order matters).
- All scenes READ and WRITE only from window.GAME_STATE — never store shared data on scene instance properties.
- Scene key strings: the string passed to super() MUST exactly match the string passed to this.scene.start(). Use a constants object to prevent typos:
  window.CONFIG = { SCENES: { BOOT: 'BootScene', GAME: 'GameScene', OVER: 'GameOverScene' } }
  then in each class: super(window.CONFIG.SCENES.GAME) and transitions: this.scene.start(window.CONFIG.SCENES.OVER)
- Define window.CONFIG in the first <script> tag in index.html (before any scene files).
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
ABI auto-import (eliminates the manual placeholder step — REQUIRED):
- In scripts/deploy.js, after deploying the contract, write the address + ABI to a JSON file
  that the frontend can import directly:
  const fs = require('fs')
  const artifact = require('../artifacts/contracts/<ContractName>.sol/<ContractName>.json')
  const deployment = { address: contract.target, abi: artifact.abi }
  fs.mkdirSync('./frontend/src/contracts', { recursive: true })
  fs.writeFileSync('./frontend/src/contracts/<ContractName>.json', JSON.stringify(deployment, null, 2))
- In frontend/app.js, load the deployment JSON (either via fetch or hardhat artifacts copy):
  fetch('./contracts/<ContractName>.json').then(r => r.json()).then(deployment => {
    const contract = new ethers.Contract(deployment.address, deployment.abi, signer)
  })
- NEVER output the string "DEPLOYED_CONTRACT_ADDRESS" as a placeholder in any file.
- Add frontend/src/contracts/ to .gitignore — it is a build artifact regenerated on each deploy.
- Hardhat networks config: always include hardhat: {} (in-memory) and localhost: { url: "http://127.0.0.1:8545" } so tests run on the in-memory network and deploy targets localhost.
IPFS/Filecoin deployment (include when project requires decentralized hosting):
- In scripts/deploy.js, after deploying the contract, also write frontend/public/deployment.json:
  { "chainId": network.config.chainId, "address": contract.target, "network": network.name }
- Add a scripts/pin-to-ipfs.js that uses the Pinata API (pinata.cloud free tier) to upload the frontend/dist/ directory:
  const axios = require('axios'); const FormData = require('form-data'); const fs = require('fs');
  async function pinDirectory(dirPath) { /* POST to https://api.pinata.cloud/pinning/pinFileToIPFS */ }
  - The script reads PINATA_API_KEY and PINATA_SECRET from env
  - Outputs the IPFS CID and gateway URL: https://gateway.pinata.cloud/ipfs/<CID>
- Add PINATA_API_KEY and PINATA_SECRET to .env.example
- In README.md, add: "Deploy to IPFS: npm install && npx hardhat run scripts/deploy.js && cd frontend && npm run build && node ../scripts/pin-to-ipfs.js"
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
Reconnect state sync (REQUIRED for all stateful projects):
- On server: inside every 'connection' handler, immediately emit 'state_sync' to the newly
  connected socket with the complete current state: socket.emit('state_sync', getCurrentState()).
  Define getCurrentState() as a function that returns the full authoritative server state.
- On client: listen for 'state_sync' and FULLY REPLACE (not merge) local UI state with the
  received data. This ensures a reconnecting client sees the current world without stale data.
- socket.io-client handles TCP reconnect automatically. The server 'connection' event re-fires
  on each reconnect, so state_sync is re-sent automatically — no extra client-side reconnect logic needed.
- Room pattern (for multiplayer): socket.join(roomId) for isolated game/session instances.
  Broadcast within a room with io.to(roomId).emit() instead of io.emit() (which hits all clients).
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
Physics:
- For simple collision/bounce: use THREE.Raycaster — cast a ray from the object's current
  position in its direction of travel, check intersections before moving the object each frame.
- For real physics (gravity, rigid bodies, constraints): load Cannon-es via CDN:
  <script src="https://cdn.jsdelivr.net/npm/cannon-es@0.20.0/dist/cannon-es.js"></script>
  Create a CANNON.World({ gravity: new CANNON.Vec3(0, -9.82, 0) }). For each Three.js mesh,
  create a matching CANNON.Body and add it to the world. In the animate() loop: world.step(1/60),
  then copy body.position and body.quaternion to mesh.position and mesh.quaternion.
- Performance: for 50+ moving objects of the same type, use THREE.InstancedMesh instead of
  separate Mesh objects. Update instance matrices each frame with setMatrixAt() + instanceMatrix.needsUpdate=true.
- Always add at least one THREE.AmbientLight and one THREE.DirectionalLight — MeshStandardMaterial
  and MeshPhongMaterial are completely invisible without lighting.
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
IPC contract discipline (prevents channel-mismatch bugs — the #1 Electron failure mode):
- Define a CHANNELS constant object at the top of preload.js and import/require it in main.js:
  const CHANNELS = { FILE_READ: 'file:read', DIALOG_OPEN: 'dialog:open', STORE_GET: 'store:get' }
- preload.js: contextBridge.exposeInMainWorld('api', { one method per CHANNEL entry, each
  calling ipcRenderer.invoke(CHANNELS.X, ...args) }).
- main.js: one ipcMain.handle(CHANNELS.X, async (event, ...args) => { ... }) per exposed method.
  Every channel in the bridge MUST have a handler. Every handler MUST have a channel in the bridge.
- IPC handlers MUST return { data, error } — never throw. Unhandled throws in ipcMain handlers
  cause silent failures in the renderer because IPC errors don't propagate as JS exceptions.
- Channel naming convention: 'verb:noun' in kebab-case (file:read, dialog:open, store:set).
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
IPC contract discipline (the #1 Tauri failure mode is silent 404 from missing registration):
- Every Rust function exposed to the frontend MUST have BOTH #[tauri::command] AND be listed
  in .invoke_handler(tauri::generate_handler![cmd1, cmd2, ...]). Missing from generate_handler
  gives a silent 404 with no error message — this is the most common Tauri bug.
- The string passed to invoke() MUST exactly match the Rust function name in snake_case:
  invoke('get_user_data') requires fn get_user_data() in Rust. No camelCase in invoke().
- invoke() argument object keys MUST exactly match Rust parameter names:
  invoke('save_file', { filePath: x }) requires fn save_file(file_path: String) — WRONG.
  invoke('save_file', { file_path: x }) requires fn save_file(file_path: String) — CORRECT.
- All commands MUST return Result<T, String>. Use Err("description".to_string()) for errors.
  Err(String) maps to a JavaScript rejected Promise — catch with .catch() or try/await.
- Organize all commands in src-tauri/src/commands.rs, pub use them in main.rs.
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

    "film": """\
Stack: Film / video render via ffmpeg (LLM-as-director → ffmpeg-as-renderer)
You do NOT emit HTML and you do NOT emit raw video bytes. You are the DIRECTOR: you
write a deterministic ffmpeg edit script plus a machine-readable shot list/manifest
that the harness feeds to ffmpeg to render the final film. Write every file completely.
- The single most important rule: for EVERY output video file the task declares
  (*.mp4 / *.webm / *.mov), emit a line that starts EXACTLY with "ffmpeg " (lowercase,
  one trailing space) and ends with that output file path as the LAST token. The harness
  scans for the first "ffmpeg " line and substitutes the real output path for that last
  token, so the output path MUST be the final argument and nothing may follow it.
- Put the ffmpeg command(s) in a render script named render.sh (or build_film.sh). One
  command per line.
- STILLS-TO-MOTION CONTRACT (most important visual rule): the upstream asset task produces
  a SMALL number of still images (typically 1-3 per scene) under frames/ (e.g.
  frames/scene1_still_01.png) — NOT a per-frame sequence. Generating one image per video
  frame is infeasible here (~16s per still), so a scene NEVER has hundreds of frames; you
  MUST synthesize the scene's motion and duration FROM those few stills with ffmpeg. Animate
  each still with a slow Ken Burns zoom/pan via the `zoompan` filter, and transition between
  stills with `xfade`. Proven working pattern on this host (ffmpeg 8.1.1) for two stills:
    ffmpeg -y -loop 1 -t <half> -i frames/still_01.png -loop 1 -t <half> -i frames/still_02.png \\
      -i audio/<bed>.wav -filter_complex \\
      "[0:v]scale=2560:1440:force_original_aspect_ratio=increase,crop=2560:1440,zoompan=z='min(zoom+0.0012,1.3)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1280x720:fps=24,setsar=1[v0];\\
        [1:v]scale=2560:1440:force_original_aspect_ratio=increase,crop=2560:1440,zoompan=z='min(zoom+0.0012,1.3)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1280x720:fps=24,setsar=1[v1];\\
        [v0][v1]xfade=transition=fade:duration=0.5:offset=<half-0.5>,format=yuv420p[v]" \\
      -map "[v]" -map 2:a -t <dur> -c:v libx264 -crf 20 -pix_fmt yuv420p -c:a aac -b:a 128k <out>.mp4
  For a single still, drop the second input and the `xfade` (one zoompan stream straight to
  output). It is a HARD ERROR to substitute a synthetic lavfi VIDEO source (color=, testsrc=,
  smptebars, nullsrc) for the stills — the harness REJECTS such a render and the task fails.
  Only when NO stills exist as a dependency may you fall back to a synthetic lavfi background.
- A synthetic AUDIO bed is always fine: pair the frame video with `-f lavfi -i
  aevalsrc=...` (or use the upstream audio WAV) for sound.
- All input/output paths are relative to the scene dir (the harness runs the script
  there): `frames/...`, `audio/...`, `video/...`. Do NOT reference files that no task
  produces.
- Always set: -y (overwrite), -pix_fmt yuv420p (broad compatibility), -movflags +faststart
  for mp4, and an explicit -t <seconds> or duration on every lavfi input so the render
  terminates. Encode video with libx264 and audio with aac.
- For titles/credits use drawtext with a fontsize and fontcolor; for multi-shot films build
  the timeline with the concat or xfade filtergraph rather than many intermediate files.
- shotlist.json (REQUIRED manifest): an array of shots, each
  { "id", "duration_seconds", "description", "source" (the lavfi/source spec),
  "audio" (sine spec or "none"), "transition" ("cut"|"fade"|"xfade") }. This documents the
  director's intent so the render is reviewable and reproducible.
- README.md: explain how to render locally (bash render.sh) and that the harness runs the
  ffmpeg line automatically; note that ffmpeg must be installed.
- If (and only if) the task explicitly requires a Python render pipeline instead of a
  render.sh, name the entry script render_scene.py (or render.py) and write EVERY local
  module it imports, completely, in the same task — the harness executes
  `python render_scene.py` and a missing module is an immediate ModuleNotFoundError failure.
  CRITICAL: a Python render script MUST call `subprocess.run(CMD, check=True)` — it must
  EXECUTE ffmpeg, not print the command. `print(cmd)` or `print(shlex.join(cmd))` produces
  NO output file and causes an immediate harness failure. The output file must exist on disk
  when the script exits 0.
- Windows ffmpeg constraints (this host runs Windows + ffmpeg 8.1.1):
  * fontconfig is NOT installed — omit `drawtext` entirely; use solid `color=` backgrounds.
  * `geq=` filter inside `filter_complex` fails with "Invalid argument" — use simple
    `color=c=RRGGBB` instead of gradient expressions.
  * Font paths with `:` (e.g. `C:/Windows/Fonts/arial.ttf`) break option parsing — skip
    `fontfile=` entirely.
  * Animate the stills (NOT a frame sequence): `-loop 1 -t <secs> -i "frames/<still>.png"`
    per still with a `zoompan` Ken Burns move, joined by `xfade`, paired with the upstream
    audio WAV (or `-f lavfi -i "aevalsrc=exprs=EXPR:c=mono:s=44100:d=N"`). Use `-pix_fmt
    yuv420p`. A synthetic `color=` video source is ONLY acceptable as a last resort when the
    task has no still-image dependency.
- NEVER emit placeholder ffmpeg flags, NEVER leave the output path unresolved, and NEVER
  produce an index.html — this is a film, not a web page.
""",

    "video-editor": """\
Stack: Video editing / compositing via ffmpeg (LLM-as-editor → ffmpeg-as-renderer)
You do NOT emit HTML and you do NOT emit raw video bytes. You are the EDITOR: you write a
deterministic ffmpeg edit script (cuts, trims, concatenation, transitions, overlays,
audio mixing) plus a machine-readable edit decision list that the harness renders with
ffmpeg. Write every file completely.
- The single most important rule: for EVERY output video file the task declares
  (*.mp4 / *.webm / *.mov), emit a line that starts EXACTLY with "ffmpeg " (lowercase, one
  trailing space) and ends with the output file path as the LAST token. The harness scans
  for the first "ffmpeg " line and replaces that last token with the real output path, so
  the output path MUST be the final argument with nothing after it.
- Put the ffmpeg command(s) in edit.sh. One command per line. Prefer a single filtergraph
  (-filter_complex) over many intermediate render passes. Common editor operations:
  * Trim/cut: trim=start=..:end=.. , atrim for audio, setpts/asetpts to reset timestamps.
  * Concatenate clips: the concat filter (for differing codecs) or concat demuxer (matching
    codecs). When inputs may be absent in CI, fall back to self-contained lavfi sources
    (color=, testsrc=, sine=) so the render still succeeds without external assets.
  * Transitions: xfade (video) + acrossfade (audio) between segments.
  * Overlays / picture-in-picture / lower-thirds: overlay + drawtext.
  * Audio mix: amix / amerge to combine music + voiceover; volume to balance levels.
- Always set -y, -pix_fmt yuv420p, -movflags +faststart (mp4), encode with libx264 + aac,
  and give every synthetic input an explicit duration (-t) so the render terminates.
- edl.json (REQUIRED edit decision list): an array of operations, each
  { "op" ("trim"|"concat"|"overlay"|"transition"|"audio_mix"), "inputs", "params",
  "output_segment" }. This makes the edit reviewable and reproducible.
- README.md: how to render locally (bash edit.sh), note the harness runs the ffmpeg line
  automatically and that ffmpeg must be installed.
- NEVER leave the output path unresolved, NEVER emit placeholder/example flags, and NEVER
  produce an index.html — this is a rendered video, not a web page.
""",

    "devops": """\
Stack: DevOps / infrastructure (Dockerfile, Docker Compose, nginx, CI/CD, environment config)
Generate production-ready infrastructure files for the project. Write every file completely.

DOCKERFILE (multi-stage, non-root):
- Stage 1 "builder": install all dependencies (npm install or pip install -r requirements.txt).
- Stage 2 "production": copy only built artifacts + runtime deps; create non-root user with useradd -r and run as that user.
- EXPOSE the application port. Set CMD or ENTRYPOINT to start the app.
- For Node.js: use node:20-alpine base. For Python: use python:3.12-slim. For static sites: use nginx:1.25-alpine.

DOCKER-COMPOSE (docker-compose.yml):
- Define services: app (main), db (if PostgreSQL/MySQL needed), redis (if caching needed), nginx (if reverse proxy needed).
- Use env_file: [".env"] for secrets. Never hardcode secrets in docker-compose.yml.
- Persist data with named volumes for databases.
- healthcheck on the app service: test: ["CMD", "curl", "-f", "http://localhost:<port>/health"], interval: 30s, timeout: 10s, retries: 3.

NGINX (nginx.conf):
- Reverse proxy to app on internal port.
- gzip compression: gzip on; gzip_types text/plain text/css application/json application/javascript.
- Cache static assets: location ~* \.(js|css|png|svg|ico)$ { expires 1y; add_header Cache-Control "public, immutable"; }
- Security headers: X-Frame-Options DENY; X-Content-Type-Options nosniff; Referrer-Policy strict-origin-when-cross-origin.
- Rate limiting: limit_req_zone $binary_remote_addr zone=api:10m rate=100r/m; apply to API routes.

CI/CD (.github/workflows/ci.yml):
- Trigger on push to main and pull_request.
- Jobs: lint (eslint or flake8), test (jest or pytest), build (docker build), security (npm audit or bandit).
- Cache: actions/cache for node_modules or pip.
- Use actions/checkout@v4, actions/setup-node@v4 or actions/setup-python@v4.
- Report test results with a test summary step.

ENV CONFIG:
- .env.example: list every required env var with a placeholder value and comment explaining each.
- Include: APP_PORT, DATABASE_URL (if DB used), JWT_SECRET (if auth used), NODE_ENV or PYTHON_ENV, LOG_LEVEL.
- Never include real secrets in .env.example.
""",

    "documentation": """\
Stack: Documentation (README, API reference, JSDoc/docstrings, CHANGELOG)
Generate comprehensive documentation for the project. Write every file completely.

README.md:
- Title with project name as H1.
- Badges row: build status (GitHub Actions), license, version (use shields.io URLs with placeholder repo).
- One-paragraph description of what the project does and who it is for.
- ## Features — bullet list of key capabilities.
- ## Prerequisites — list Node/Python/Docker version requirements.
- ## Installation — numbered steps from git clone to running locally; include both dev and production paths.
- ## Usage — code examples showing the most common use cases; use fenced code blocks with language identifiers.
- ## API Reference — table for each endpoint: Method | Route | Description | Auth Required | Request Body | Response.
- ## Architecture — ASCII diagram showing how components connect (services, databases, frontends). Use box-drawing characters.
- ## Contributing — brief guide: fork → branch → PR.
- ## License — state MIT (or as appropriate).

JSDOC (for JavaScript/TypeScript files):
- Add JSDoc comments to every exported function and class.
- Include: @param with type and description, @returns with type and description, @throws if the function can throw, @example with a working code snippet.
- Format: /** ... */ block immediately before the function declaration.

PYTHON DOCSTRINGS (for Python files):
- Add Google-style docstrings to every public function, class, and method.
- Include: one-line summary, Args section, Returns section, Raises section (if applicable), Example section.
- Format: triple double-quotes, indented under the def/class line.

CHANGELOG.md:
- Header: # Changelog and a note "All notable changes to this project will be documented in this file."
- First entry: ## [Unreleased] with ### Added subsection listing the initial features.
- Follow Keep a Changelog format: ## [version] - YYYY-MM-DD, subsections: Added / Changed / Deprecated / Removed / Fixed / Security.
""",

    "swift": """\
iOS SwiftUI app generation rules:
Project structure:
- ContentView.swift: root @main App struct + primary SwiftUI View hierarchy
- Models/: Swift structs conforming to Codable, Identifiable where needed
- Views/: individual SwiftUI View structs — one per screen
- ViewModels/: @ObservableObject classes with @Published properties (MVVM)
- Services/: data layer — URLSession calls, UserDefaults, CoreData if needed
- App.swift: @main entry point with WindowGroup/NavigationStack

SwiftUI rules (follow strictly):
- Use @State for local, @StateObject for owned view models, @EnvironmentObject for shared state
- NavigationStack + .navigationDestination(for:) for navigation (iOS 16+, not NavigationView)
- List + ForEach + .onDelete for CRUD lists
- Async/await for network calls — async throws functions, Task {} in .onAppear
- Error handling: Result<T, Error> or try/catch with @State var showError: Bool
- Persistence: UserDefaults for simple KV, SwiftData/CoreData for structured data

Verification note: verification is always "none" — Xcode cannot run headless in this pipeline.
Include a README.md with: File → New → Project → select the right target → drag all .swift files in → run on simulator.
""",

    "kotlin": """\
Android Jetpack Compose app generation rules:
Project structure:
- app/src/main/java/<package>/MainActivity.kt: @AndroidEntryPoint Activity with setContent { AppTheme { NavHost } }
- app/src/main/java/<package>/ui/screens/: Composable screen functions — one per screen
- app/src/main/java/<package>/ui/components/: reusable @Composable functions
- app/src/main/java/<package>/viewmodel/: ViewModel subclasses with StateFlow<UiState>
- app/src/main/java/<package>/data/: Repository pattern — Room DAO + Retrofit/OkHttp
- app/src/main/res/: standard Android resources directory
- app/build.gradle.kts: dependency declarations
- build.gradle.kts (root): project-level build config

Jetpack Compose rules (follow strictly):
- Use rememberSaveable for state that survives recomposition, viewModel() for ViewModel injection
- Navigation: NavController + NavHost + composable("route") — pass by id, not object
- State: sealed class UiState, ViewModel exposes StateFlow<UiState>, collect with collectAsStateWithLifecycle()
- Side effects: LaunchedEffect(key) for coroutines, DisposableEffect for cleanup
- Lists: LazyColumn + items(list) { item -> } — never RecyclerView in Compose
- Persistence: Room database with @Entity, @Dao, @Database; inject via Hilt (@HiltViewModel)

Verification note: verification is always "none" — Android build cannot run headless in this pipeline.
Include a README.md with Android Studio setup instructions and minimum SDK version (API 26 / Android 8.0).
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

# ── Complexity router ─────────────────────────────────────────────────────────

# Paid-call budget: shared across all worker threads in one project run. Reset at project
# start (reset_paid_budget) and consumed by each non-ollama attempt (reserve_paid_call).
_paid_calls_made = 0
_paid_lock = threading.Lock()

# OAuth-rung (flat-rate subscription) capacity: separate from the dollar budget above.
# OAuth providers (codex) don't consume dollars, but subscription quotas are finite, so
# each provider gets a per-run capacity cap. _codex_disabled latches once a run hits an
# auth/quota failure so subsequent attempts skip the rung cheaply instead of re-probing.
_oauth_calls_made: dict[str, int] = {}
_oauth_lock = threading.Lock()
_codex_disabled = False
_grok_disabled = False
_claude_cli_disabled = False

# Per-role Codex sub-caps (Phase 4). Codex capacity is shared between two caller roles:
# - planning: Creative Director / Technical Architect calls via planning_call (bounded by
#   CODEX_PLANNING_RESERVE in orchestrator.py's _codex_planning_calls counter, not here)
# - worker: worker rescue Codex calls (bounded here by CODEX_WORKER_RESERVE)
# No lending: a planning overflow routes to Sonnet (not the worker budget); a worker
# overflow routes to Sonnet (not the planning budget). Both counters reset on reset_paid_budget().
# The outer CODEX_CLI_MAX_CALLS cap in _reserve_oauth_call() remains as the shared guard.
_codex_worker_calls = 0

# Single-flight lock for Grok: xAI rotates the OAuth refresh token on each use, so concurrent
# `grok -p` subprocesses would race and invalidate the cached ~/.grok/auth.json session. Serialize
# Grok CLI calls behind this lock so only one runs at a time (Codex needs no such lock).
_grok_call_lock = threading.Lock()

# Single-flight lock for the Claude Max CLI: serialize `claude -p` calls so parallel workers can't
# burn the operator's shared interactive Max usage pool before the usage-limit latch propagates to
# future reservations, and can't race Claude Code's local session/config state.
_claude_cli_call_lock = threading.Lock()

# Credentials scrubbed from the `claude -p` subprocess env. Claude Code's auth precedence puts an
# API key / Bedrock / Vertex routing AHEAD of the subscription OAuth in non-interactive mode, so if
# this repo's metered-rung ANTHROPIC_API_KEY (or a cloud-routing var) leaks into the call, the
# "free" Max rung silently becomes a METERED API call — defeating the whole rung. Stripped so
# `claude -p` falls back to the subscription OAuth (or a dedicated CLAUDE_CLI_HOME config dir).
_CLAUDE_CLI_ENV_BLOCKLIST = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "AWS_BEARER_TOKEN_BEDROCK",
})


def reset_paid_budget() -> None:
    """Reset the per-project paid (cloud) worker-call counter. Call at project start."""
    global _paid_calls_made, _codex_disabled, _grok_disabled, _claude_cli_disabled, _codex_worker_calls
    with _paid_lock:
        _paid_calls_made = 0
    with _oauth_lock:
        _oauth_calls_made.clear()
        _codex_disabled = False
        _grok_disabled = False
        _claude_cli_disabled = False
        _codex_worker_calls = 0


def _reserve_paid_call() -> bool:
    """Atomically reserve one paid worker call. Returns False if the budget is exhausted."""
    global _paid_calls_made
    with _paid_lock:
        if _paid_calls_made >= MAX_PAID_WORKER_CALLS:
            return False
        _paid_calls_made += 1
        return True


def _reserve_oauth_call(provider: str) -> bool:
    """Atomically reserve one OAuth (flat-rate) worker call against this provider's per-run
    capacity cap. Returns False if the rung has been latched off (an auth/quota failure earlier
    in the run) or the cap is reached. Checking the disable-latch and reserving capacity under
    the SAME lock is what makes the latch hold under parallel workers — otherwise one worker
    could read _codex_disabled==False, another could latch it True, and the first would still
    reserve and launch `codex exec`. Does NOT touch the dollar budget."""
    if provider == "codex":
        cap = CODEX_CLI_MAX_CALLS
    elif provider == "grok":
        cap = GROK_MAX_CALLS
    elif provider == "claude_cli":
        cap = CLAUDE_CLI_MAX_CALLS
    else:
        cap = 0
    with _oauth_lock:
        if provider == "codex" and _codex_disabled:
            return False
        if provider == "grok" and _grok_disabled:
            return False
        if provider == "claude_cli" and _claude_cli_disabled:
            return False
        made = _oauth_calls_made.get(provider, 0)
        if made >= cap:
            return False
        _oauth_calls_made[provider] = made + 1
        return True


def _oauth_enabled(provider: str) -> bool:
    """Per-provider global enable flag (config constant, no race). An OAuth rung that's globally
    off is skipped without touching capacity — mirrors how the codex rung ships inert until opted in."""
    if provider == "codex":
        return CODEX_CLI_ENABLED
    if provider == "grok":
        return GROK_CLI_ENABLED
    if provider == "claude_cli":
        return CLAUDE_CLI_ENABLED
    return False


def _strongest_local_rung() -> int:
    """Index of the strongest ollama (local, free) rung in the ladder, or top if none."""
    local = [i for i, (prov, _) in enumerate(WORKER_LADDER) if prov == "ollama"]
    return local[-1] if local else len(WORKER_LADDER) - 1


def route_task(task) -> int:
    """Pick the *base* worker ladder rung (0 = weakest) from task complexity.

    Base routing is always LOCAL — a task never starts on a paid cloud rung. Genuinely hard
    tasks reach cloud only via escalation-on-retry (see routed_rung), i.e. after a local
    attempt has actually failed verification. This keeps the system local-first by default.
      - rung 0 (cheapest local): trivial single-file scaffold/style/data/config
      - strongest-local rung: everything else (the normal-code workhorse)
    """
    if not WORKER_LADDER:
        return 0
    local_top = _strongest_local_rung()
    ttype = (getattr(task, "type", "") or "").lower()
    n_files = len(getattr(task, "files", []) or [])

    if ttype in LOCAL_FIRST_TASK_TYPES and n_files <= 1:
        return 0
    return local_top


def routed_rung(task) -> int:
    """Effective ladder rung = base complexity rung + retry_count, capped at the top.

    The +retry_count term is what makes retries *escalate*: each failed attempt bumps the
    task one rung up the ladder (e.g. 14b → Sonnet) instead of re-running the same model.
    """
    if not WORKER_LADDER:
        return 0
    return min(route_task(task) + getattr(task, "retry_count", 0), len(WORKER_LADDER) - 1)


def execute_task(
    task,
    spec: dict,
    dependency_files: dict[str, dict[str, str]],
    context: dict | None = None,
) -> dict:
    """
    Ask the worker model to implement a task.
    Returns {"files": [...], "model_used": "<provider>/<model>"}.

    Routing: a complexity-based ladder (WORKER_LADDER) selects a starting rung; the chain
    runs from there up to the strongest rung, escalating one rung per retry. Falls back to
    the legacy WORKER_PROVIDER + WORKER_FALLBACKS chain if WORKER_LADDER is unset.
    Raises ValueError immediately on bad JSON format so the scheduler can send EXECUTION_ERROR.
    """
    global _codex_disabled, _grok_disabled, _claude_cli_disabled, _codex_worker_calls  # latched off when an OAuth rung hits auth/quota mid-run
    arch  = spec.get("architecture", {})
    stack = arch.get("stack", "vanilla")
    # For full-stack projects, pick the sub-stack based on task type
    if stack == "full-stack":
        task_type = getattr(task, "type", "") or ""
        if task_type == "auth":
            effective_stack = "auth"
        elif task_type in ("backend", "api", "database", "config"):
            effective_stack = "fastapi"
        elif task_type == "devops":
            effective_stack = "devops"
        elif task_type == "documentation":
            effective_stack = "documentation"
        else:
            effective_stack = "react-vite"
    elif stack in _STACK_PROMPTS:
        task_type = getattr(task, "type", "") or ""
        if task_type == "devops":
            effective_stack = "devops"
        elif task_type == "documentation":
            effective_stack = "documentation"
        else:
            effective_stack = stack
    else:
        effective_stack = stack
    system_prompt = _SYSTEM_PROMPT + "\n" + _STACK_PROMPTS.get(effective_stack, _STACK_PROMPTS["vanilla"])
    user_message = _build_user_message(task, spec, dependency_files, context)

    # Prepend pre-emptive hints from past escalation patterns for this task type + stack.
    # Gives the weakest model a chance to avoid known failure modes before its first attempt.
    _hints = get_worker_hints(getattr(task, "type", ""), effective_stack)
    if _hints:
        user_message += "\n\nPAST FAILURE PATTERNS FOR THIS TASK TYPE:\n" + "\n".join(
            f"- {h}" for h in _hints
        )

    # Build attempt chain. With a ladder configured: start at the routed rung and walk UP to
    # the strongest rung (escalation). If the chain is all-cloud, append the strongest LOCAL
    # rung as a last-ditch attempt so a host without an API key (or one over its paid budget)
    # still degrades to local output instead of failing outright.
    if WORKER_LADDER:
        effective = routed_rung(task)
        attempts = list(WORKER_LADDER[effective:])
        if not any(prov == "ollama" for prov, _ in attempts):
            local_rungs = [r for r in WORKER_LADDER if r[0] == "ollama"]
            if local_rungs:
                attempts.append(local_rungs[-1])
    else:
        # Legacy behaviour: primary first, then fallbacks.
        attempts = [(WORKER_PROVIDER, WORKER_MODEL)] + list(WORKER_FALLBACKS)

    last_err: Exception | None = None
    failed_attempts: list[tuple[str, str, str]] = []  # (provider, model, error)
    tokens_by_model: dict[str, dict[str, int]] = {}  # accumulated per-attempt token counts

    def _accum_tokens(label: str) -> None:
        """Read thread-local call tokens and accumulate into tokens_by_model keyed by label."""
        tok = _get_call_tokens()
        if label not in tokens_by_model:
            tokens_by_model[label] = {"input": 0, "output": 0}
        tokens_by_model[label]["input"]  += tok["input"]
        tokens_by_model[label]["output"] += tok["output"]

    for provider, model in attempts:
        # Gate the attempt by provider class. METERED providers (anthropic/openrouter) draw
        # on the per-project dollar budget; OAUTH providers (codex) draw on a separate flat-rate
        # capacity cap and never spend dollars; ollama (local) is ungated.
        if provider in METERED_PROVIDERS:
            if not _reserve_paid_call():
                console.print(
                    f"  [yellow]Paid-call budget ({MAX_PAID_WORKER_CALLS}) exhausted — "
                    f"skipping {provider}/{model}, staying local.[/yellow]"
                )
                continue
        elif provider in OAUTH_PROVIDERS:
            # Globally off? skip without touching capacity (config constant, no race).
            if not _oauth_enabled(provider):
                continue
            # Per-role worker sub-cap (Phase 4): worker rescue Codex calls are bounded by
            # CODEX_WORKER_RESERVE (separate from the planning reserve used by CodexOrchestrator
            # and planning_call). Check and increment atomically under _oauth_lock so parallel
            # workers don't race the counter. No lending — overflow routes to next rung.
            if provider == "codex":
                with _oauth_lock:
                    if _codex_worker_calls >= CODEX_WORKER_RESERVE:
                        console.print(
                            f"  [yellow]Codex worker reserve ({CODEX_WORKER_RESERVE}) exhausted "
                            f"for this run — skipping codex worker rung, escalating to next.[/yellow]"
                        )
                        continue
                    _codex_worker_calls += 1
            # The disable-latch check AND the capacity reservation happen atomically inside
            # _reserve_oauth_call, so a parallel worker can't launch the CLI after the latch
            # flips. A False here means "disabled or capacity exhausted" — either way skip.
            if not _reserve_oauth_call(provider):
                console.print(
                    f"  [yellow]{provider} unavailable (auth/quota latch) or per-run capacity "
                    f"exhausted — skipping, escalating to next rung.[/yellow]"
                )
                if provider == "codex":
                    # _reserve_oauth_call failed after we pre-incremented the worker counter;
                    # undo the increment so we don't burn a worker slot on a failed reserve.
                    with _oauth_lock:
                        _codex_worker_calls = max(0, _codex_worker_calls - 1)
                continue
        try:
            _t0 = time.monotonic()
            raw = _call_provider(provider, model, system_prompt, user_message)
            parsed = _parse_and_validate(raw, task)
            label = model if provider == "ollama" else f"{provider}/{model}"
            parsed["model_used"] = label
            # Record escalation outcomes so future weak-model attempts can learn from them.
            if failed_attempts:
                # Phase 1: distill the rescue into a reusable lesson. Prefer the in-schema
                # `lesson` the rescuing model supplied; else derive a low-confidence one from the
                # diff (changed files) + the last failure — no extra paid call either way.
                _lesson = parsed.get("lesson") if isinstance(parsed.get("lesson"), dict) else None
                if not _lesson:
                    _changed = ", ".join(f.get("path", "") for f in parsed.get("files", [])[:6])
                    _lesson = {
                        "changed_files_summary": _changed,
                        "failure_pattern": (failed_attempts[-1][2] or "")[:160],
                        "confidence": "low",
                    }
                for fail_prov, fail_model, fail_err in failed_attempts:
                    log_escalation(
                        task_type=getattr(task, "type", "unknown"),
                        stack=effective_stack,
                        failed_model=f"{fail_prov}/{fail_model}",
                        succeeded_model=f"{provider}/{model}",
                        error_summary=fail_err,
                        objective_summary=getattr(task, "objective", "")[:150],
                        lesson=_lesson,
                    )
            record_role_event("worker", provider=provider, model=model, success=True,
                              fallback=bool(failed_attempts), latency_s=time.monotonic() - _t0)
            _accum_tokens(label)
            if tokens_by_model:
                parsed["tokens_by_model"] = dict(tokens_by_model)
                # Persist tokens to mission_control.json without requiring scheduler changes.
                task_id = getattr(task, "id", None)
                if task_id:
                    try:
                        from state_writer import writer as _sw
                        _sw.on_task_tokens(task_id, tokens_by_model)
                    except Exception:  # noqa: BLE001
                        pass  # telemetry failure must never abort a successful task
            return parsed

        except ValueError:
            record_role_event("worker", provider=provider, model=model, success=False,
                              schema_fail=True, latency_s=time.monotonic() - _t0)
            # Persist the tokens spent so far (this attempt + any prior failed attempts) before the
            # bad-output ValueError propagates to the scheduler. _accum_tokens drains the thread-local
            # (so it can't leak into the next call) AND records the count — otherwise a task that
            # ultimately schema-fails under-reports its token usage in the dashboard (#105 follow-up #4).
            label_fail = model if provider == "ollama" else f"{provider}/{model}"
            _accum_tokens(label_fail)
            if tokens_by_model:
                task_id = getattr(task, "id", None)
                if task_id:
                    try:
                        from state_writer import writer as _sw
                        _sw.on_task_tokens(task_id, tokens_by_model)
                    except Exception:  # noqa: BLE001
                        pass  # telemetry failure must never mask the real ValueError
            raise  # Bad output format — let scheduler handle via EXECUTION_ERROR

        except Exception as exc:  # noqa: BLE001
            record_role_event("worker", provider=provider, model=model, success=False,
                              latency_s=time.monotonic() - _t0)
            label_fail = model if provider == "ollama" else f"{provider}/{model}"
            _accum_tokens(label_fail)  # capture any partial tokens from failed attempt
            # Infrastructure failure on Ollama (server unreachable) — do NOT escalate
            # to cloud. Anthropic escalation is for capability failures only (bad output,
            # wrong format). A down Ollama server should fail the task, not burn API credits.
            if provider == "ollama" and _is_ollama_unavailable(exc):
                raise RuntimeError(
                    f"Ollama unavailable at {OLLAMA_HOST} — start Ollama before running "
                    f"builds. Task will fail rather than escalate to paid cloud workers. "
                    f"({exc})"
                ) from exc
            # An OAuth rung that's unavailable (not logged in / quota / exe missing / timeout)
            # must NOT hard-raise like ollama — it skips cleanly to the next rung so the build
            # still completes. Latch the rung off for the rest of the run to avoid re-probing.
            if provider in OAUTH_PROVIDERS and _oauth_unavailable(provider, exc):
                with _oauth_lock:
                    if provider == "codex":
                        _codex_disabled = True
                    elif provider == "grok":
                        _grok_disabled = True
                    elif provider == "claude_cli":
                        _claude_cli_disabled = True
                console.print(
                    f"  [yellow]{provider} unavailable (auth/quota) — disabling {provider} rung "
                    f"for this run, escalating to next rung.[/yellow]"
                )
                failed_attempts.append((provider, model, str(exc)[:200]))
                continue
            last_err = exc
            failed_attempts.append((provider, model, str(exc)[:200]))
            console.print(
                f"  [yellow]Worker {provider}/{model} error: {exc!r} — trying fallback…[/yellow]"
            )
            continue

    raise RuntimeError(
        f"All worker providers exhausted. Last error: {last_err}"
    ) from last_err


# ── Provider dispatch ─────────────────────────────────────────────────────────

def _is_ollama_unavailable(exc: Exception) -> bool:
    """True when Ollama server is unreachable (infrastructure failure), not when the
    model produced bad output (capability failure). Prevents cloud escalation when
    Ollama is simply not running — Anthropic should only be reached on capability failures."""
    if isinstance(exc, (ConnectionError, ConnectionRefusedError, OSError)):
        return True
    if isinstance(exc, httpx.ConnectError):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in (
        "connection refused", "connect error", "cannot connect",
        "connection error", "no route to host", "failed to connect",
    ))


def _is_codex_unavailable(exc: Exception) -> bool:
    """True when the Codex OAuth rung is unavailable (exe missing, not logged in, quota /
    rate-limit hit, or the subprocess timed out) — these mean "skip to the next rung", NOT a
    capability failure. A bad-output ValueError is handled elsewhere and returns False here."""
    if isinstance(exc, (FileNotFoundError, subprocess.TimeoutExpired)):
        return True
    msg = str(exc).lower()
    # NB: match specific auth/quota phrases, NOT a bare "login" — bare "login" can appear in a
    # genuine capability failure (e.g. the task is writing a login form / auth code that Codex
    # echoes on a nonzero exit), which would wrongly skip the rung as "unavailable".
    return any(k in msg for k in (
        "not logged in", "unauthorized", "401", "403", "429", "rate limit",
        "usage limit", "quota", "please run codex login", "login required",
        "login to codex", "run codex login",
    ))


def _is_grok_unavailable(exc: Exception) -> bool:
    """True when the Grok OAuth rung is PERMANENTLY unavailable for this run (exe missing, not
    logged in, auth expired, subscription/daily quota exhausted, or the subprocess timed out) —
    these latch the rung off and skip to the next rung.

    Deliberately does NOT match a transient throttle (429 / "rate limit" / "too many requests" /
    "throttled"): SuperGrok enforces a shorter-window rate limit under burst, and a transient
    throttle must NOT trip the permanent disable-latch — it falls through to the generic handler
    which just escalates that one attempt to the next rung (a later task/retry can use Grok again).
    A bad-output ValueError is handled elsewhere and returns False here.
    (NB: differs from _is_codex_unavailable, which DOES treat 429/rate-limit as unavailable.)"""
    if isinstance(exc, (FileNotFoundError, subprocess.TimeoutExpired)):
        return True
    msg = str(exc).lower()
    # Transient throttle → NOT permanent-unavailable; let it escalate without latching.
    if any(k in msg for k in ("429", "rate limit", "too many requests", "throttl", "try again")):
        return False
    return any(k in msg for k in (
        "not logged in", "not signed in", "unauthorized", "401", "403",
        "please run grok login", "run grok login", "login required", "sign in to grok",
        "quota exhausted", "quota exceeded", "daily limit", "out of credits",
        "subscription required", "no auth",
    ))


def _is_claude_cli_unavailable(exc: Exception) -> bool:
    """True when the Claude Max CLI rung is unavailable for this run (exe missing, not logged in,
    or — the common case — the subscription's usage limit has been reached / a rate limit hit /
    the subprocess timed out). All of these mean "latch off, skip to the metered API rung", NOT a
    capability failure. Hitting the Max usage limit must latch (unlike a transient API 429) because
    the limit is a rolling window that won't clear within a build — and crucially it's the SAME pool
    the operator's interactive session uses, so we stop hammering it. A bad-output ValueError is
    handled elsewhere and returns False here."""
    if isinstance(exc, (FileNotFoundError, subprocess.TimeoutExpired)):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in (
        "not logged in", "please run /login", "run /login", "login required",
        "unauthorized", "401", "403", "429", "rate limit",
        "usage limit", "reached your usage", "limit reached", "quota",
        "out of credits", "subscription",
    ))


def _oauth_unavailable(provider: str, exc: Exception) -> bool:
    """Dispatch an OAuth-rung failure to the provider's availability classifier. True → latch the
    rung off + skip to next rung; False → treat as a normal capability failure (escalate)."""
    if provider == "codex":
        return _is_codex_unavailable(exc)
    if provider == "grok":
        return _is_grok_unavailable(exc)
    if provider == "claude_cli":
        return _is_claude_cli_unavailable(exc)
    return False


class _CodexTierUnavailable(Exception):
    """The Codex tier could not run / must be skipped: not enabled, latched off, OAuth capacity (or a
    caller-supplied reserve gate) exhausted, or an auth/quota/exe failure. Callers decide what to do
    with it (planning_call falls through to Anthropic; CodexOrchestrator converts it to RuntimeError)."""


class _CodexTierInvalid(Exception):
    """Codex produced output that failed validation on both attempts (a capability failure)."""


def _codex_tier(system: str, user: str, validate_fn, *, role: str,
                model: str | None = None, reserve_attempt=None) -> dict:
    """Run the Codex ($0 OAuth) tier — the protocol SHARED by planning_call and CodexOrchestrator:
    up to 2 attempts (one same-tier retry for output-wrapping/truncation), each reserving capacity,
    shelling to `codex exec`, parsing via llm_json.loads_llm_json_object (which preserves BOTH the
    trailing-prose and the in-string-escape tolerances the two call sites previously did differently),
    and gating on validate_fn. Returns the validated dict.

    Raises `_CodexTierUnavailable` when Codex is disabled / latched / capacity-exhausted or hits an
    auth/quota/exe failure (latching `_codex_disabled` in that last case); raises `_CodexTierInvalid`
    when output fails validation on both attempts. `reserve_attempt()` is called once per attempt
    BEFORE the call and must reserve capacity, raising `_CodexTierUnavailable` if it can't — it
    defaults to a plain `_reserve_oauth_call('codex')`. Codex-only: never falls back to Anthropic
    (the caller owns escalation)."""
    from llm_json import loads_llm_json_object
    global _codex_disabled

    if not _oauth_enabled("codex"):
        raise _CodexTierUnavailable("Codex CLI not enabled")

    if reserve_attempt is None:
        def reserve_attempt():
            if not _reserve_oauth_call("codex"):
                raise _CodexTierUnavailable("Codex latched off or OAuth capacity exhausted")

    _model = model or CODEX_MODEL
    last_err: Exception | None = None
    for _attempt in range(2):
        reserve_attempt()  # may raise _CodexTierUnavailable (capacity / caller-supplied gate)
        _t0 = time.monotonic()
        try:
            raw = _call_codex(_model, system, user)
            parsed = loads_llm_json_object(raw)
            validate_fn(parsed)
            record_role_event(role, provider="codex", model=_model, success=True,
                              latency_s=time.monotonic() - _t0)
            return parsed
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if _is_codex_unavailable(exc):
                with _oauth_lock:
                    _codex_disabled = True
                record_role_event(role, provider="codex", model=_model, success=False,
                                  latency_s=time.monotonic() - _t0)
                raise _CodexTierUnavailable(str(exc)) from exc
            # capability / validation failure → record schema_fail and retry once at this tier
            record_role_event(role, provider="codex", model=_model, success=False,
                              schema_fail=True, latency_s=time.monotonic() - _t0)
    raise _CodexTierInvalid(f"Codex failed validation after 2 attempts: {last_err}")


def planning_call(
    system: str,
    user: str,
    validate_fn,
    *,
    role: str = "planning",
    codex_model: str | None = None,
    sonnet_model: str = "claude-sonnet-4-6",
    opus_model: str = OPUS_MODEL,
) -> dict:
    """Codex-first planning helper for strict-schema control-plane roles (Creative Director,
    Technical Architect, later the orchestrator). Tries the cheapest *reliable* tier first:

        Codex (free OAuth) → one same-tier retry → Anthropic Sonnet → Anthropic Opus

    Each candidate's parsed JSON is gated by `validate_fn(parsed)` — role-specific validation that
    raises on a bad shape — NOT mere parse success, so a plausible-but-wrong plan is rejected and
    escalates. Codex draws the shared OAuth reservation + disable-latch (no quota bypass); a Codex
    capability/schema failure retries once at the same tier, while a Codex *unavailability*
    (auth/quota/exe) latches the rung and falls straight through to Anthropic. Planning NEVER hard-
    fails on Codex quota — it always has the Anthropic fallback. Raises RuntimeError only if every
    tier fails validation.

    Wired as of Phase 3 — Creative Director + Technical Architect route through this (Phase 4 adds
    the per-role Codex sub-budget). Returns the validated dict.
    """
    last_err: Exception | None = None

    # Tier 1 — Codex (free OAuth), with one same-tier retry, via the shared Codex-tier helper.
    if _oauth_enabled("codex"):
        try:
            return _codex_tier(system, user, validate_fn, role=role, model=codex_model)
        except (_CodexTierUnavailable, _CodexTierInvalid) as exc:
            last_err = exc  # Codex unavailable or failed validation → fall through to Anthropic

    # Tier 2/3 — Anthropic Sonnet → Opus (paid), validated. fallback=True marks the cross-tier hop.
    for model in (sonnet_model, opus_model):
        _t0 = time.monotonic()
        try:
            raw = _call_anthropic(model, system, user, label=role)
            parsed = _loads_tolerant(_strip_fences(raw))
            validate_fn(parsed)
            record_role_event(role, provider="anthropic", model=model, success=True,
                              fallback=True, latency_s=time.monotonic() - _t0)
            return parsed
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            record_role_event(role, provider="anthropic", model=model, success=False,
                              schema_fail=True, fallback=True, latency_s=time.monotonic() - _t0)
            continue

    raise RuntimeError(f"planning_call({role}) exhausted all tiers. Last error: {last_err}") from last_err


def _call_provider(provider: str, model: str, system: str, user: str) -> str:
    if provider == "ollama":
        return _call_ollama(model, system, user)
    if provider == "anthropic":
        return _call_anthropic(model, system, user)
    if provider == "openrouter":
        return _call_openrouter(model, system, user)
    if provider == "codex":
        return _call_codex(model, system, user)
    if provider == "grok":
        return _call_grok(model, system, user)
    if provider == "claude_cli":
        return _call_claude_cli(model, system, user)
    raise ValueError(f"Unknown worker provider: {provider!r}")


def _call_ollama(model: str, system: str, user: str) -> str:
    from permissions import observe
    observe("llm_local", detail=f"ollama chat ({model})")  # roadmap #6: observe-only
    # Bound the request so a hung local generation can't stall the pipeline indefinitely.
    client = ollama.Client(host=OLLAMA_HOST, timeout=WORKER_TASK_TIMEOUT)
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        format="json",
        options={"temperature": 0.15, "num_predict": 8192},
    )
    from cost import record_ollama_usage
    prompt_toks = getattr(response, "prompt_eval_count", None) or 0
    eval_toks   = getattr(response, "eval_count", None) or 0
    record_ollama_usage(prompt_tokens=prompt_toks, eval_tokens=eval_toks)
    _set_call_tokens(prompt_toks, eval_toks)
    return response.message.content.strip()


def _call_anthropic(model: str, system: str, user: str, label: str = "worker-esc") -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot use anthropic worker provider")
    import anthropic
    from cache_telemetry import log_cache_usage
    from cost import record_usage
    # Bound the request like the ollama client (worker.py:_call_ollama) so a hung
    # escalation call can't stall the pipeline indefinitely; on timeout the SDK
    # raises and main.py's handler writes a terminal FAILED state.
    # `label` attributes the cost to the calling role (worker-esc by default; planning_call
    # passes the planning role so CD/TA/orchestrator Anthropic fallbacks aren't mis-labelled).
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=WORKER_TASK_TIMEOUT)
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    log_cache_usage(response.usage, label)
    record_usage(response.usage, model, label)
    inp = getattr(response.usage, "input_tokens", 0) or 0
    out = getattr(response.usage, "output_tokens", 0) or 0
    _set_call_tokens(inp, out)
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


def _call_codex(model: str, system: str, user: str) -> str:
    """Run a one-shot read-only `codex exec` under the operator's ChatGPT subscription.

    Mirrors _call_ollama's (model, system, user) -> str contract. The combined prompt is piped
    on stdin; the clean final message (the JSON contract) is captured via `-o <tmpfile>`. The
    sandbox is read-only so Codex writes no project files. Telemetry is recorded at $0 via
    record_oauth_usage. The CODEX_TIMEOUT bounds the subprocess so a hang can't stall the build.
    """
    exe = shutil.which("codex") or shutil.which("codex.cmd") or "codex"
    fd, tmpfile = tempfile.mkstemp(suffix=".txt", prefix="codex_out_")
    os.close(fd)
    cmd = [
        exe, "exec", "--skip-git-repo-check", "--ephemeral",
        "-s", "read-only", "-o", tmpfile, "-m", (model or CODEX_MODEL),
    ]
    if CODEX_EFFORT:
        cmd.extend(["-c", f'model_reasoning_effort="{CODEX_EFFORT}"'])
    cmd.append("-")  # read the prompt from stdin

    env = {**os.environ}
    if CODEX_HOME:
        env["CODEX_HOME"] = CODEX_HOME

    from permissions import observe
    observe("llm_cli", detail=f"codex exec ({model or CODEX_MODEL})")  # roadmap #6: observe-only
    from cost import record_oauth_usage
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            input=(system + "\n\n" + user),
            capture_output=True,
            text=True,
            # Force UTF-8 for stdin/stdout. Without this, text mode uses the Windows locale
            # encoding (cp1252) — real worker prompts contain non-cp1252 glyphs (arrows, bullets,
            # box chars), and `codex exec` reads stdin strictly as UTF-8, so it rejects the prompt
            # with "input is not valid UTF-8". errors="replace" keeps a stray output byte from
            # crashing the decode side too.
            encoding="utf-8",
            errors="replace",
            timeout=CODEX_TIMEOUT,
            env=env,
            cwd=os.getcwd(),
        )
        if result.returncode != 0:
            tail = ((result.stderr or "") + (result.stdout or ""))[-300:]
            raise RuntimeError(
                f"codex exec exited {result.returncode}: ...{tail}"
            )
        try:
            with open(tmpfile, "r", encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            text = ""
        if not text:
            text = (result.stdout or "").strip()
        record_oauth_usage("codex", success=True, latency_s=time.monotonic() - start, tokens=0)
        # Codex CLI does not expose per-call token counts; record 0 so the per-task
        # accumulator still sees an entry for this model.
        _set_call_tokens(0, 0)
        return text
    except BaseException:
        # Count attempted invocations too — a failed call (exe missing, timeout, nonzero exit,
        # auth/quota) is what trips the disable-latch, so it must be visible in cost telemetry.
        # "calls" = attempts; the success counter tracks how many of those actually returned.
        record_oauth_usage("codex", success=False, latency_s=time.monotonic() - start, tokens=0)
        raise
    finally:
        try:
            os.remove(tmpfile)
        except OSError:
            pass


def _extract_grok_text(stdout: str) -> str:
    """Pull the final assistant message out of `grok -p --output-format json` output.

    The confirmed envelope is a single JSON object with the message in "text"
    (e.g. {"text": "...", "stopReason": "EndTurn", ...}). Falls back defensively: other dict
    shapes, a streaming-json event list, or — if it isn't JSON at all — the raw stdout, since the
    downstream _parse_and_validate hunts for the {"files":[...]} contract inside whatever it gets.

    Empty-text envelope: grok-build (a reasoning model) intermittently returns an envelope whose
    "text" is empty because the answer landed in "thought" (reasoning) instead — or it produced
    only reasoning and no final message. In that case we (1) fall back to "thought" as a last-resort
    content source, then (2) return "" rather than the raw envelope. Returning the envelope here was
    a bug: _parse_and_validate would parse it as a dict-without-"files" and raise the misleading
    "missing 'files' list. Got keys: ['text','stopReason',...]" — masking an empty generation behind
    a contract error and feeding an unfixable input to EXECUTION_ERROR refinement. "" routes it to an
    honest empty-output retry instead. The raw-stdout passthrough is preserved ONLY for the
    not-JSON-at-all case (plain text / streaming lines), where hunting for the contract is valid."""
    s = (stdout or "").strip()
    if not s:
        return ""
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return s  # plain text / streaming lines — let _parse_and_validate find the contract
    if isinstance(obj, dict):
        for key in ("text", "result", "response", "message", "output", "final", "thought"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # Recognized envelope but no text-bearing key held content (empty generation). Return ""
        # for an honest empty-output failure — NOT the raw envelope, which mis-parses as a
        # malformed contract and triggers a pointless refinement loop.
        return ""
    if isinstance(obj, list):
        for ev in reversed(obj):  # streaming events: last message-bearing event wins
            if isinstance(ev, dict):
                for key in ("text", "content", "message"):
                    v = ev.get(key)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
    return s


def _call_grok(model: str, system: str, user: str) -> str:
    """Run a one-shot headless `grok -p` under the operator's SuperGrok OAuth subscription ($0).

    Mirrors _call_ollama's (model, system, user) -> str contract. The combined prompt is passed via
    -p and the final message is read from the JSON envelope's "text" field. Authenticates purely
    via the cached ~/.grok/auth.json OAuth token (NO xAI API key) — flat-rate, $0 marginal. Runs in
    an isolated scratch cwd so the agentic CLI never touches the project tree, and holds the
    single-flight _grok_call_lock for the whole subprocess: xAI rotates the OAuth refresh token on
    each use, so concurrent calls would race the cached session. GROK_TIMEOUT bounds the call so a
    hang can't stall the build. UTF-8 is forced for the same reason as Codex (non-cp1252 glyphs)."""
    exe = (shutil.which("grok") or shutil.which("grok.exe")
           or os.path.join(os.path.expanduser(GROK_HOME or "~/.grok"), "bin", "grok.exe"))
    scratch = tempfile.mkdtemp(prefix="grok_cwd_")
    cmd = [exe, "-p", system + "\n\n" + user, "--output-format", "json", "--cwd", scratch]
    if model or GROK_MODEL:
        cmd.extend(["-m", (model or GROK_MODEL)])
    env = {**os.environ}
    if GROK_HOME:
        env["GROK_HOME"] = GROK_HOME

    from permissions import observe
    observe("llm_cli", detail=f"grok -p ({model or GROK_MODEL})")  # roadmap #6: observe-only
    from cost import record_oauth_usage
    start = time.monotonic()
    try:
        with _grok_call_lock:  # serialize: xAI rotates the OAuth refresh token per use
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GROK_TIMEOUT,
                env=env,
                cwd=scratch,
            )
        if result.returncode != 0:
            tail = ((result.stderr or "") + (result.stdout or ""))[-300:]
            raise RuntimeError(f"grok -p exited {result.returncode}: ...{tail}")
        text = _extract_grok_text(result.stdout or "")
        record_oauth_usage("grok", success=True, latency_s=time.monotonic() - start, tokens=0)
        # Grok CLI ($0 OAuth) does not expose per-call token counts; record 0 so the
        # per-task accumulator still sees an entry for this model.
        _set_call_tokens(0, 0)
        return text
    except BaseException:
        # Count attempted invocations too — a failed call (exe missing, timeout, nonzero exit,
        # auth/quota) is what trips the disable-latch, so it must be visible in cost telemetry.
        record_oauth_usage("grok", success=False, latency_s=time.monotonic() - start, tokens=0)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _extract_claude_text(stdout: str) -> tuple[str, int, int]:
    """Pull the final assistant message (and token counts) out of `claude -p --output-format json`.

    The print-mode envelope is a single JSON object: {"type":"result","subtype":"success",
    "is_error":false,"result":"<text>","usage":{"input_tokens":N,"output_tokens":M},...}. Returns
    (text, input_tokens, output_tokens). Falls back defensively to other text keys, then the raw
    stdout, since downstream _parse_and_validate hunts for the {"files":[...]} contract anyway.
    Raises RuntimeError when the envelope reports is_error (a model-level failure on a 0 exit)."""
    s = (stdout or "").strip()
    if not s:
        return "", 0, 0
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return s, 0, 0  # plain text — let _parse_and_validate find the contract
    if isinstance(obj, dict):
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        if obj.get("is_error"):
            raise RuntimeError(f"claude -p reported an error: {str(obj.get('result'))[:300]}")
        # If structured output is ever enabled (--json-schema), it already IS the contract shape —
        # prefer it over the free-text result so validation is part of the transport.
        so = obj.get("structured_output")
        if isinstance(so, (dict, list)):
            return json.dumps(so), inp, out
        for key in ("result", "text", "response", "message", "output", "final"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip(), inp, out
    return s, 0, 0


def _call_claude_cli(model: str, system: str, user: str) -> str:
    """Run a one-shot headless `claude -p` under the operator's Claude Max subscription ($0 marginal).

    Mirrors _call_ollama's (model, system, user) -> str contract. CRITICAL: `claude -p` is ALWAYS the
    full Claude Code agent (there is no bare-model path to the Max subscription), so it is constrained
    hard to behave as a pure generator:
      * --tools ""             disable ALL built-in agent tools (no Bash/Edit/Write/Read/Glob/…)
      * --strict-mcp-config    ignore operator/project MCP servers (none are passed in)
      * --setting-sources ""   load NO user/project/local settings (no hooks/plugins/customizations)
      * --disable-slash-commands
      * --no-session-persistence  don't write session state to disk
      * --system-prompt-file   REPLACE Claude Code's coding-agent identity with j-claw's worker
                               prompt (a temp file — a huge --system-prompt argv would blow the
                               Windows command-line length limit); the task JSON is piped on stdin.
    The subprocess env is SCRUBBED of API-key / Bedrock / Vertex credentials (_CLAUDE_CLI_ENV_BLOCKLIST)
    so Claude Code uses the subscription OAuth and not the metered API. Serialized behind
    _claude_cli_call_lock; runs in an isolated scratch cwd; UTF-8 forced (non-cp1252 glyphs, as Codex).
    CLAUDE_CLI_TIMEOUT bounds the call.
    NOTE: ships inert (CLAUDE_CLI_ENABLED=false). The constraint flags + the agent's ability to emit
    the clean {"files":[...]} contract MUST be confirmed by a live smoke test before enabling — see the
    live-validation checklist in config.py's CLAUDE_CLI block."""
    exe = shutil.which("claude") or shutil.which("claude.cmd") or "claude"
    scratch = tempfile.mkdtemp(prefix="claude_cwd_")
    sysfd, sysfile = tempfile.mkstemp(suffix=".txt", prefix="claude_sys_")
    os.close(sysfd)
    with open(sysfile, "w", encoding="utf-8") as fh:
        fh.write(system)
    cmd = [
        exe, "-p", "--output-format", "json", "--model", (model or CLAUDE_CLI_MODEL),
        "--system-prompt-file", sysfile,   # replace the coding-agent identity with the worker prompt
        "--tools", "",                     # no built-in tools — pure generation
        "--strict-mcp-config",             # ignore any operator/project MCP servers
        "--setting-sources", "",           # load NO user/project/local settings (hooks/plugins/customizations)
        "--disable-slash-commands",
        "--no-session-persistence",        # don't write session state to disk
    ]
    # Strip credentials that would override the subscription OAuth and silently meter the call.
    env = {k: v for k, v in os.environ.items() if k not in _CLAUDE_CLI_ENV_BLOCKLIST}
    if CLAUDE_CLI_HOME:
        env["CLAUDE_CONFIG_DIR"] = CLAUDE_CLI_HOME

    from permissions import observe
    observe("llm_cli", detail=f"claude -p ({model or CLAUDE_CLI_MODEL})")  # roadmap #6: observe-only
    from cost import record_oauth_usage
    start = time.monotonic()
    try:
        with _claude_cli_call_lock:  # serialize: protect the shared Max pool + local session state
            result = subprocess.run(
                cmd,
                input=user,            # the task JSON only; the worker prompt is the system-prompt-file
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CLAUDE_CLI_TIMEOUT,
                env=env,
                cwd=scratch,
            )
        if result.returncode != 0:
            tail = ((result.stderr or "") + (result.stdout or ""))[-300:]
            raise RuntimeError(f"claude -p exited {result.returncode}: ...{tail}")
        text, inp, out = _extract_claude_text(result.stdout or "")
        record_oauth_usage("claude_cli", success=True, latency_s=time.monotonic() - start,
                            tokens=inp + out)
        # Unlike Codex/Grok, claude -p DOES expose usage — record real counts when present.
        _set_call_tokens(inp, out)
        return text
    except BaseException:
        record_oauth_usage("claude_cli", success=False, latency_s=time.monotonic() - start, tokens=0)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            os.remove(sysfile)
        except OSError:
            pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_user_message(task, spec: dict, dependency_files: dict, context: dict | None = None) -> str:
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
    if context:
        payload["memory_context"] = context
    return json.dumps(payload, indent=2)


def _loads_tolerant(raw: str):
    """Parse worker JSON, tolerating trailing prose/data after the first object."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    if start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(raw[start:])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _extract_code_block(raw: str) -> str | None:
    """Return the largest fenced ``` code block in raw, or None."""
    blocks = re.findall(r"```[a-zA-Z0-9_+\-]*\n(.*?)```", raw, re.DOTALL)
    if not blocks:
        return None
    return (max(blocks, key=len).strip() or None)


def _salvage_single_file(raw: str, parsed, task) -> dict | None:
    """Conservatively recover a single-file task's content from malformed output.

    Only fires when the task declares exactly one output file (multi-file guessing is unsafe).
    Sources, in order: an explicit content/code/file string field, then the largest fenced
    code block. Returns a reconstructed {files:[...]} dict, or None to let the caller escalate.
    This cuts the paid-escalation tax on "write a script" tasks where the local model produces
    valid code but botches the surrounding JSON.
    """
    files = getattr(task, "files", None) if task is not None else None
    if not files or len(files) != 1:
        return None
    path = files[0]

    content: str | None = None
    if isinstance(parsed, dict):
        for key in ("content", "code", "file"):
            val = parsed.get(key)
            if isinstance(val, str) and val.strip():
                content = val
                break
    if content is None:
        content = _extract_code_block(raw)
    if content is None or len(content.strip()) < 20:
        return None

    console.print(
        f"  [yellow]⚠ Salvaged single-file output for '{path}' from malformed worker JSON "
        "(avoided an escalation)[/yellow]"
    )
    return {"files": [{"path": path, "content": content}]}


def _parse_and_validate(raw: str, task=None) -> dict:
    raw = _strip_fences(raw)
    parsed = _loads_tolerant(raw)

    if not isinstance(parsed, dict) or not isinstance(parsed.get("files"), list):
        # Before escalating, try to salvage a single-file task's body from malformed /
        # mis-schema'd output (the local model often nails the code but botches JSON escaping).
        salvaged = _salvage_single_file(raw, parsed, task)
        if salvaged is not None:
            parsed = salvaged
        elif not isinstance(parsed, dict):
            raise ValueError(f"Worker returned invalid JSON:\n--- raw (first 600 chars) ---\n{raw[:600]}")
        else:
            raise ValueError(f"Worker output missing 'files' list. Got keys: {list(parsed.keys())}")

    clean_files = []
    for entry in parsed["files"]:
        if not isinstance(entry.get("path"), str) or not isinstance(entry.get("content"), str):
            raise ValueError(f"Worker file entry missing 'path' or 'content': {entry}")
        content = _fix_literal_newlines(entry["path"], entry["content"])
        _warn_if_truncated(entry["path"], content)
        # Strict boundary (Phase 1): a file entry is EXACTLY {path, content}. Any other key
        # (e.g. a stray "lesson") is dropped here so learning metadata can NEVER be written to
        # disk as a file — the file-writer only ever sees path/content.
        clean_files.append({"path": entry["path"], "content": content})

    result = {"files": clean_files}
    # Optional TOP-LEVEL `lesson` (Phase 1 learning-loop distillation). Read for the experience
    # log only; it is a sibling of `files`, never part of it, and never reaches the file-writer.
    # Ignored unless it's a non-empty dict.
    lesson = parsed.get("lesson") if isinstance(parsed, dict) else None
    if isinstance(lesson, dict) and lesson:
        result["lesson"] = lesson
    return result


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
