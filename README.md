# J-Claw — Local-First Autonomous Coding Pipeline

J-Claw is a self-contained agentic software production system. You describe a project in plain English; the pipeline plans it, writes all the code, verifies the output, reviews the result with Claude, self-heals if issues are found, and hands off a signed report — with no human in the loop beyond the initial intent.

Runs entirely on your local machine. The worker model is a local Ollama LLM. The orchestrator is Claude (via Anthropic or OpenRouter API), with a manual fallback mode that requires no API key.

---

## What It Does

```
"Build a multiplayer drawing game with React frontend and FastAPI backend"
            │
            ▼  INIT
    Orchestrator generates a project spec (FORMAT 1)
            │  (auto-accepted with --yes, or you review and revise)
            ▼  SPEC_ACCEPTED
    Orchestrator emits a task DAG (FORMAT 2) — up to 50 tasks
            │
            ▼  Execute tasks in topological order (up to 2 parallel workers)
            │   └─ Worker (Ollama) writes each file
            │   └─ Harness runs verification (lint / unit_test / build / smoke)
            │   └─ On failure → EXECUTION_ERROR → Orchestrator rewrites task → retry
            │   └─ Experience tracker logs outcomes for future retries
            │
            ▼  PROJECT_REVIEW
    Orchestrator inspects all outputs — pass or add follow-up tasks
            │
            ▼  Final Code Review (Claude API)
    Claude reads every output file — checks for stubs, broken imports, missing files
            │
            ├─ VERDICT: PASS → write HANDOFF.md, done
            │
            └─ VERDICT: ISSUES FOUND
                │
                ▼  REVIEW_FAILED (self-healing loop, up to 2 cycles)
        Orchestrator generates targeted fix tasks
        Worker re-writes the broken files
        Claude re-reviews
                │
                ▼  Write HANDOFF.md + git commit
    Optional: invoke claude CLI for autonomous final stamp
            │
            ▼  Done — output in harness/projects/<name>/
```

---

## Supported Stacks (10)

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

All stacks also support:
- **PWA output** (vanilla + react-vite): `manifest.json` + `sw.js` service worker — every generated app is installable on mobile/desktop
- **JWT auth** (full-stack): when spec mentions login/register, generates `auth.py`, User model, `/auth/register` + `/auth/login` endpoints, React `LoginForm`, `RegisterForm`, `PrivateRoute`
- **Asset generation**: image assets routed to local Stable Diffusion WebUI (AUTOMATIC1111/Forge/ComfyUI at `localhost:7860`), SVG color-block placeholders if SD not running

---

## Architecture

```
j-claw/
├── orchestrator.txt              System prompt — the planning and review brain
├── run.bat                       Entry point (Windows)
├── bot.bat                       Telegram bot entry point
├── dashboard.py                  Mission Control dashboard server (port 8765)
├── openclaw-skill/
│   └── SKILL.md                  OpenClaw skill — invoke j-claw from Telegram/WhatsApp
├── dashboard/
│   └── index.html                Live pipeline dashboard (dark theme, auto-polling)
└── harness/
    ├── main.py                   CLI + top-level pipeline loop + self-healing loop
    ├── orchestrator.py           Orchestrator (Claude/OpenRouter) + ManualOrchestrator
    ├── scheduler.py              DAG scheduler — topological exec, error handling, review
    ├── worker.py                 Sends tasks to Ollama; 10 stack-specific prompt sets
    ├── verification.py           Ecosystem detection + verification runners + PWA check
    ├── asset_worker.py           Local SD WebUI asset generation + SVG fallback
    ├── audio_worker.py           Local Coqui TTS audio generation + silent fallback
    ├── experience_log.py         Local-only EXECUTION_ERROR outcome tracker
    ├── telegram_bot.py           Telegram bot — /run /status /cancel /projects
    ├── start_bot.py              Bot entry point
    ├── final_review.py           Claude API code review — stubs, broken imports, etc.
    ├── handoff.py                HANDOFF.md writer + deployment hook + claude CLI stamp
    ├── state_writer.py           Singleton event bus → mission_control.json
    ├── validator.py              JSON schema + DAG integrity checks
    ├── project.py                ProjectInstance and Task data classes
    ├── config.py                 .env loading — models, paths, limits, all config
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

[OpenClaw](https://openclaw.ai) is a local AI assistant with native Telegram/WhatsApp/Discord access. J-claw ships a ready-made OpenClaw skill.

**To activate:**
1. Install OpenClaw from openclaw.ai
2. Copy the skill: `cp openclaw-skill C:\Users\<you>\.openclaw\workspace\skills\j-claw -Recurse`
3. Done — say "build me a React dashboard" in your Telegram chat and OpenClaw invokes j-claw

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

## What's Left To Build

### P1 — Completed this session

| Item | Status |
|---|---|
| **Database migrations** — Alembic for FastAPI (`alembic upgrade head` at startup) | ✅ Done |
| **Audio generation** — Coqui TTS + silent placeholder fallback | ✅ Done |
| **Experience tracker** — local JSONL fix-outcome log fed back into retries | ✅ Done |

### P2 — Planned

| Item | What it does |
|---|---|
| **Tauri stack** | Rust + WebView desktop apps — lighter than Electron |
| **E2E test generation** | Playwright test files auto-generated alongside every project |
| **WebSocket/SSE stack** | Real-time dashboards and data streams (separate from socket-io games) |
| **Inter-service testing** | Spin up FastAPI + React together, smoke test against real stack |

### P3 — Long term

| Item |
|---|
| Movie pipeline (script → storyboard → voice → video assembly) |
| Godot 3D game generation via headless Godot CLI |
| IPFS / on-chain deployment for Web3 projects |
| Payment integration (Stripe/LemonSqueezy scaffolding) |
| Real native mobile compilation (Swift/Kotlin — Expo covers JS-only today) |

---

## Known Limitations

- **Projects directory is gitignored** — generated output is local only.
- **Final code review requires `ANTHROPIC_API_KEY`** — without it, `REVIEW.md` and `HANDOFF.md` won't contain a real verdict.
- **claude CLI stamp is optional** — OpenClaw verdict in the dashboard only appears if `claude` is installed and on PATH.
- **SD/Coqui/Ollama must be running** — the pipeline degrades gracefully (SVG/silent/OpenRouter fallbacks) but local services need to be up for full capability.

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
