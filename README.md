# J-Claw — Autonomous Development Agency

J-Claw is a fully autonomous local-first software factory. Describe what you want in plain English — a game, app, website, or film — and the pipeline interprets the creative intent, plans the full build, writes all the code and media, verifies every output, self-heals any issues, and delivers a production-ready artifact with no human in the loop.

Three layers of intelligence:
- **Creative Director** (Claude Opus) — interprets intent, determines output type, produces a creative brief
- **Orchestrator** (Claude Sonnet) — translates the brief into a task DAG, drives the pipeline, self-heals
- **Worker** (local Ollama model) — writes all code and runs all generation tasks on your hardware

---

## What It Does

```
"Build a game like Celeste" / "Make a 30-second explainer film about AI"
            │
            ▼  CREATIVE DIRECTOR (Claude Opus)
    Interprets intent → determines output type (game / film / app / website)
    Produces CREATIVE_BRIEF: visual style, scenes/flows, asset requirements
            │
            ▼  INIT (Claude Sonnet)
    Reads CREATIVE_BRIEF → generates project spec (FORMAT 1)
            │  (auto-accepted with --yes, or you review and revise)
            ▼  SPEC_ACCEPTED
    Generates task DAG (FORMAT 2) — up to 75 tasks
            │
            ▼  Execute tasks in topological order (up to 4 parallel workers)
            │   ├─ Code tasks  → Worker (Ollama) writes files
            │   ├─ Asset tasks → Stable Diffusion WebUI (SD-enriched prompts from brief)
            │   ├─ Audio tasks → Coqui TTS (tone/speaker from brief)
            │   ├─ Video tasks → video_worker (ffmpeg pipeline)  [Phase 2]
            │   └─ On failure  → EXECUTION_ERROR (Haiku) → retry
            │
            ▼  Final Review (Claude Sonnet)
    Reads all outputs — code stubs, broken imports, media quality
            │
            ├─ VERDICT: PASS → write HANDOFF.md, done
            │
            └─ VERDICT: ISSUES FOUND
                │
                ▼  REVIEW_FAILED (self-healing loop, up to 2 cycles)
        Orchestrator generates targeted fix tasks → Worker re-writes → re-review
                │
                ▼  Write HANDOFF.md + git commit
            │
            ▼  Done — output in harness/projects/<name>/
```

---

## Supported Stacks

### Current (10)

| Stack | Use case | Verification |
|---|---|---|
| `vanilla` | Static HTML/JS/CSS + Tailwind CDN apps | Headless HTML structure check |
| `react-vite` | React 18 + Vite + Tailwind SPAs | `npm run build` |
| `fastapi` | Python REST API + SQLite + Alembic migrations | `pip install` + `alembic upgrade head` |
| `phaser` | Phaser 3 browser games (CDN) | Playwright canvas check |
| `full-stack` | React frontend + FastAPI backend in one pipeline run | Both above |
| `web3` | Solidity + Hardhat + ethers.js DApps | `npx hardhat compile && test` |
| `react-native` | Expo managed mobile apps (iOS/Android) | `npm install` |
| `socket-io` | Node.js + Socket.io real-time multiplayer | `npm install` |
| `three-js` | Three.js 3D browser scenes (CDN, WebGL) | Playwright canvas check |
| `electron` | Electron desktop apps (contextIsolation + contextBridge) | `npm install` |

### Coming (Phase 2–4)

| Stack | Use case |
|---|---|
| `film` | Narrative film / animated explainer — ffmpeg + SD frames + Coqui narration |
| `video-editor` | Browser-based clip editor — ffmpeg WASM + Canvas API |
| `tauri` | Rust + WebView desktop apps — lighter than Electron |
| `godot` | GDScript games — Godot headless export |
| `websocket-sse` | Real-time dashboards and data streams |

All stacks also support:
- **PWA output** (vanilla + react-vite): `manifest.json` + `sw.js` — every generated app is installable on mobile/desktop
- **JWT auth** (full-stack): `auth.py`, User model, `/auth/register` + `/auth/login`, React `LoginForm`, `RegisterForm`, `PrivateRoute`
- **Asset generation**: Stable Diffusion WebUI with Creative Director-enriched prompts (SVG fallback if SD not running)
- **Audio generation**: Coqui TTS with tone/speaker from Creative Brief (silent WAV fallback)
- **E2E tests**: Playwright test files auto-generated alongside every project *(Phase 4)*

---

## Architecture

```
j-claw/
├── orchestrator.txt              Orchestrator system prompt (FORMATs 1–5)
├── creative_director.txt         Creative Director system prompt (Claude Opus)  ← Phase 1
├── run.bat                       Entry point (Windows)
├── bot.bat                       Telegram bot entry point
├── dashboard.py                  Mission Control dashboard server (port 8765)
├── openclaw-skill/
│   └── SKILL.md                  OpenClaw skill — invoke j-claw from Telegram/WhatsApp
├── dashboard/
│   └── index.html                Live pipeline dashboard (dark theme, auto-polling)
└── harness/
    ├── main.py                   CLI + pipeline loop + creative director pre-pass
    ├── creative_director.py      Creative Director — intent → CREATIVE_BRIEF  ← Phase 1
    ├── orchestrator.py           Orchestrator (Claude/OpenRouter) + prompt caching
    ├── scheduler.py              DAG scheduler — topological exec, media task routing
    ├── worker.py                 Sends tasks to Ollama; stack-specific prompt sets
    ├── video_worker.py           ffmpeg-based video/film pipeline  ← Phase 2
    ├── music_worker.py           Music generation (placeholder → MusicGen)  ← Phase 3
    ├── verification.py           Ecosystem detection + ffprobe/frame checks
    ├── asset_worker.py           SD WebUI asset generation + SVG fallback
    ├── audio_worker.py           Coqui TTS audio generation + silent fallback
    ├── experience_log.py         EXECUTION_ERROR outcome tracker (JSONL)
    ├── telegram_bot.py           Telegram bot — /run /status /cancel /projects
    ├── start_bot.py              Bot entry point
    ├── final_review.py           Claude API code review — stubs, imports, media quality
    ├── handoff.py                HANDOFF.md writer + deployment hook
    ├── state_writer.py           Singleton event bus → mission_control.json
    ├── validator.py              JSON schema + DAG integrity + media task types
    ├── project.py                ProjectInstance, Task, binary_outputs
    ├── config.py                 .env loading — all models, paths, limits
    ├── .env.example              Template — copy to .env and fill in keys
    └── projects/                 Generated project output (gitignored)
```

---

## Pipeline State Machine

| State | Format | Description |
|---|---|---|
| `INIT` | FORMAT 1 | Orchestrator generates project spec |
| `SPEC_REVISION` | FORMAT 1 | Re-emit spec with `revision_feedback` applied |
| `SPEC_ACCEPTED` | FORMAT 2 | Full task DAG — up to 50 tasks with deps, files, criteria |
| `EXECUTION_ERROR` | FORMAT 3 | Fix for a failed task: `modify`, `split`, or `deprecate` |
| `PROJECT_REVIEW` | FORMAT 4 | Final orchestrator verdict: pass or add follow-up tasks |
| `REVIEW_FAILED` | FORMAT 4 | Self-healing: receives Claude review issues, returns fix tasks |
| `CONTINUE` | FORMAT 2 | Incremental tasks to add a feature to an existing project |

**FORMAT 5 (oversize)**: if a project exceeds the 50-task budget, orchestrator emits a sub-project graph and the harness runs each as its own pipeline instance in dependency order.

---

## Setup

### Requirements

- Windows 10/11
- Python 3.10+
- [Ollama](https://ollama.com/download/windows) with a code model pulled
- Anthropic API key *(for automated orchestrator mode and final code review)*

### Install

```powershell
git clone https://github.com/Matt28296/j-claw.git
cd j-claw\harness

Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser   # once

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Pull a worker model

```powershell
ollama pull qwen2.5-coder:14b   # recommended — 8–16 GB VRAM at Q4
ollama pull qwen2.5-coder:7b    # lighter — 8 GB VRAM
```

### Configure

```powershell
copy harness\.env.example harness\.env
# then edit harness\.env with your keys
```

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for auto orchestrator + final review |
| `OPENROUTER_API_KEY` | — | Alternative — set `ORCHESTRATOR_PROVIDER=openrouter` |
| `WORKER_MODEL` | `qwen2.5-coder:7b` | Ollama model for code writing |
| `ORCHESTRATOR_MODEL` | `claude-sonnet-4-6` | Claude model for planning and review |
| `WORKER_FALLBACKS` | openrouter free models | Fallback chain if Ollama is down |
| `MAX_PARALLEL_WORKERS` | `2` | Concurrent Ollama workers (independent DAG branches) |
| `ORCHESTRATOR_MAX_TOKENS` | `16384` | Raise to `32768` for very large full-stack DAGs |
| `SD_API_URL` | `http://localhost:7860` | Stable Diffusion WebUI endpoint for asset tasks |
| `ASSET_PROVIDER` | `sd` | `sd` or `none` |
| `COQUI_API_URL` | `http://localhost:5002` | Coqui TTS endpoint for audio tasks |
| `DEPLOY_HOOK` | — | CLI command run after git commit (e.g. `vercel --prod --yes`) |
| `JWT_SECRET` | random default | Secret key for generated apps that include auth |
| `TELEGRAM_BOT_TOKEN` | — | BotFather token — enables `bot.bat` Telegram control |
| `TELEGRAM_CHAT_ID` | — | Restrict bot to your chat ID (get from @userinfobot) |
| `EXPERIENCE_LOG` | `experience.jsonl` | Path for local EXECUTION_ERROR outcome log |
| `PROJECTS_DIR` | `./projects` | Output directory for generated projects |

---

## Usage

From the repo root:

```powershell
# Fully zero-touch — auto-accept spec
.\run.bat --yes "A snake game in the browser"

# Interactive — review and approve the spec before execution
.\run.bat "A single-page todo app"

# Manual orchestrator — no API key required
.\run.bat --manual

# Add a feature to an existing generated project
.\run.bat --continue "harness\projects\<project-folder>" "Add user authentication"

# Mission Control dashboard (open in a second terminal)
python dashboard.py   # then open http://localhost:8765/dashboard/index.html

# Telegram bot — control the pipeline from your phone
.\bot.bat             # requires TELEGRAM_BOT_TOKEN in .env
```

---

## Telegram Bot Commands

Start with `.\bot.bat` after setting `TELEGRAM_BOT_TOKEN` in `.env`:

| Command | What it does |
|---|---|
| `/run <spec>` | Triggers a pipeline run — streams output to your Telegram chat |
| `/status` | Shows current pipeline state, tasks done/total, elapsed time |
| `/cancel` | Kills the running pipeline |
| `/projects` | Lists the 5 most recently generated projects |

---

## OpenClaw Integration

[OpenClaw](https://openclaw.ai) is a local AI assistant with native Telegram/WhatsApp/Discord access. J-claw ships a ready-made OpenClaw skill so you can trigger builds by sending a message to your Telegram bot.

### Setup status

| Step | Status |
|---|---|
| Install OpenClaw | ✅ Done (2026.5.28) |
| Fix Discord/Telegram streaming config | ✅ Done |
| Copy j-claw skill to `~/.openclaw/workspace/skills/j-claw/` | ✅ Done |
| Add `ANTHROPIC_API_KEY` to `~/.openclaw/.env` | ✅ Done |
| Create Telegram bot (@JarvisClaw96bot) + add token | ✅ Done |
| Telegram account paired | ✅ Done |
| Switch OpenClaw agent to Anthropic Haiku (no VRAM conflict) | ✅ Done |
| Start OpenClaw gateway | ⬜ Run `openclaw gateway` to activate |

### To activate

Run in a separate PowerShell window (leave it running):

```powershell
openclaw gateway
```

Startup should show: `agent model: anthropic/claude-haiku-4-5-20251001`

Then send your bot `build me a snake game` in Telegram.

### Architecture note

OpenClaw's embedded agent (Claude Haiku) acts as a thin router — it reads the j-claw SKILL.md and invokes `run.bat`. The actual build runs via the Creative Director + Orchestrator + Worker pipeline locally. Haiku is used for the routing layer only; it requires no VRAM and doesn't conflict with the Ollama worker.

> **Security note**: Before installing any third-party OpenClaw plugins, audit their source code. OpenClaw plugins run in-process with full OS privileges — no sandbox. The `@alan512/ExperienceEngine` plugin was reviewed and rejected (exfiltrates task data to external LLMs). The `@openclaw/memory-lancedb` plugin is safe only when configured with local Ollama embeddings.

---

## Asset Generation

Image assets (sprites, icons, backgrounds) are generated locally via Stable Diffusion WebUI:

1. Start AUTOMATIC1111/Forge/ComfyUI with `--api` flag
2. Run j-claw normally — asset tasks are routed to SD automatically
3. If SD is not running, SVG color-block placeholders are written instead (pipeline continues unblocked)

Configure the SD endpoint: `SD_API_URL=http://localhost:7860` in `.env`.

---

## Audio Generation

Sound effects and TTS for game/app projects via Coqui TTS:

1. Start a Coqui TTS server at `localhost:5002`
2. Audio tasks are routed automatically
3. Silent `.wav` placeholders are written if Coqui is not running

Configure: `COQUI_API_URL=http://localhost:5002` in `.env`.

---

## Deployment Hooks

Run a deployment command automatically after every successful project build:

```
# harness/.env
DEPLOY_HOOK=vercel --prod --yes          # Vercel
DEPLOY_HOOK=netlify deploy --prod        # Netlify
DEPLOY_HOOK=railway up                   # Railway
```

The hook runs in the project output directory after git commit. The deployment URL is extracted from the command output and written to `HANDOFF.md`.

---

## Experience Tracker

J-claw keeps a local JSONL log (`experience.jsonl`) of every `EXECUTION_ERROR` refinement outcome. When retrying a failed task, the top matching successful fix patterns are prepended to the orchestrator's context — so the pipeline gets better at fixing recurring errors over time.

- Fully local — no external APIs, no network calls
- Simple word-overlap matching (no embeddings)
- Configure path: `EXPERIENCE_LOG=experience.jsonl` in `.env`

---

## Mission Control Dashboard

```powershell
python dashboard.py
# open http://localhost:8765/dashboard/index.html
```

Live panels:
- **Active Agent** — which API is being called, live elapsed timer
- **Tasks** — color-coded cards (pending / running / done / failed), retry badges, file pills
- **Events** — live feed of every pipeline event
- **Test Results** — per-task verification with ecosystem badges; Playwright/pytest output colorized
- **Work Log** — chronological ORCH vs WORKER record with model names
- **Review Banner** — green PASS / red ISSUES FOUND when pipeline completes
- **OpenClaw Banner** — purple APPROVED stamp when claude CLI is on PATH

**Copy Logs button** — structured plain-text snapshot of all pipeline state to clipboard.

---

## Pipeline Output

Every project writes to `harness/projects/<slug>/`:

| File | Description |
|---|---|
| Source files | All generated code (frontend + backend as needed) |
| `manifest.json` + `sw.js` | PWA files (vanilla and react-vite stacks) |
| `REVIEW.md` | Claude code review — `VERDICT: PASS` or `VERDICT: ISSUES FOUND` |
| `HANDOFF.md` | Pipeline report — status, heal cycles, test results, deployment URL |

---

## Roadmap

### Done

| Item | Status |
|---|---|
| Core pipeline: spec → DAG → code → verify → review → self-heal | ✅ |
| 10 stacks: vanilla, react-vite, fastapi, phaser, full-stack, web3, react-native, socket-io, three-js, electron | ✅ |
| PWA output, JWT auth, Alembic migrations | ✅ |
| SD WebUI asset generation + Coqui TTS audio | ✅ |
| Experience tracker (JSONL fix-outcome log) | ✅ |
| Orchestrator JSON truncation fix + FORMAT 5 bug fix | ✅ |
| OpenClaw skill deployed + Telegram bot paired | ✅ |

### Phase 1 — Creative Director + API optimization

| Item | What it does |
|---|---|
| `creative_director.py` + `creative_director.txt` | Claude Opus pre-pass: interprets intent → CREATIVE_BRIEF JSON |
| `orchestrator.py` prompt caching | Cache 550-line system prompt across calls — ~80% cost reduction |
| Haiku for EXECUTION_ERROR calls | Downgrade simple fix-routing from Sonnet → Haiku |
| Merge PROJECT_REVIEW into final_review | Eliminate one redundant API call per project |
| `config.py` new vars | `CREATIVE_DIRECTOR_MODEL`, `EXECUTION_ERROR_MODEL`, `MAX_TASKS=75`, `MAX_PARALLEL_WORKERS=4` |

### Phase 2 — Video/Movie Pipeline

| Item | What it does |
|---|---|
| `video_worker.py` | ffmpeg-based video generation — LLM writes the ffmpeg command, harness executes it |
| `verification.py` video checks | `ffprobe`, `frame_integrity`, `sync_check` |
| `project.py` binary outputs | `binary_outputs` field on Task for `.mp4`/`.wav`/`.mov` files |
| `validator.py` new task types | `video`, `editing`, `composition`, `vfx` |
| `orchestrator.txt` film stacks | `film`, `video-editor` stacks + video task types |

### Phase 3 — Enhanced Media

| Item | What it does |
|---|---|
| `asset_worker.py` | Use Creative Brief visual identity to enrich SD prompts |
| `audio_worker.py` | Speaker/tone/duration control from Creative Brief |
| `music_worker.py` | Music generation placeholder (→ MusicGen/Audiocraft) |

### Phase 4 — New Code Stacks

| Item | What it does |
|---|---|
| Tauri | Rust + WebView desktop apps |
| Godot | GDScript + headless export |
| WebSocket/SSE | Real-time dashboards |
| E2E test generation | Playwright tests auto-generated alongside every project |

### Phase 5 — Long term

| Item |
|---|
| IPFS / on-chain deployment for Web3 projects |
| Payment integration (Stripe/LemonSqueezy) |
| Real native mobile (Swift/Kotlin) |

---

## Known Limitations

- **Projects directory is gitignored** — generated output is local only.
- **Final code review requires `ANTHROPIC_API_KEY`** — without it, `REVIEW.md` and `HANDOFF.md` won't contain a real verdict.
- **claude CLI stamp is optional** — OpenClaw verdict in the dashboard only appears if `claude` is installed and on PATH.
- **SD/Coqui/Ollama must be running** — the pipeline degrades gracefully (SVG/silent/OpenRouter fallbacks) but local services need to be up for full capability.
- **Full-stack projects split into sub-projects** — when the spec is "React + FastAPI", the orchestrator emits FORMAT 5 and the harness runs a `backend_api/` sub-project then a `frontend_react/` sub-project in sequence. Both land under `harness/projects/<slug>/`.

---

## Architecture Notes

**Orchestrator** (`orchestrator.py`): Three implementations behind the same interface:
- `Orchestrator` — Anthropic API (default)
- `OpenRouterOrchestrator` — any OpenRouter model with cascading fallback on rate limit
- `ManualOrchestrator` — writes JSON files, waits for human to fill in response (no API key needed)

**Worker** (`worker.py`): Sends tasks to local Ollama with stack-specific prompt instructions. 10 stack prompts covering web, API, game, mobile, desktop, Web3, and asset generation. Detects truncated output, fixes literal `\n` sequences.

**Scheduler** (`scheduler.py`): Topological DAG execution with parallel workers. Routes asset tasks to `asset_worker.py`, audio tasks to `audio_worker.py`, code tasks to `worker.py`. On failure: calls orchestrator in `EXECUTION_ERROR`, reads experience hints, retries up to `MAX_RETRIES_PER_TASK`.

**Verification** (`verification.py`): Auto-detects ecosystem (Node, Python, FastAPI, React+Vite, Phaser, vanilla, web3, electron, socket-io, three-js). Runs appropriate checks. Validates PWA files (`manifest.json` + `sw.js`) for vanilla/react-vite projects.

**Self-healing loop** (`main.py`): When final review returns `ISSUES FOUND`, parses the `ISSUES:` list, calls orchestrator in `REVIEW_FAILED` state, re-runs scheduler. Up to 2 cycles.

---

## License

MIT
