from __future__ import annotations
import json
import re
import ollama
from rich.console import Console

import threading

from config import (
    WORKER_MODEL, OLLAMA_HOST, WORKER_PROVIDER,
    WORKER_FALLBACKS, ANTHROPIC_API_KEY, OPENROUTER_API_KEY,
    WORKER_LADDER, LOCAL_FIRST_TASK_TYPES, MAX_PAID_WORKER_CALLS,
    WORKER_TASK_TIMEOUT,
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
  command per line. Use only ffmpeg built-in sources/filters that need no external assets:
  lavfi sources (color=, testsrc=, sine=), drawtext, concat filter, xfade transitions,
  and overlay. Do NOT reference image/audio files that are not also produced by an
  upstream task — a self-contained synthetic render must always succeed.
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


def reset_paid_budget() -> None:
    """Reset the per-project paid (cloud) worker-call counter. Call at project start."""
    global _paid_calls_made
    with _paid_lock:
        _paid_calls_made = 0


def _reserve_paid_call() -> bool:
    """Atomically reserve one paid worker call. Returns False if the budget is exhausted."""
    global _paid_calls_made
    with _paid_lock:
        if _paid_calls_made >= MAX_PAID_WORKER_CALLS:
            return False
        _paid_calls_made += 1
        return True


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
    for provider, model in attempts:
        # Gate paid (non-local) calls on the per-project budget. When exhausted, skip the
        # cloud rung and fall through to the local last-ditch instead of spending more.
        if provider != "ollama" and not _reserve_paid_call():
            console.print(
                f"  [yellow]Paid-call budget ({MAX_PAID_WORKER_CALLS}) exhausted — "
                f"skipping {provider}/{model}, staying local.[/yellow]"
            )
            continue
        try:
            raw = _call_provider(provider, model, system_prompt, user_message)
            parsed = _parse_and_validate(raw, task)
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
    return response.message.content.strip()


def _call_anthropic(model: str, system: str, user: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot use anthropic worker provider")
    import anthropic
    from cache_telemetry import log_cache_usage
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    log_cache_usage(response.usage, "worker-esc")
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
