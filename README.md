# J-Claw — Autonomous Development Agency (v2)

J-Claw is a fully autonomous local-first AI software factory. Describe what you want in plain English — a game, app, website, or film — and the pipeline interprets the creative intent, designs the architecture, plans the full build, writes all the code and media, verifies every output, self-heals any issues, and delivers a production-ready artifact with no human in the loop.

Four layers of intelligence:
- **Creative Director** (Claude Opus) — interprets intent, determines output type, produces a creative brief *(WHAT)*
- **Technical Architect** (Claude Sonnet) — chooses stack, file structure, ADRs, seeds persistent project memory *(HOW)*
- **Orchestrator** (Claude Sonnet) — translates spec into a task DAG, drives the pipeline, self-heals
- **Worker** (local Ollama model) — writes all code and runs all generation tasks on your hardware

---

## What It Does

```
"Build a game like Celeste" / "Make a 30-second explainer film about AI"
            │
            ▼  CREATIVE DIRECTOR (Claude Opus)
    Interprets intent → output_type, features, constraints, desired_experience
    NO stack choice — that belongs to the architect
            │
            ▼  TECHNICAL ARCHITECT (Claude Sonnet)
    Reads CREATIVE_BRIEF → chooses confirmed_stack, file_structure
    Creates ADRs (Architecture Decision Records) documenting every major call
    Seeds project_memory/ with architecture.md, coding_standards.md,
    api_contracts.md, known_issues.md, decision_log.jsonl, ADR files
            │
            ▼  INIT (Claude Sonnet)
    Reads CREATIVE_BRIEF + TECH_SPEC → generates project spec (FORMAT 1)
            │  (auto-accepted with --yes, or you review and revise)
            ▼  SPEC_ACCEPTED
    Generates task DAG (FORMAT 2) — up to 75 tasks
            │
            ▼  Execute tasks in topological order (up to 4 parallel workers)
            │   ├─ Per task: CONTEXT BUILDER selects relevant ~4K tokens from memory
            │   ├─ Code tasks      → Worker (Ollama) writes files + optional memory_patch.json
            │   ├─ DevOps tasks    → Worker writes Dockerfile, docker-compose, nginx, CI/CD
            │   ├─ Docs tasks      → Worker writes README, JSDoc, docstrings, CHANGELOG
            │   ├─ Asset tasks     → Stable Diffusion WebUI (SD-enriched prompts from brief)
            │   ├─ Audio tasks     → Coqui TTS (tone/speaker from brief)
            │   ├─ Video tasks     → video_worker (ffmpeg pipeline)
            │   └─ On failure      → EXECUTION_ERROR (Haiku) → retry
            │
            ▼  Memory patch (if worker produced memory_patch.json)
    MEMORY VALIDATOR checks version + operation rules → PASS/WARN/REJECT
    PASS/WARN → ProjectMemory.apply_patch() → increment version
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

## Supported Stacks (15)

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
| `film` | Narrative film / animated explainer — ffmpeg + SD frames + Coqui narration | ffprobe |
| `video-editor` | Browser-based clip editor — ffmpeg WASM + Canvas API | build |
| `tauri` | Rust + WebView desktop apps — lighter than Electron | build |
| `godot` | GDScript games — Godot headless export | none |
| `websocket-sse` | Real-time dashboards and data streams | `npm install` |

All stacks also support:
- **PWA output** (vanilla + react-vite): `manifest.json` + `sw.js` — every generated app is installable on mobile/desktop
- **JWT auth** (full-stack): `auth.py`, User model, `/auth/register` + `/auth/login`, React `LoginForm`, `RegisterForm`, `PrivateRoute`
- **DevOps tasks**: Dockerfile (multi-stage, non-root), `docker-compose.yml`, `nginx.conf`, `.github/workflows/ci.yml`, `.env.example`
- **Documentation tasks**: `README.md`, JSDoc comments, Google-style Python docstrings, `CHANGELOG.md`
- **Asset generation**: Stable Diffusion WebUI with Creative Director-enriched prompts (SVG fallback if SD not running)
- **Audio generation**: Coqui TTS with tone/speaker from Creative Brief (silent WAV fallback)
- **Security scanning**: `bandit` (Python) / `npm audit` (Node) — `verification: "security"` task type
- **Lighthouse**: performance + accessibility checks for web projects — `verification: "lighthouse"` task type

---

## Architecture

```
j-claw/
├── orchestrator.txt              Orchestrator system prompt (FORMATs 1–5)
├── creative_director.txt         Creative Director system prompt (Claude Opus)
├── technical_architect.txt       Technical Architect system prompt (Claude Sonnet)
├── run.bat                       Entry point (Windows)
├── bot.bat                       Telegram bot entry point
├── dashboard.py                  Mission Control dashboard server (port 8765, auto-starts)
├── openclaw-skill/
│   └── SKILL.md                  OpenClaw skill — invoke j-claw from Telegram/WhatsApp
├── dashboard/
│   └── index.html                Live pipeline dashboard (dark theme, auto-polling)
└── harness/
    ├── main.py                   CLI + pipeline loop (Creative Director → Architect → INIT)
    ├── creative_director.py      Creative Director — intent → CREATIVE_BRIEF (WHAT)
    ├── technical_architect.py    Technical Architect — brief → TECH_SPEC + project_memory/ (HOW)
    ├── context_builder.py        Deterministic context selection (~4K tokens per task, no LLM)
    ├── project_memory.py         ProjectMemory + RuntimeMemory — persistent + ephemeral state
    ├── memory_validator.py       Patch validator — operation rules, version check, PASS/WARN/REJECT
    ├── orchestrator.py           Orchestrator (Claude/OpenRouter) + prompt caching
    ├── scheduler.py              DAG scheduler — context building, memory patch apply, task routing
    ├── worker.py                 Sends tasks to Ollama; 17 stack-specific prompt sets
    ├── video_worker.py           ffmpeg-based video/film pipeline
    ├── music_worker.py           Music generation (placeholder → MusicGen)
    ├── verification.py           Ecosystem detection + ffprobe/frame/security/lighthouse checks
    ├── asset_worker.py           SD WebUI asset generation + SVG fallback
    ├── audio_worker.py           Coqui TTS audio generation + silent fallback
    ├── experience_log.py         EXECUTION_ERROR outcome tracker (JSONL)
    ├── telegram_bot.py           Telegram bot — /run /status /cancel /projects
    ├── start_bot.py              Bot entry point
    ├── final_review.py           Claude API code review — stubs, imports, media quality
    ├── handoff.py                HANDOFF.md writer + deployment hook
    ├── state_writer.py           Singleton event bus → mission_control.json
    ├── validator.py              JSON schema + DAG integrity + task/verification type enums
    ├── project.py                ProjectInstance, Task, binary_outputs
    ├── config.py                 .env loading — all models, paths, limits
    ├── .env.example              Template — copy to .env and fill in keys
    └── projects/                 Generated project output (gitignored)
        └── <project-slug>/
            ├── creative_brief.json    CREATIVE_BRIEF from Creative Director
            ├── tech_spec.json         TECH_SPEC from Technical Architect
            ├── project_memory/        Long-lived architecture docs
            │   ├── _meta.json             {version, last_modified, last_patch_by}
            │   ├── architecture.md        Architecture notes from TECH_SPEC
            │   ├── coding_standards.md    Coding standards for this project
            │   ├── api_contracts.md       API endpoint registry (patched by workers)
            │   ├── decision_log.jsonl     Operational decisions (append-only)
            │   ├── known_issues.md        Known risks and workarounds
            │   ├── project_summary.md     Project description + goals
            │   └── architecture_decisions/
            │       ├── ADR-001-*.md       Stack choice ADR (always created)
            │       └── ADR-NNN-*.md       Additional architectural decisions
            └── runtime_memory/        Ephemeral execution state (cleared on completion)
                ├── current_state.json     {phase, completed_tasks, failed_tasks}
                ├── task_registry.json     Task status map
                └── active_workers.json    Currently running workers
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
| `ANTHROPIC_API_KEY` | — | Required for Creative Director, Technical Architect, Orchestrator, Final Review |
| `OPENROUTER_API_KEY` | — | Alternative orchestrator — set `ORCHESTRATOR_PROVIDER=openrouter` |
| `WORKER_MODEL` | `qwen2.5-coder:7b` | Ollama model for code writing (never Claude) |
| `ORCHESTRATOR_MODEL` | `claude-sonnet-4-6` | Claude model for architect, planning, and review |
| `TECHNICAL_ARCHITECT_ENABLED` | `true` | Set `false` to skip architect pass (legacy mode) |
| `DASHBOARD_AUTOOPEN` | `true` | Auto-open browser to dashboard when pipeline starts |
| `DASHBOARD_PORT` | `8765` | Dashboard server port |
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
| 15 stacks (including film, tauri, godot, websocket-sse) | ✅ |
| PWA output, JWT auth, Alembic migrations | ✅ |
| SD WebUI asset generation + Coqui TTS audio | ✅ |
| Experience tracker (JSONL fix-outcome log) | ✅ |
| Orchestrator JSON truncation fix + FORMAT 5 bug fix | ✅ |
| OpenClaw skill deployed + Telegram bot paired | ✅ |
| Creative Director (Claude Opus) — WHAT layer | ✅ |
| Technical Architect (Claude Sonnet) — HOW layer + ADRs | ✅ |
| Persistent project memory (project_memory/ + runtime_memory/) | ✅ |
| Context Builder — deterministic ~4K token selection per task | ✅ |
| Memory Patch System — operation-based, optimistic concurrency | ✅ |
| Memory Validator — PASS/WARN/REJECT rules, <10ms, no LLM | ✅ |
| Architecture Decision Records (ADR-001-*.md) | ✅ |
| DevOps specialist agent (Dockerfile, docker-compose, nginx, CI/CD) | ✅ |
| Documentation specialist agent (README, JSDoc, docstrings, CHANGELOG) | ✅ |
| Security verification (bandit / npm audit) | ✅ (wired, `verification: "security"`) |
| Lighthouse verification (performance + accessibility) | ✅ (wired, `verification: "lighthouse"`) |
| Dashboard auto-start + browser open on pipeline start | ✅ |

### Next

| Item |
|---|
| `verification.py` — implement security + lighthouse check logic |
| E2E test generation — Playwright tests auto-generated alongside every project |
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

**Technical Architect** (`technical_architect.py`): Runs once per project between the Creative Director and the Orchestrator INIT. Owns all technical decisions — stack, file structure, dependencies, coding standards. Writes ADR-001 (stack choice) and any additional ADRs. Seeds `project_memory/` with architecture docs that every downstream worker reads.

**Context Builder** (`context_builder.py`): Deterministic Python service — no LLM. Runs before every worker task. Reads `project_memory/` and selects the most relevant ~4K tokens: always coding standards + current state; conditionally API contracts (code tasks), architecture head (devops tasks), project summary (docs tasks), recent decisions, matching known issues, and ADR index. Output is a structured JSON dict injected into the worker prompt.

**Memory Patch System**: Workers can write a `memory_patch.json` alongside their code files. The `MemoryValidator` checks the patch against the current version (optimistic concurrency) and operation rules (duplicate check, schema validation, ID format). PASS/WARN → `ProjectMemory.apply_patch()` increments the version atomically. REJECT → logged and skipped.

**Worker** (`worker.py`): Sends tasks to local Ollama with stack-specific prompt instructions. 17 stack prompts covering web, API, game, mobile, desktop, Web3, DevOps, documentation, and asset generation. Receives structured context from Context Builder. Detects truncated output, fixes literal `\n` sequences.

**Scheduler** (`scheduler.py`): Topological DAG execution with parallel workers. Routes asset tasks to `asset_worker.py`, audio tasks to `audio_worker.py`, code tasks to `worker.py`. On failure: calls orchestrator in `EXECUTION_ERROR`, reads experience hints, retries up to `MAX_RETRIES_PER_TASK`.

**Verification** (`verification.py`): Auto-detects ecosystem (Node, Python, FastAPI, React+Vite, Phaser, vanilla, web3, electron, socket-io, three-js). Runs appropriate checks. Validates PWA files (`manifest.json` + `sw.js`) for vanilla/react-vite projects.

**Self-healing loop** (`main.py`): When final review returns `ISSUES FOUND`, parses the `ISSUES:` list, calls orchestrator in `REVIEW_FAILED` state, re-runs scheduler. Up to 2 cycles.

---

## License

MIT
