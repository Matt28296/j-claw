# J-Claw — Autonomous Development Agency (v2)

J-Claw is a fully autonomous local-first AI software factory. Describe what you want in plain English — a game, app, website, or film — and the pipeline interprets the creative intent, designs the architecture, plans the full build, writes all the code and media, verifies every output, self-heals any issues, and delivers a production-ready artifact with no human in the loop.

Four layers of intelligence (each with a verified, free-first fallback path — both $0 OAuth tiers are exhausted before any metered Anthropic call):
- **Creative Director** (free-first: Codex → Claude Max CLI, both $0 OAuth; metered Anthropic Sonnet→Opus only as a budget-capped last resort) — interprets intent, determines output type, produces a creative brief *(WHAT)*
- **Technical Architect** (same free-first ladder: Codex → Claude Max CLI → metered Anthropic last) — chooses stack, file structure, ADRs, seeds persistent project memory *(HOW)*
- **Orchestrator** (`ORCHESTRATOR_PROVIDER=anthropic`, difficulty-routed; free-first emergency chain Codex → Claude Max CLI, with metered Anthropic Sonnet→Opus as a budget-capped last resort gated by the `PAID_ORCH_ENABLED` kill-switch) — translates spec into a task DAG, drives the pipeline, self-heals
- **Worker** (local-first ladder: `qwen3:8b → deepseek-coder-v2:16b` → three $0 flat-rate OAuth rungs `grok-build → codex::gpt-5.5 → claude (Max CLI)` → `claude-sonnet-4-6` then `claude-opus-4-8` as a budget-capped last resort, capped by `MAX_PAID_WORKER_CALLS`) — writes all code and runs all generation tasks, local-first; the free OAuth tiers (Grok via SuperGrok, Codex via ChatGPT, Claude via Max subscription) absorb escalations before any Anthropic dollars are spent

---

## Two-Machine Setup (Optional)

J-Claw supports offloading Ollama serving and LoRA training to a second PC over Tailscale:

- **9070 XT** (primary) — orchestrator, scheduler, routing, dataset export, eval/promote
- **3060 Ti** (trainer + sidecar) — extra Ollama serving capacity **or** QLoRA training, never both

The full pipeline (export_dataset → train → GGUF → eval_worker → promote_worker) has been proven
end-to-end. Both machines communicate via Tailscale + Syncthing (`jclaw-training/` and `node_state/`).

| Machine | Setup reference |
|---|---|
| 3060 Ti (trainer + sidecar) | `SETUP_3060TI.md` — full operations reference |
| 9070 XT (orchestrator) | `DEV_NOTES_two_machine_llm.md` — design rationale + run commands |
| Both machines | `Matt28296/jclaw-coord` repo — inter-machine messaging |

Security invariant: a local-Ollama failure must **never** escalate to a paid cloud provider.
Firewall: inbound TCP 11434 scoped to `100.64.0.0/10` (Tailscale CGNAT) only — never public.

---

## What It Does

```
"Build a game like Celeste" / "Make a 30-second explainer film about AI"
            │
            ▼  CREATIVE DIRECTOR (Codex-first, $0 OAuth → Sonnet→Opus fallback)
    Interprets intent → output_type, features, constraints, desired_experience
    NO stack choice — that belongs to the architect
            │
            ▼  TECHNICAL ARCHITECT (Codex-first, $0 OAuth → Sonnet→Opus fallback)
    Reads CREATIVE_BRIEF → chooses confirmed_stack, file_structure
    Creates ADRs (Architecture Decision Records) documenting every major call
    Seeds project_memory/ with architecture.md, coding_standards.md,
    api_contracts.md, known_issues.md, decision_log.jsonl, ADR files
            │
            ▼  INIT (Orchestrator — free-first: Codex → Claude Max CLI → metered Anthropic last)
    Reads CREATIVE_BRIEF + TECH_SPEC → generates project spec (FORMAT 1)
            │  oversized full-stack/game intents decompose into sub-projects (FORMAT 5)
            │  (auto-accepted with --yes, or you review and revise)
            ▼  SPEC_ACCEPTED
    Generates task DAG (FORMAT 2) — up to 75 tasks
            │
            ▼  Execute tasks in topological order (up to 4 parallel workers)
            │   ├─ Per task: CONTEXT BUILDER selects relevant ~4K tokens from memory
            │   ├─ Code tasks      → Worker (Ollama) writes files + optional memory_patch.json
            │   ├─ DevOps tasks    → Worker writes Dockerfile, docker-compose, nginx, CI/CD
            │   ├─ Docs tasks      → Worker writes README, JSDoc, docstrings, CHANGELOG
            │   ├─ Asset tasks     → ComfyUI + DirectML (style-aware checkpoint from brief)
            │   ├─ Audio tasks     → Piper TTS (narration) + FluidSynth/MIDI (music)
            │   ├─ Video tasks     → video_worker (ffmpeg pipeline)
            │   └─ On failure      → EXECUTION_ERROR (Haiku) → retry
            │
            ▼  Memory patch (if worker produced memory_patch.json)
    MEMORY VALIDATOR checks version + operation rules → PASS/WARN/REJECT
    PASS/WARN → ProjectMemory.apply_patch() → increment version
            │
            ▼  Final Review (CLI-first: free Claude Max OAuth `claude -p`; metered API only as fallback when paid is enabled; fails closed)
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

Stacks are tiered by how deeply the pipeline can *prove* the output works on a
Windows build box. A check that can't run (missing tool) is reported as an honest
`⊘ SKIPPED` in HANDOFF.md — never a silent pass.

### Verified stacks

| Stack | Use case | Verification | Depth |
|---|---|---|---|
| `vanilla` | Static HTML/JS/CSS + Tailwind CDN apps | Headless HTML structure check + completeness gate | behavior |
| `phaser` | Phaser 3 browser games (CDN) | Playwright canvas + runtime-error check | behavior |
| `three-js` | Three.js 3D browser scenes (CDN, WebGL) | Playwright canvas check | behavior |
| `react-vite` | React 18 + Vite + Tailwind SPAs | `npm run build` | build |
| `fastapi` | Python REST API + SQLite + Alembic migrations | `pip install` + `alembic upgrade head` | build |
| `full-stack` | React frontend + FastAPI backend in one pipeline run | Both above | build+behavior |
| `web3` | Solidity + Hardhat + ethers.js DApps | `npx hardhat compile && test` | test |
| `video-editor` | Browser-based clip editor — ffmpeg WASM + Canvas API | build | build |
| `tauri` | Rust + WebView desktop apps — lighter than Electron | build | build |
| `godot` | GDScript games | Godot headless syntax check | static |
| `film` | Narrative film / animated explainer — ffmpeg + ComfyUI frames + Piper narration + FluidSynth music | ffprobe + frame integrity + A/V sync | artifact |
| `socket-io` | Node.js + Socket.io real-time multiplayer | `npm install` | install |
| `electron` | Electron desktop apps (contextIsolation + contextBridge) | `npm install` | install |
| `websocket-sse` | Real-time dashboards and data streams | `npm install` | install |

Depth legend: **behavior** = the app is launched and observed; **build/test** = it
compiles and its tests pass; **artifact** = the output file is probed for integrity;
**static** = syntax-level check; **install** = dependencies resolve only.

### Generate-only stacks (no verification on a Windows box)

| Stack | Use case | Why unverified |
|---|---|---|
| `react-native` | Expo managed mobile apps (iOS/Android) | `npm install` only — no iOS/Android simulator on Windows |

Swift and Kotlin generation prompts also exist in the worker, but without a
macOS/Android toolchain the output is code-only — treat it as a starting point,
not a verified artifact.

All stacks also support:
- **PWA output** (vanilla + react-vite): `manifest.json` + `sw.js` — every generated app is installable on mobile/desktop
- **JWT auth** (full-stack): `auth.py`, User model, `/auth/register` + `/auth/login`, React `LoginForm`, `RegisterForm`, `PrivateRoute`
- **DevOps tasks**: Dockerfile (multi-stage, non-root), `docker-compose.yml`, `nginx.conf`, `.github/workflows/ci.yml`, `.env.example`
- **Documentation tasks**: `README.md`, JSDoc comments, Google-style Python docstrings, `CHANGELOG.md`
- **Asset generation**: ComfyUI (DirectML/CUDA) or A1111/Forge WebUI with Creative Director-enriched prompts (SVG/PNG fallback if offline)
- **Audio generation**: Piper TTS binary with narration text from Creative Brief (silent WAV fallback when binary absent)
- **Music generation**: algorithmic MIDI (midiutil, genre-matched: jazz/horror/epic/romance/ambient) rendered via FluidSynth + FluidR3_GM soundfont (silent WAV fallback)
- **Security scanning**: `bandit` (Python) / `npm audit` (Node) — `verification: "security"` task type
- **Lighthouse**: performance + accessibility checks for web projects — `verification: "lighthouse"` task type

---

## Architecture

```
j-claw/
├── orchestrator.txt              Orchestrator system prompt (FORMATs 1–5)
├── creative_director.txt         Creative Director system prompt (Codex-first; Sonnet→Opus fallback)
├── technical_architect.txt       Technical Architect system prompt (Codex-first; Sonnet→Opus fallback)
├── run.bat                       Entry point (Windows)
├── bot.bat                       Telegram bot entry point
├── dashboard.py                  Mission Control dashboard server (port 8765, auto-starts)
├── cc_dashboard.py               Claude Code session dashboard server (port 8766, read-only)
├── openclaw-skill/
│   └── SKILL.md                  OpenClaw skill — invoke j-claw from Telegram/WhatsApp
├── dashboard/
│   └── index.html                Live pipeline dashboard (dark theme, auto-polling)
├── cc_dashboard/
│   └── index.html                Claude Code session dashboard (read-only, tails session JSONL)
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
    ├── music_worker.py           Music generation (algorithmic MIDI → FluidSynth)
    ├── verification.py           Ecosystem detection + ffprobe/frame/security/lighthouse checks
    ├── asset_worker.py           ComfyUI/SD asset generation + SVG fallback
    ├── audio_worker.py           Piper TTS narration + silent WAV fallback
    ├── experience_log.py         EXECUTION_ERROR outcome tracker (JSONL)
    ├── session_log.py            Append-only replayable per-run JSONL transcript (sessions/)
    ├── permissions.py            Action-risk classifier + observe() logging (roadmap #6)
    ├── risk_evidence.py          Aggregates risk_classified events for threshold tuning
    ├── worktree_manager.py       Per-task git worktree isolation (create/copy-out/remove; no git-merge)
    ├── interpretation_risk.py    Deterministic brief-difficulty scoring → role routing
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
| `SPEC_ACCEPTED` | FORMAT 2 | Full task DAG — up to 75 tasks with deps, files, criteria |
| `EXECUTION_ERROR` | FORMAT 3 | Fix for a failed task: `modify`, `split`, or `deprecate` |
| `PROJECT_REVIEW` | FORMAT 4 | Final orchestrator verdict: pass or add follow-up tasks |
| `REVIEW_FAILED` | FORMAT 4 | Self-healing: receives Claude review issues, returns fix tasks |
| `CONTINUE` | FORMAT 2 | Incremental tasks to add a feature to an existing project |

**FORMAT 5 (oversize)**: if a project exceeds the 75-task budget, orchestrator emits a sub-project graph and the harness runs each as its own pipeline instance in dependency order.

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
ollama pull qwen3:8b                  # rung-0: trivial single-file tasks — ~4.9 GB
ollama pull deepseek-coder-v2:16b     # rung-1: primary code worker (MoE, ~8.9 GB Q4_0, ~90% HumanEval)
```

The default `WORKER_LADDER` uses both: `qwen3:8b` for trivial single-file tasks, `deepseek-coder-v2:16b` for the rest (ROCm-validated; ~90% HumanEval vs ~75% for qwen2.5-coder:14b at the same VRAM), escalating to `claude-sonnet-4-6` on retry, with `claude-opus-4-8` as the final rung — a task reaches Opus only after deepseek AND Sonnet have failed it, by which point the retry prompt carries the full error log + triage hints. Both paid rungs share the `MAX_PAID_WORKER_CALLS` budget. On 16 GB VRAM, set `OLLAMA_MAX_LOADED_MODELS=1` if you also run the OpenClaw bot, to avoid VRAM contention.

### Configure

```powershell
copy harness\.env.example harness\.env
# then edit harness\.env with your keys
```

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for the Anthropic control-plane roles (final review / error triage), the planning fallback (director/architect when Codex is unavailable), and the Anthropic worker escalation rungs |
| `ORCHESTRATOR_PROVIDER` | `anthropic` | `gemini` (free tier, recommended) \| `anthropic` \| `openrouter` |
| `GOOGLE_API_KEY` | — | Required when `ORCHESTRATOR_PROVIDER=gemini` (aistudio.google.com — free tier) |
| `OPENROUTER_API_KEY` | — | Alternative orchestrator — set `ORCHESTRATOR_PROVIDER=openrouter` |
| `FINAL_REVIEW_MODEL` / `EXECUTION_ERROR_MODEL` | Haiku | Per-role Anthropic model for review / error triage — bump to Sonnet for higher quality at higher cost |
| `CREATIVE_DIRECTOR_MODEL` / `TECHNICAL_ARCHITECT_MODEL` | Haiku | Vestigial as of Phase 3 — the Creative Director + Technical Architect now plan **Codex-first** via `planning_call` (Codex → Sonnet → Opus); these vars no longer drive their normal path |
| `WORKER_MODEL` | `qwen2.5-coder:14b` | Legacy single Ollama worker model (never Claude); superseded by `WORKER_LADDER` |
| `WORKER_LADDER` | `qwen3:8b → qwen2.5-coder:14b → grok::grok-build → codex::gpt-5.5 → claude-sonnet-4-6` | Weakest→strongest worker ladder. Base routing is always local; a task escalates one rung per retry. The two $0 OAuth rungs are inert unless enabled: `grok::` needs `GROK_CLI_ENABLED=true`, `codex::` needs `CODEX_CLI_ENABLED=true` (both live in the operator's `.env`). |
| `MAX_PAID_WORKER_CALLS` | `15` | Hard cap on paid (non-Ollama, *metered*) worker escalations per project run; once spent, tasks clamp to the strongest local rung. The Codex OAuth rung does NOT count against this budget |
| `CODEX_CLI_ENABLED` | `false` | Master switch for the flat-rate `codex::` OAuth worker rung (ChatGPT Plus/Pro subscription, billed per-subscription not per-token) |
| `CLAUDE_CLI_ENABLED` | `false` | Master switch for the optional `claude_cli::` OAuth worker rung — headless `claude -p` under a **Claude Max** subscription ($0 marginal, same Sonnet/Opus models as the metered API). Live-validated but **ships inert**; shares the operator's interactive Max quota and is ToS-sensitive (see the live-validation checklist in `config.py`). Not in the default ladder. |
| `CODEX_MODEL` | `gpt-5.5` | Model passed to `codex exec` for the OAuth rung |
| `CODEX_EFFORT` | — | Reasoning effort override (`low`/`medium`/`high`); empty leaves Codex's configured default |
| `CODEX_CLI_MAX_CALLS` | `20` | Per-run capacity cap for Codex OAuth calls (separate from the dollar budget — protects the subscription's rate-limit window) |
| `CODEX_TIMEOUT` | `300` | Seconds before a single `codex exec` subprocess is killed (fail-fast → skip rung) |
| `CODEX_HOME` | — | Path to the Codex profile/auth dir passed into the worker subprocess (set if the worker can't see your interactive ChatGPT login) |
| `ORCHESTRATOR_MODEL` | `claude-sonnet-4-6` | Claude model for architect, planning, and review |
| `TECHNICAL_ARCHITECT_ENABLED` | `true` | Set `false` to skip architect pass (legacy mode) |
| `DASHBOARD_AUTOOPEN` | `true` | Auto-open browser to dashboard when pipeline starts |
| `DASHBOARD_PORT` | `8765` | Dashboard server port |
| `WORKER_FALLBACKS` | openrouter free + `ollama::qwen3:8b` | Legacy fallback chain (used only when `WORKER_LADDER` is unset) |
| `MAX_PARALLEL_WORKERS` | `4` | Concurrent Ollama workers (independent DAG branches) |
| `ORCHESTRATOR_MAX_TOKENS` | `16384` | Raise to `32768` for very large full-stack DAGs |
| `SD_API_URL` | `http://localhost:7860` | A1111/Forge WebUI endpoint (used when `ASSET_PROVIDER=sd`) |
| `ASSET_PROVIDER` | `sd` | `comfyui` \| `sd` \| `none` |
| `COMFYUI_API_URL` | `http://localhost:8188` | ComfyUI API endpoint (used when `ASSET_PROVIDER=comfyui`) |
| `COMFYUI_CHECKPOINT` | auto-detect | ComfyUI checkpoint name; empty = use first installed model |
| `COMFYUI_WIDTH` / `COMFYUI_HEIGHT` | `768` / `512` | Output image dimensions |
| `PIPER_BINARY` | — | Path to `piper.exe` (download from github.com/rhasspy/piper) |
| `PIPER_VOICE` | — | Path to `.onnx` voice model (e.g. `en_US-ryan-high.onnx`) |
| `FLUIDSYNTH_BINARY` | — | Path to `fluidsynth.exe` for MIDI→WAV rendering |
| `FLUIDSYNTH_SOUNDFONT` | — | Path to `.sf2` soundfont (e.g. `FluidR3_GM.sf2`, ~141 MB) |
| `GODOT_PATH` | `godot` | Path to Godot 4 CLI binary (for headless verification) |
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
| OpenClaw auto-restart watchdog | ✅ Done (`C:\Users\Tyler\openclaw-watchdog.ps1`) |
| Reliable bot replies | ✅ **Done (2026-06-04)** — Haiku router, replies confirmed live on @JarvisClaw96bot |

> ### ✅ Resolved (2026-06-04): the bot now replies reliably
>
> **Fix applied & confirmed live.** The router model was switched from the crash-prone
> `ollama/qwen3:8b` to `anthropic/claude-haiku-4-5-20251001`, with `tools.profile: minimal`
> (router-light). A direct agent turn and a real Telegram message both return a coherent
> Haiku reply. The router is *not* the J-Claw worker, so the "Ollama-only worker" constraint
> doesn't apply — Haiku-as-router is the intended design.
>
> **Root cause was subtler than the config.** The config on disk was already correct, but the
> *running* gateway was a **stale orphaned process** (started before the config edit) still
> serving the old `qwen3:8b` router in memory — so inbound messages were received but no reply
> was produced. The trap: `openclaw daemon restart` / `gateway stop` only manage the Windows
> **Scheduled Task**, not a gateway launched independently. The fix was to kill the PID
> listening on `:18789` directly, then `openclaw daemon start` (fresh process re-reads the
> Haiku config). Verify the live router with:
> `openclaw agent --agent main --message "PONG and your model"` → expect `anthropic/claude-haiku-4-5`.
>
> **Underlying qwen3 instability still stands** (`model runner has unexpectedly stopped` on the
> AMD RX 9070 XT — runner instability, not context size), which is *why* the bot runs on Haiku.
> Optional hardening remains: `OLLAMA_MAX_LOADED_MODELS=1` (bot/worker VRAM contention),
> `ollama signin` (web_search), and pruning the ~12 dangling Ollama manifests.

### Managing the gateway

The gateway runs as a Windows Scheduled Task (`OpenClaw Gateway`), managed via the CLI:

```powershell
openclaw daemon status      # install state + connectivity probe
openclaw daemon start        # start the supervised gateway (reads ~/.openclaw/openclaw.json)
openclaw daemon restart      # stop + start the Scheduled Task
openclaw health              # live gateway: channels, sessions, event-loop health
```

> ⚠️ **Gotcha:** `daemon`/`gateway stop` only act on the Scheduled Task. If a gateway was ever
> launched independently, it can keep running an old in-memory config even after you edit
> `openclaw.json`. If config changes don't take effect, find the PID on `:18789`
> (`Get-NetTCPConnection -LocalPort 18789`), `Stop-Process` it, then `openclaw daemon start`.

Config edits to `openclaw.json` hot-reload for *most* settings, but a **model/router change is
only guaranteed to take effect after a full restart** (see the resolved issue above).

### Architecture note

OpenClaw's embedded agent acts as a thin **router** — it reads the j-claw SKILL.md and invokes `run.bat`. The actual build runs via the Creative Director + Orchestrator + Worker pipeline locally. The router model only needs to route, so a small reliable model (Haiku, or a stable local model) is appropriate; this is a separate concern from the J-Claw code-generation worker, which is always local Ollama.

> **Security note**: Before installing any third-party OpenClaw plugins, audit their source code. OpenClaw plugins run in-process with full OS privileges — no sandbox. The `@alan512/ExperienceEngine` plugin was reviewed and rejected (exfiltrates task data to external LLMs). The `@openclaw/memory-lancedb` plugin is safe only when configured with local Ollama embeddings.

---

## Asset Generation

Image assets (sprites, icons, backgrounds) are generated locally via ComfyUI or AUTOMATIC1111/Forge:

- **ComfyUI** (`ASSET_PROVIDER=comfyui`, default): async workflow API on port 8188. Supports DirectML (AMD) and CUDA. Auto-detects installed checkpoint. Start via `run_amd_gpu.bat` (DirectML) or standard launcher.
- **A1111/Forge** (`ASSET_PROVIDER=sd`): sync API on port 7860.
- **Disabled** (`ASSET_PROVIDER=none`) or backend unreachable: SVG/PNG color-block placeholders are written instead (pipeline continues unblocked).

Configure: `ASSET_PROVIDER`, `COMFYUI_API_URL`, `COMFYUI_WIDTH`, `COMFYUI_HEIGHT`, `COMFYUI_CHECKPOINT` in `.env`.

> ℹ️ **DirectML / RDNA4 note (RX 9070 XT, updated 2026-06-16):** an earlier run this session
> produced **RGB noise** under `torch-directml` and the fix was assumed to be a ROCm migration.
> A later same-session verification **contradicts that on the current config** — ComfyUI on a clean
> `--directml` with the `RealVisXL_V5.0_fp16` checkpoint produced a **clean, coherent noir frame**
> (verified by viewing the PNG). So image gen is **working on this card** as configured; the noise
> was likely checkpoint-specific (an anime model on a noir scene) or transient. If noise recurs,
> the contingency is **ROCm** (native Windows ROCm 7.2.1 or WSL2) + a photoreal checkpoint. The
> harness fixes (RAM scheduling, ffmpeg cwd, frame guard) are independent and verified. See
> `SESSION_HANDOFF.md` (PR #71) for the full history.

---

## Audio Generation

Narration and voice-over for film/audio projects via Piper TTS (local binary, no GPU, no internet):

1. Download the Piper binary for Windows from github.com/rhasspy/piper/releases
2. Download an ONNX voice model (e.g. `en_US-ryan-high.onnx`)
3. Set `PIPER_BINARY` and `PIPER_VOICE` in `.env`
4. Audio tasks are routed automatically — ~0.26× realtime on CPU
5. Silent `.wav` placeholders are written if the binary or voice model is absent

Configure: `PIPER_BINARY`, `PIPER_VOICE` in `.env`.

---

## Music Generation

Background music for film/audio projects via algorithmic MIDI composition rendered with FluidSynth:

1. Download FluidSynth for Windows from github.com/FluidSynth/fluidsynth/releases
2. Download the FluidR3_GM.sf2 soundfont (~141 MB General MIDI)
3. `pip install midiutil` in the harness venv
4. Set `FLUIDSYNTH_BINARY` and `FLUIDSYNTH_SOUNDFONT` in `.env`
5. Genre is auto-detected from the creative brief: `jazz` (walking bass + piano), `horror` (tremolo strings + pad), `epic` (brass + drums), `romance` (strings + piano), `ambient` (pad)
6. Silent `.wav` placeholders are written if FluidSynth or the soundfont is absent

---

## Deployment Hooks

Run a deployment command automatically after every successful project build:

```
# harness/.env
DEPLOY_HOOK=<venv-python> harness\deploy_netlify.py   # Netlify (recommended — see below)
DEPLOY_HOOK=vercel --prod --yes                       # Vercel
DEPLOY_HOOK=railway up                                # Railway
DEPLOY_TIMEOUT=300
```

The hook runs in the project output directory after git commit, **gated to static web
stacks** (`vanilla`, `react-vite`, `phaser`, `three-js`) — APIs, films, and desktop apps
are skipped with an honest `⊘` note instead of half-deployed. The outcome (URL or
skip/failure reason) is written to a `## Deployment` section in `HANDOFF.md` and included
in the Telegram terminal push.

**`harness/deploy_netlify.py`** makes Netlify deploys truly unattended (a bare
`netlify deploy` prompts interactively in an unlinked directory): it authenticates via
`NETLIFY_AUTH_TOKEN` (create one at app.netlify.com → User settings → Applications),
finds-or-creates one site per project (`jclaw-<slug>` — re-runs redeploy the same URL),
publishes `dist/` when present (react-vite) else the project root, and prints exactly one
URL for the harness to record. Missing token = loud recorded failure, never a hang.

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

### Claude Code Session Dashboard (`cc_dashboard.py`, port 8766)

A separate, **read-only** dashboard for watching an interactive **Claude Code** session (distinct from
the build pipeline above). It tails the active Claude Code session transcript on disk and renders it live
— no configuration, no API keys, and **zero token cost** (pure local file I/O; it makes no LLM calls).

```powershell
python cc_dashboard.py            # http://127.0.0.1:8766/cc_dashboard/index.html
python cc_dashboard.py --session <uuid> --no-browser   # pin a session / suppress auto-open
```

Live panels: **session vitals** (cwd, branch, model, plan/permission mode, elapsed), a turn-by-turn
**timeline** (user → tool call → result ✓/✗ → assistant text), a **sub-agent fleet** (every `Agent`
spawn with status/duration/result, sourced from the per-agent transcripts), **tokens & context**
(cost tokens, cache-read shown separately, est. cost, context-window fill %), and **files / git / PRs**.

It defaults to `127.0.0.1` only (the transcript contains raw prompts + tool I/O — never bind it to a
LAN), and is fully self-contained (inline CSS/JS, zero external refs). A background tailer reads only the
newly-appended bytes of the JSONL each second and writes an atomic `cc_state.json` the page polls.

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
| ComfyUI/SD asset generation + Piper TTS narration + FluidSynth music | ✅ |
| Experience tracker (JSONL fix-outcome log) | ✅ |
| Orchestrator JSON truncation fix + FORMAT 5 bug fix | ✅ |
| OpenClaw skill deployed + Telegram bot paired | ✅ |
| Creative Director (Codex-first; Sonnet→Opus fallback) — WHAT layer | ✅ |
| Technical Architect (Codex-first; Sonnet→Opus fallback) — HOW layer + ADRs | ✅ |
| Persistent project memory (project_memory/ + runtime_memory/) | ✅ |
| Context Builder — deterministic ~4K token selection per task | ✅ |
| Memory Patch System — operation-based, optimistic concurrency | ✅ |
| Memory Validator — PASS/WARN/REJECT rules, <10ms, no LLM | ✅ |
| Architecture Decision Records (ADR-001-*.md) | ✅ |
| DevOps specialist agent (Dockerfile, docker-compose, nginx, CI/CD) | ✅ |
| Documentation specialist agent (README, JSDoc, docstrings, CHANGELOG) | ✅ |
| Security verification (bandit / npm audit) — FAIL on HIGH/CRITICAL only | ✅ |
| Lighthouse verification (performance + accessibility) — perf < 0.5 or a11y < 0.7 FAIL | ✅ |
| Godot headless check — `godot --headless --check-only`, triggered on `none` when `project.godot` present | ✅ |
| HTML meta warnings — meta description, html lang, img alt (WARN not FAIL) | ✅ |
| Expo web export check — `npx expo export --platform web` appended to react-native build | ✅ |
| FORMAT 5 wiring passthrough — `wiring.json` forwarded between sub-projects | ✅ |
| orchestrator.txt — tech_spec INIT docs, documentation task type, security/lighthouse enum | ✅ |
| Dashboard auto-start + browser open on pipeline start | ✅ |

| E2E test generation — `e2e_generator.py` produces `tests/e2e.spec.ts` after pipeline for web stacks | ✅ |
| IPFS deployment — `scripts/pin-to-ipfs.js` (Pinata API) auto-generated for Web3 projects | ✅ |
| Stripe integration — payment prompts in fastapi + react-vite stacks (checkout, webhook, .env) | ✅ |
| Swift (iOS/SwiftUI) + Kotlin (Android/Compose) native mobile stacks | ✅ |
| **Phase 1 — verification honesty (2026-06-04, live-validated):** E2E + project-Playwright checks now **gate** the project and feed the self-heal loop (previously computed then ignored); generated Playwright tests use relative `goto('/')` against the configured `:18090` baseURL (was a dead `:3000`) | ✅ |
| **Phase 1 — SKIP ≠ PASS:** checks that auto-pass only because a tool/runner is missing are marked `⊘ SKIPPED` (not a verified pass) in `HANDOFF.md`, so a green report is no longer silently hollow | ✅ |
| **Phase 1 — real game check:** Playwright game check now fails on a zero-size canvas and observes a 1.5s window so game-loop runtime errors surface, not just init-time errors | ✅ |
| **Sprint A — worker ladder + budget:** weakest→strongest ladder (`qwen3:8b → qwen2.5-coder:14b → claude-sonnet-4-6`; rung-1 later upgraded to `deepseek-coder-v2:16b`), `MAX_PAID_WORKER_CALLS` paid-escalation cap, unified dispatch timeouts, bounded heal loop, mypy/ruff wired (`ac3bdce`) | ✅ |
| **Escalation-tax fix:** binary/image tasks → `asset_worker` (valid PNG placeholder when SD offline), single-file script salvage before paying for Sonnet (`b479e57`) | ✅ |
| **Heal-loop convergence:** `heal_metrics.py` issue-set similarity + escalate-then-stop on non-convergence (`056ad67`) | ✅ |
| **Phase 2 — movies pipeline:** `generate_video` reads `task.files` + ffmpeg render, real film/video-editor director→renderer prompts, `music_worker` real-backend gate, honest `frame_integrity`/`sync_check` (`056ad67`) | ✅ |
| **OpenClaw bot fixed (2026-06-04):** Haiku router + `tools.profile: minimal`; replies confirmed live (root cause: stale orphaned gateway process) | ✅ |
| **Pre-merge review fixes:** failure-handoff phase tracking made functional; worker-timeout liveness limitation documented (`7c7656e`) | ✅ |
| **Completeness gate + cost telemetry (PR #10):** static stub/asset/duplicate-decl checks gate per-task and project-level; per-build Anthropic cost + prompt-cache telemetry | ✅ |
| **Telegram terminal push (PR #11):** `notify.py` pushes PASS/FAIL/crash with heal cycles, cost line, HANDOFF path, deploy URL — the factory is silent while working, loud at the end | ✅ |
| **Telegram FIFO queue + `/continue` (PR #13):** builds queue strictly sequentially (one GPU); feature additions to existing projects from the phone | ✅ |
| **Experience lessons → planning (PR #14):** recurring failure patterns per stack aggregated into a ≤500-token lessons block in the orchestrator INIT/DAG payloads | ✅ |
| **Film render execution + honest video gates (PR #15, 2026-06-11):** `_ensure_rendered` executes the project's render pipeline (ffmpeg edit-script lines / Python entry) as part of verification; missing video now **FAILS** ffprobe/frame/sync instead of "auto-passed"; film stacks never get silent placeholder videos; `completeness.py` statically flags entry-script imports to never-written modules | ✅ |
| **FORMAT 5 aggregation + parent film assembly (PR #16):** sub-project outcomes collected (one crashed scene no longer sinks the rest); parent exit code + single aggregate Telegram push reflect honest aggregate; parent concatenates scene clips → frame-checked `final.mp4`; aggregate parent `HANDOFF.md` | ✅ |
| **Unattended Netlify deployment (PR #17):** `deploy_netlify.py` wrapper + stack gating + `## Deployment` in HANDOFF — see Deployment Hooks | ✅ |
| **Validation-driven hardening (PRs #18–#23, 2026-06-11):** six defects caught by live film validation runs — FORMAT 5 recursion spiral stopped (runtime `decomposition_allowed: false`); assembly sub-projects detected by name/goal/dependency-shape and skipped; render shell scripts executed via Git Bash (WSL-stub rejected); task completion gated on declared files actually existing; video tasks routed by output not label; final review fails **closed** on API errors, can finally see `.sh`/`.sol`/`.gd` files, and all stack reads go through `config.spec_stack()` (was silently reading an empty top-level key) | ✅ |
| **Film duration honesty (PR #25):** rendered video under half the expected duration (shotlist sum or "N-second" goal phrase) fails the build — ffprobe alone passed a 1-second render of a 20-second scene | ✅ |
| **Netlify deployment LIVE-VALIDATED (PR #26, 2026-06-12):** token configured; wrapper hardened from live testing — site management via Netlify REST API (the CLI's Windows cmd shim mangled JSON and minted randomly-named sites), CLI candidates probed with `--version` (both pre-existing installs were broken), `.env` self-load. Proof: two consecutive deploys → same named site, HTTP 200 | ✅ |
| **Role-model right-sizing + cache fix (PR #28, 2026-06-12):** Creative Director Opus→Haiku, Technical Architect Sonnet→Haiku, Final Review Sonnet→Haiku (new `FINAL_REVIEW_MODEL`); `e2e_generator` was the only uncached Anthropic call — fixed. ~30–50% cheaper per build | ✅ |
| **Orchestrator context-bloat elimination (PR #29):** `REVIEW_FAILED` sends a slim task list (`{id, files, status}`; failed tasks keep type+objective) instead of all 50 full task objects; `EXECUTION_ERROR` sends a 3-field `dag_summary` instead of the full `active_dag` — ~40–70% fewer orchestrator input tokens on large builds | ✅ |
| **Gemini free-tier orchestrator (PR #30):** `ORCHESTRATOR_PROVIDER=gemini` runs the orchestrator on Gemini 2.5 Flash via Google's OpenAI-compatible endpoint, called directly so the AI Studio free tier (1M tokens/day) applies — validation builds drop to ~$0 orchestrator spend. Live-validated INIT call; Anthropic stays the default + instant fallback | ✅ |
| **Worker rung-1 upgrade (2026-06-12):** `deepseek-coder-v2:16b` (MoE, 8.9 GB Q4_0, ~90% HumanEval) replaces `qwen2.5-coder:14b` as rung-1 in the worker ladder; ROCm smoke-tested on the RX 9070 XT — clean output, no crash. `WORKER_LADDER` updated in `harness/.env` | ✅ |
| **Transient-error fallback hardening (PR #34, 2026-06-12):** Gemini 503s raise `InternalServerError`, which escaped the retry loop (only `RateLimitError` was caught) and crashed scene sub-projects — the flash→flash-lite fallback never engaged. Both orchestrator retry loops now catch 5xx + connection errors; live-proven on the next two validation runs | ✅ |
| **`project_type: film` schema fix (PR #35):** Gemini answers `'film'` literally where Claude happened to emit a compliant enum value — validator + both prompt lists now include `film` | ✅ |
| **Opus 4.8 last-resort worker rung (PR #36):** $5/$25 per MTok (~1.67× Sonnet) made a final-escalation rung economical; reachable only after deepseek AND Sonnet fail the same task; shares the paid-call budget | ✅ |
| **Dashboard spawn guard (PR #37):** every build spawned a duplicate dashboard server; 15 stacked instances on port 8765 wedged the Mission Control UI. `_start_dashboard` now probes the port first | ✅ |
| **Film validation v3–v6 (2026-06-12 third session):** four live runs, four real defects caught (the PR #34/#35 fixes above + UTF-8 launch env + the DAG-stage decomposition gap). All are Gemini-literalism defects — Claude inferred intent where Gemini follows the prompt/schema literally. Render path not yet reached — v7 pending | 🔄 in progress |
| **DAG-stage decomposition guard + Gemini retry pacing (PR #39, 2026-06-12):** scene sub-projects were re-decomposing at `SPEC_ACCEPTED` (Gemini returned FORMAT 5 again), tripling orchestrator calls per build and exhausting the free-tier quota before any task ran. Fix: `SPEC_ACCEPTED` payload now carries `decomposition_allowed: false` when inside a sub-project (mirrors the proven INIT guard). Gemini 429 retry delay parsing added: was waiting blind 35–105s on "retry in 3s" errors — now reads Google's `RetryInfo.retryDelay`. Per-build orchestrator calls: ~6–8 (was 18–24) | ✅ |
| **Emergency cross-provider orchestrator fallback (PR #40, 2026-06-12):** `CompositeOrchestrator` + `make_orchestrator()` factory — when Gemini exhausts all retries, automatically routes the same call to Anthropic Sonnet instead of crashing. Availability failures go sideways to another provider at the same tier (Sonnet); capability failures escalate up the worker ladder (Opus rung, PR #36). Config: `ORCHESTRATOR_EMERGENCY_PROVIDER` / `EMERGENCY_ORCHESTRATOR_MODEL` | ✅ |
| **LLM layer test suite (PR #41, 2026-06-12):** `harness/test_llm_layers.py` — 25 mocked tests covering every LLM call layer and fallback path: both orchestrator providers (all retry/fallback/error shapes), `CompositeOrchestrator`, `routed_rung` (4-rung ladder incl. Opus), `execute_task` attempt chain (rung walk-up, `ValueError` short-circuit, paid-budget clamp, all-exhausted), final review fail-closed regression guard. Zero API spend | ✅ |
| **Mission Control dashboard telemetry (PR #44, 2026-06-13):** all 12 live panels wired end-to-end — agent network, task drawer, cancel/continue/retry controls, cost breakdown, rung badges, health bar, live test results, healing timeline; model display fix; heal badge no longer double-divides count | ✅ |
| **orchestrator.txt render + HTML rules (PRs #45–#46, 2026-06-13):** render scripts must call `subprocess.run(cmd, check=True)` (never `print(cmd)`); Windows ffmpeg constraints documented (`drawtext` / `geq=` unavailable; use `color=` solid backgrounds); HTML stub prevention — the `index.html` task must name every CSS `<link>`, CDN `<script>`, and page section by `id` + visible content in its `objective` | ✅ |
| **Dashboard state wiring (PR #47, 2026-06-13):** `on_cost()` normalization (`total_usd`/`by_model`/`tokens`/`paid_calls`); `on_review_failed()` emits event with "REVIEW_FAILED" text so heal badge counter works; `on_openclaw_stamp()` wired from `handoff.py` | ✅ |
| **Gemini timeout + APITimeoutError fallback (PR #48, 2026-06-13):** `_OpenAICompatOrchestrator` now passes `timeout=ORCHESTRATOR_TIMEOUT` to every `chat.completions.create()` call and catches `APITimeoutError` as an availability failure — triggers the model fallback chain, then `CompositeOrchestrator` Sonnet fallback. Before: indefinite freeze when Gemini stalled. After: 300s timeout → auto-fallback | ✅ |
| **Factory rehearsal item #1 (2026-06-13):** `/run Build a simple personal portfolio website` — build ran end-to-end, Netlify URL deployed, Telegram notification received ✅. CSS worker quality gap identified → PR #46 addresses root cause for future builds | ✅ |
| **CDN stack unit-test guard (PR #49, 2026-06-14):** Two-part fix for vitest tasks burning paid call budget on CDN-only projects: (1) `orchestrator.txt` — `vanilla`/`phaser`/`three-js` stacks may NOT plan any `qa` task with `verification: "unit_test"` or `"smoke"` (no `node_modules`, no install step); (2) `verification.py` — `node` ecosystem unit_test auto-passes when `node_modules/` is absent. Root cause: a CDN project's `qa` task writing `package.json` shifted ecosystem detection to `"node"`, causing `npm test` to run and fail all 4 retries, exhausting the paid call budget before `index.html` or JS tasks could complete | ✅ |
| **One-file-per-task ≤150 line limit (PR #50, 2026-06-14):** `orchestrator.txt` principle 2 extended — each task writes exactly one file, ≤150 lines (a 14B local model's reliable output window). CSS must never be a single monolithic file — split by concern: `variables.css`, `reset.css`, `layout.css`, `components.css`, `animations.css`, `responsive.css`, one task per file. JS must never be a single monolithic file — split by feature (`js/scroll.js`, `js/menu.js`, etc.). Root cause: a single `css/style.css` with all styles exceeded the output token window at every rung — deepseek wrong format, Sonnet/Opus both truncated mid-JSON, all 4 retries failed | ✅ |
| **Unique file ownership + monolithic file ban (PR #51, 2026-06-15):** Workers always write the COMPLETE file — two tasks declaring the same filename means the second silently overwrites the first. Added explicit "one file, one task owner" rule to `orchestrator.txt` principle 2. Banned generic filenames (`css/style.css`, `js/app.js`, `js/main.js`) in favour of named split files. Fixed internal consistency: `worker.py` vanilla service worker template was hardcoding `./app.js`; `orchestrator.txt` HTML example referenced banned names | ✅ |
| **completeness.py stripping order fix (PR #52, 2026-06-15):** `_strip_comments_strings` was stripping `//` line comments before strings, so `'// text'` had its `//` stripped first, corrupting the string boundary and leaving `var(--neon-yellow)` exposed as an apparent bare function call (checker reported "function var() called but never defined"). Fixed stripping order: single-quoted strings → double-quoted → template literals → block comments → line comments (last). Belt-and-suspenders: CSS function names added to bare-call allowlist | ✅ |
| **ID/class coordination + JS toggle class rules (PR #53, 2026-06-15):** `orchestrator.txt` — every `<section>` must carry BOTH `id` (anchor nav) AND `class` matching its CSS selector; a section with only `id` silently breaks all `.hero { }` rules. Any JS toggle task must name the toggled CSS class in its objective and depend on a CSS task that defines rules for that class (a toggle adding a class with no CSS = visible no-op). HTML example updated to `<section id="hero" class="hero">` pattern. Root cause found in Tony Montana v6: `<section id="hero">` without `class="hero"` + `dark-mode.js` toggling `html.light-mode` with no CSS rules defined | ✅ |
| **rmtree read-only .git objects fix (PR #54, 2026-06-15):** `shutil.rmtree` fails with `PermissionError: [WinError 5]` on the second run of any project because `git_commit_project` leaves a `.git` folder with read-only object files (standard Windows git behavior). Added `onexc=_force_remove_readonly` to `main.py` — on any permission error, `chmod` the file to `S_IWRITE` then retry. Same pattern git-for-windows uses internally. Previously every repeat build of the same project failed before any tasks ran | ✅ |
| **Ollama token tracking in cost panel (PR #60, 2026-06-15):** `harness/cost.py` accumulates `prompt_eval_count`/`eval_count` from every Ollama response via `record_ollama_usage()`; `state_writer.on_cost()` normalises and forwards `ollama_tokens.input/output`; dashboard renders a "local (ollama)" row in the cost table (visible even when cloud spend is zero) | ✅ |
| **Ollama connection error guard — no silent cloud escalation (PR #61, 2026-06-15):** `_is_ollama_unavailable(exc)` in `worker.py` distinguishes infrastructure failures (server unreachable: `ConnectionError`, `httpx.ConnectError`, "connection refused" patterns) from capability failures (bad output, wrong JSON). Infrastructure failure on an Ollama rung raises `RuntimeError` immediately — the worker ladder does NOT walk up to Sonnet/Opus. Discovered after a $0.50 build where all 23+ tasks silently escalated to Sonnet because Ollama was down. 32/32 tests green | ✅ |
| **Worker quality rules (PR #63, 2026-06-15):** Three systematic gaps fixed after the NES portfolio rehearsal build. `orchestrator.txt`: Tailwind CDN changed from MANDATORY to CONDITIONAL (never add it for pixel-art/retro/custom-aesthetic projects); DOM event listener binding rule added (unbound method reference loses `this` — always use arrow wrapper or bind in constructor); contact form rule added (static vanilla projects must use `mailto:` placeholder, never `formspree.io/f/REPLACE_ME` which silently 404s). `harness/completeness.py`: `_missing_manifest_icons()` added — parses `manifest.json`, flags any declared `icons[].src` paths that don't exist on disk. 32/32 tests green | ✅ |
| **CANCELED state on /cancel (PR #65, 2026-06-15):** `cmd_cancel` in `telegram_bot.py` killed the subprocess but never wrote a terminal state to `mission_control.json` — the killed process can't flush state itself. Added `_write_canceled_state()`, called immediately after kill: patches the JSON file directly from the bot process, sets `pipeline_state: "CANCELED"`, clears `active_agent`, writes the `terminal` block, marks running `agent_nodes` as canceled. Dashboard now flips to CANCELED terminal state immediately instead of hanging on the last EXECUTING snapshot | ✅ |
| **ComfyUI DirectML backend (PR #67, 2026-06-15):** `asset_worker.py` rewritten with a ComfyUI backend: async SDXL workflow (`/prompt` → poll `/history/{id}` → `/view`), auto-detects installed checkpoint, configurable resolution. `ASSET_PROVIDER=comfyui` in `.env`. `run_amd_gpu.bat` fixed (`--cpu` → `--directml`) for AMD RX 9070 XT. A1111/Forge sync path preserved for `ASSET_PROVIDER=sd` | ✅ |
| **Local Piper TTS + FluidSynth music (PR #68, 2026-06-15):** `audio_worker.py` rewritten — replaces Coqui TTS HTTP server with Piper binary subprocess (stdin→WAV, ~0.26× realtime CPU). `music_worker.py` rewritten — replaces MusicGen/audiocraft with `midiutil` MIDI composition rendered via FluidSynth + FluidR3_GM soundfont. Genre auto-detected from creative brief (jazz/horror/epic/romance/ambient); jazz uses walking bass + Cm7 piano comps at 120 BPM (correct for noir film test). Film stack is now fully local: ComfyUI frames ✅ + Piper narration ✅ + FluidSynth music ✅ + ffmpeg assembly ✅ | ✅ |
| **Orphaned-run reconciliation (PR #72, 2026-06-16):** a killed/restarted bot could leave in-flight runs stuck in `EXECUTING`. The bot now reconciles orphaned runs on startup so a restart can't freeze the pipeline state (the long-standing restart-orphan trap) | ✅ |
| **gitignore bot runtime logs (PR #74, 2026-06-16):** `*.log` ignored — bot daemon logs can contain the Telegram token in API request URLs | ✅ |
| **Media + mission-control test coverage (PR #75, 2026-06-16):** `tests/test_mission_control.py` (8) covers `state_writer` terminal transitions, deploy/cost/review recording, atomic-write cleanup, and the `dashboard.py` HTTP control endpoints; `tests/test_media_workers.py` (6) covers genre/duration detection plus real Piper-TTS/FluidSynth WAV smoke tests that skip cleanly when binaries are absent. 14/14 green | ✅ |
| **Style-aware ComfyUI checkpoints (PR #76, 2026-06-16):** `asset_worker.py` detects realistic vs anime/cartoon from the brief and routes to the matching checkpoint (RealVisXL / Animagine) with per-style prompt modifiers; realistic is the default and the noir-film case resolves to realistic. Checkpoint selection falls back through style-match → other configured → first available → config name when ComfyUI is unreachable. New config: `COMFYUI_CHECKPOINT_REALISTIC/ANIME`, `COMFYUI_STEPS=26`, `dpmpp_2m`+`karras`. `tests/test_asset_worker.py` — 12 pure-function tests | ✅ |
| **Codex CLI OAuth worker rung (2026-06-16):** an optional flat-rate `codex::gpt-5.5` rung between the strongest local rung and Anthropic. It shells to `codex exec` (read-only sandbox, stdin prompt, clean output via `-o`) under the operator's ChatGPT Plus/Pro subscription — so escalations that would otherwise spend Anthropic dollars are caught for free first, making Anthropic the true last resort. Budget logic is now provider-class-driven: `METERED_PROVIDERS` (anthropic/openrouter) draw on `MAX_PAID_WORKER_CALLS`; `OAUTH_PROVIDERS` (codex) draw on a separate `CODEX_CLI_MAX_CALLS` capacity counter and never spend dollars. If Codex is unavailable (not logged in / 401 / 429 / quota / exe missing / timeout) the rung latches off for the run and escalation continues cleanly — the build never blocks on interactive reauth. $0 OAuth telemetry surfaces in the cost panel as a per-provider call-count row. Off by default (`CODEX_CLI_ENABLED=false`); `test_llm_layers.py` — 7 new mocked tests. **Landed as PR #79, hardened by PR #81** (atomic latch/reserve under `_oauth_lock`, narrowed unavailability classifier, `success=False` failure telemetry; suite now **40 green**). **Live-validated 2026-06-16** — first real `codex exec` returned valid JSON in ~9s | ✅ |
| **Media-backend telemetry labels (PR #80, 2026-06-16):** `scheduler.py` reported the pre-rewrite backends (`sd-webui`/`coqui-tts`/`musicgen`); now reports the live ones (`comfyui`/`piper-tts`/`fluidsynth`). Cosmetic dashboard fix, no behavior change | ✅ |
| **Gemini quota latch + free-first orchestrator chain (PR #105, 2026-06-17):** quota-class 429s (daily `RESOURCE_EXHAUSTED`) on the Gemini path latch the provider off for the rest of the run (`_gemini_quota_disabled`, `GEMINI_QUOTA_FAILFAST` gate) so `CompositeOrchestrator` drops straight to the emergency chain instead of burning the 30–60s `retryDelay` on every call; transient throttles and OpenRouter keep the legacy chain-walk + backoff. `CodexOrchestrator` (validate + retry) added; `CompositeOrchestrator` generalized to an ordered chain and `make_orchestrator` rewired **free-first** Codex→Sonnet→Opus. `reset_orchestrator_run()` clears the per-run latch and is wired into both `run_project` and `run_continuation` start (beside `reset_paid_budget`). +14 mocked tests; harness suite 89 passed / 1 skipped | ✅ |
| **Offline Matrix agent-dashboard (PR #105, 2026-06-17):** the in-session agent-swarm dashboard integrates the Matrix Mission Control UI, splits assets into `css/` + `js/`, and drops the Tailwind CDN — runs fully offline (no CDN, service worker, or manifest; zero external refs). Per-LLM token rollup on `/api/agents` (totals object + per-card readout); 24 tests | ✅ |
| **j-claw dashboard OAuth tiers + per-task tokens (PR #105, 2026-06-17):** Grok mislabel fixed via `_OAUTH_PREFIXES` (checked before cloud) → OAuth-tier models render the purple `OAUTH $0` badge; rung badges remapped (R0–1 local, R2–3 oauth, R4 sonnet, R5+ opus) with metered `gpt-4`/`4o` staying cloud. Per-task `tokens_by_model` merged additively across retries, persisted to `mission_control.json`, rendered in the task drawer + cost rollup | ✅ |
| **Claude Max CLI OAuth worker rung (PR #107, 2026-06-17):** optional third `$0` OAuth rung — headless `claude -p` under a Claude Max subscription, same Sonnet/Opus models as the metered API, mirroring the Codex/Grok rung pattern. Hardened per a Codex review (env scrubbed of `ANTHROPIC_API_KEY` so it uses the subscription not the metered key; `--tools "" --strict-mcp-config --setting-sources "" --disable-slash-commands --no-session-persistence` + worker `--system-prompt-file` for pure generation) and **live-validated** (clean `{"files":[...]}`, `num_turns:1`). Ships **inert** + ToS-gated; shares the operator's interactive Max pool. 7 mocked tests | ✅ |
| **Append-only replayable session log (PR #115, 2026-06-17):** `harness/session_log.py` — per-run append-only JSONL transcript; the observability substrate for the approved Claude-Code-style upgrades roadmap (item #5). First half of Milestone 1 | ✅ |
| **Observe-only action-risk classifier (PR #116, 2026-06-17):** `harness/permissions.py` — `classify_action()` scores every side-effecting op (deploy/install/git/delete/llm-cli) by blast radius and logs a `risk_classified` event **without blocking** (roadmap item #6, logging-only half; enforcement is a deliberate later increment). Second half of Milestone 1 | ✅ |
| **cc_dashboard per-model $Cost column + TOTAL row + workflow-agent scanning (PRs #121/#124, 2026-06-17):** `cc_dashboard/index.html` — `MODEL_PRICES` prefix-map, `calcCost()`/`fmtCost()` helpers, `$Cost` column per model row, bold TOTAL row (when ≥2 models); `cc_dashboard.py` — `_scan_workflow_agents()` surfaces workflow subagents (started/result from `subagents/workflows/*/journal.jsonl`) in the Sub-agent Fleet panel; $0, pure file I/O | ✅ |
| **Git worktree isolation per task (PR #122, 2026-06-17):** `harness/worktree_manager.py` — `WorktreeManager` with `create/merge_and_remove/remove`; `_merge_lock` serializes concurrent merges; worktrees are created as siblings of the repo root (`repo.parent/.jclaw_worktrees/<task_id>`), discarded on verification fail, merged via `--no-ff` only on pass; 45 tests. `harness/scheduler.py` wired: code tasks run in isolation; asset/audio/video/music bypass unchanged; graceful degradation when git unavailable | ✅ |
| **Phase 4 difficulty routing + interpretation-risk CD + per-role Codex quotas (PR #123, 2026-06-17):** `harness/interpretation_risk.py` (new) — deterministic `score_interpretation_risk()`, $0, 3 signal categories (ambiguity cap 0.30, novelty cap 0.30, constraint-load cap 0.40), `HIGH_RISK_THRESHOLD=0.55`. `harness/orchestrator.py` — `make_orchestrator(difficulty=)`: `simple`→Haiku, `medium`→Codex-first, `complex`→Sonnet→Opus. `harness/creative_director.py` — routes by interpretation-risk score (high→Sonnet primary, very-high→Opus). `harness/config.py` — `CODEX_WORKER_RESERVE = max(0, CODEX_CLI_MAX_CALLS - CODEX_PLANNING_RESERVE)` hard sub-cap; `HAIKU_MODEL`. `harness/worker.py` — `_codex_worker_calls` counter enforced under `_oauth_lock`. `harness/main.py` — `_difficulty_from_brief()` + `_bump_difficulty()` wired to orchestrator. 124 tests | ✅ |
| **Worktree correctness hardening (PR #126, 2026-06-18):** four `WorktreeManager`/`scheduler` bugs — detached-HEAD no longer permanently detaches the repo after a merge; merge runs before the output-dir copy (git history and `output_dir` can't desync); `remove()` holds `_merge_lock`; stale worktree dirs are `git worktree prune`d. 30 worktree tests | ✅ |
| **Observe-only instrumentation across all surfaces (PR #128, 2026-06-18):** `permissions.observe()` wired at every side-effecting surface — `llm_cli` (codex/grok/claude), `fs_delete` (output-dir wipe), `install`/`build`/`test` (verification + e2e), `shell` (LLM-authored render-script exec), `render` (video/audio/music). `shell` reclassified low→**high** (arbitrary local code exec). Purely additive — observe never blocks. Completes roadmap #6's logging half across the whole pipeline | ✅ |
| **Risk-evidence aggregator (PR #130, 2026-06-18):** `harness/risk_evidence.py` — read-only tool that scans `sessions/*.jsonl`, aggregates `risk_classified` by `kind`/current-risk/mission, and reports logged-vs-current taxonomy **drift**. Re-derives risk via `classify_action` (current taxonomy) rather than trusting the logged value. CLI: `python risk_evidence.py [--json]`. Closes the read-back gap so enforcement thresholds can come from data | ✅ |
| **Enforcement gateway design — #6/#1 (2026-06-18, design-only):** `~/.claude/plans/enforcement-gateway-design.md` — `observe()`→`gate()` decision return (block = graceful skip, never raises); config-driven runtime policy; attended/unattended `RunContext` with Telegram-approval-or-fail-closed; mode×risk matrix; `auto_safe` unattended default; shadow-enforce rollout. Implementation gated on real-build evidence | 📐 designed |

---

## Current Status & What's Left to Finalize

**2026-06-18 (eleventh session) — action-risk safety layer advancing; PRs #10–#132 merged, no open PRs.** The Claude-Code-style upgrades roadmap is progressing through its safety track. **Roadmap #6 (action-risk) now logs across the entire pipeline:** the observe-only classifier (`permissions.py`) is wired at every side-effecting surface — LLM CLIs, package installs, the pre-run output-dir wipe, and the execution of LLM-authored render scripts (`shell`, reclassified to high) — via PR #128, and a read-back/aggregation tool (`risk_evidence.py`, PR #130) turns the durable `sessions/*.jsonl` evidence into per-kind/per-risk distributions. Roadmap #3 (git-worktree isolation per task) shipped (#122) and was correctness-hardened (#126). The **enforcement gateway + permission modes** (#6 enforcement half + #1) are **designed** (`~/.claude/plans/enforcement-gateway-design.md`) but deliberately **blocked on real-build evidence** — the next concrete step is running real builds so the instrumentation produces data to set thresholds from, then implementing the evidence-gated enforcement slice. Full session history in `SESSION_HANDOFF.md`.

**2026-06-17 (ninth session, continued) — Claude-Code-style upgrades roadmap approved + Milestone 1 shipped (PRs #114–#117); read-only session dashboard in review (#118).** A Codex-debated roadmap (recorded in PR #114) prioritized six Claude-Code-style capabilities; **Milestone 1 is complete**: an append-only replayable **session log** (`harness/session_log.py`, PR #115) and an **observe-only action-risk classifier** (`harness/permissions.py`, PR #116) that scores every side-effecting op by blast radius and logs it without blocking — enforcement is a deliberate later increment. PR #111 landed the deferred PR-#105 cleanups (a new `harness/llm_json.py` shared parser + Codex-tier dedup); #112/#113/#117 are doc syncs. **In review — PR #118:** a second, read-only **Claude Code Mission Control** dashboard (`cc_dashboard.py`, port 8766) that tails the live Claude Code session transcript (timeline, sub-agent fleet, token/context burn) alongside the existing build dashboard on 8765.

**2026-06-17 (ninth session) — CD validator hardening (PRs #103/#104) merged; orchestrator + dashboard work merged as PR #105 (squash `3b71f54`).** PR #103 hardened the `CreativeDirector` brief validator (malformed briefs now escalate instead of mis-routing the build); #104 synced the routing-review plan amendments. **PR #105** (this session): the orchestrator now latches Gemini off on a quota-class 429 and falls free-first through Codex→Sonnet→Opus via a generalized `CompositeOrchestrator` + `CodexOrchestrator`, with `reset_orchestrator_run()` wired into both run-start paths so the latch can't persist across runs; the in-session agent dashboard is now fully offline (Tailwind CDN removed, assets split into `css/`+`js/`, zero external refs) with per-LLM token totals; and the j-claw dashboard labels OAuth-tier models correctly (Grok fix) and tracks per-task tokens. Verified: **136 passed, 1 skipped** across all four relevant suites (including the post-review hardening fixes that shipped with the squash merge). **PR #107** then added an optional, live-validated **Claude Max CLI OAuth worker rung** (`claude_cli`) — same models as the metered API, billed against a Max subscription; ships **disabled by default** and ToS-gated. It was subsequently **activated in the operator's `harness/.env`** and proven engaging end-to-end via a forced-escalation run through the real ladder (reached `claude_cli`, valid contract, **zero metered spend**). Next: Phase 4 (interpretation-risk routing + per-role quotas) on its own branch.

**2026-06-16 (eighth session) — PRs #70–#82 merged; Codex OAuth worker rung landed + hardened + live-validated; all media backends smoke-tested green.** PR #72: orphaned-run reconciliation on bot startup (a restart can no longer freeze `EXECUTING`). PR #74: `*.log` gitignored (logs can contain the Telegram token). PR #75: 14 new tests for the media workers and mission-control state/dashboard. PR #76: `asset_worker.py` selects a realistic vs anime checkpoint from the brief (RealVisXL / Animagine) with per-style prompt modifiers and a robust checkpoint fallback chain, plus 12 pure-function tests. PR #78: style cue voting fix (film/cinematic no longer outvotes anime cues). **PRs #79 + #81: optional flat-rate `codex::gpt-5.5` OAuth worker rung** between local Ollama and Anthropic — escalations that would spend Anthropic dollars are caught for free against a ChatGPT subscription first; off by default, latches off cleanly if Codex is unavailable, $0 telemetry in the cost panel; 7 new mocked tests, hardened to **40 green** and **live-validated** (first real `codex exec` → valid JSON in ~9s). PR #80: corrected stale media-backend telemetry labels. **All six worker/media backends smoke-tested green this session** (real ComfyUI PNG, Piper WAV, FluidSynth WAV, ffmpeg render, Ollama qwen3/deepseek, live Codex). Next: factory rehearsal test #4 — film noir run (end-to-end exercise of the now-fully-local + style-aware film stack).

**2026-06-15 (seventh session) — PRs #55–#68 merged; film stack fully local; factory rehearsal 3/8 done.** PR #55: context-aware dashboard control buttons. PR #57: worker escalation learning loop. PR #60: Ollama token tracking in cost panel. PR #61 (critical): `_is_ollama_unavailable()` guard — unreachable Ollama fails immediately, no silent Sonnet escalation (saved $0.50 discovered live). PR #63: three worker quality rules — Tailwind CDN conditional, DOM event listener binding, contact form placeholder guard; + manifest icon existence check. PR #65: CANCELED state written to `mission_control.json` on `/cancel`. PR #67: ComfyUI DirectML backend (`--cpu` → `--directml` on AMD RX 9070 XT, SDXL async workflow). PR #68: Piper TTS narration + FluidSynth algorithmic music — film stack is now 100% local (no Coqui/MusicGen placeholders). Three factory rehearsal tests complete: #1 portfolio deploy ✅, #2 `/continue` fix flow ✅, #3 Tony Montana v8 clean run ✅. Next: factory rehearsal test #4 — film noir run.

**2026-06-15 (sixth session end) — PRs #10–#54 all merged; unique file ownership enforced; completeness.py stripping order fixed; ID/class coordination + JS toggle class rules enforced; .git rmtree PermissionError fixed.** The target: Telegram is the only human interface; builds queue and run unattended; finished web builds auto-deploy to a reachable URL; the operator is contacted only on terminal outcome. All machinery for that is merged and hardened.

The film-stack validation has driven **eleven** live runs across four sessions, each catching a real defect: seven on the Claude orchestrator (PRs #18–#23, #25) and four on the Gemini orchestrator (v3–v6 → PRs #34–#35, #39). The Gemini batch shares one root cause: **Claude infers intent; Gemini follows the prompt and schema literally** — every rule, enum, and schema must say exactly what it means.

**Gemini timeout fixed (PR #48).** Before: if Gemini stalled on a response, the harness froze indefinitely — no exception raised, CompositeOrchestrator Sonnet fallback never triggered. After: 300s timeout on the HTTP call → `APITimeoutError` treated as an availability failure → model fallback chain → Sonnet emergency fallback. The DAG-stage re-decomposition bug (PR #39) had already reduced orchestrator calls from 18–24 → ~6–8 per build.

### Honest capability scorecard

Rough confidence that an unattended run from a *detailed* prompt yields a finished, **working** deliverable (generation quality × verification honesty):

| Category | Confidence | Reality |
|---|---|---|
| 🟢 **Websites** (static / SPA / simple full-stack) | ~80% | Strong stacks + the one category with a real verification backbone (`npm`/`pip` build gates genuinely block). Closest to true one-shot. |
| 🟡 **Videogames** (Phaser / Three.js) | ~70% | Strong generation; gates now catch JS errors + dead canvas, but there is still no *gameplay* validation ("is it winnable"). |
| 🟡 **Apps / Dapps** | ~65% web | Web apps + web3 dapps are solid; desktop (Electron/Tauri) generates but verifies thinly; native mobile (Swift/Kotlin) **cannot be built/verified on Windows** — generate-only. |
| 🟡 **Movies** (film / video / music) | ~55% *(validation in progress)* | **ffmpeg/ffprobe installed; the render actually executes now.** Verification runs the project's render pipeline (`render.sh` via Git Bash, ffmpeg edit-script lines, or a Python entry) and a missing/stub video **fails** the build — no more hollow greens. The parent assembles per-scene clips into a frame-checked `final.mp4`. Seven live validation runs each removed a real defect (PRs #18–#23); the ceiling that remains is worker quality (the local model writing a correct 20s filtergraph vs a 1s one — heal escalation to Sonnet covers part of this). Final end-to-end validation pending API credits. |

### Issues surfaced by validation — now addressed on `main`

The 2026-06-04 validation run surfaced four issues. Three are **fixed and merged** (PR #5); the
fourth is structural and remains by design.

- ✅ **Escalation tax (FIXED):** the local worker no longer auto-escalates to paid Sonnet on
  script/binary tasks. Binary/image tasks route to `asset_worker` (valid PNG placeholder when SD
  is offline — no 404), and single-file script output is salvaged from a tolerant JSON parse
  before escalating. *(`b479e57`)*
- ✅ **Heal-loop non-convergence (FIXED):** `heal_metrics.py` now measures issue-set similarity
  round-over-round; the first non-converging signal escalates the fix round (stronger rung +
  sharper guidance), a second consecutive signal stops early instead of regressing. *(`056ad67`)*
- ✅ **OpenClaw bot (FIXED):** Haiku router, replies confirmed live — see the OpenClaw Integration
  section for the root cause (stale orphaned gateway) and the verify command.
- ⚠️ **Verification honesty depends on installed tooling (structural).** On a box missing
  `ffprobe`/`ffmpeg`/`mypy`/`ruff`/`bandit`/Playwright, those checks SKIP (now honestly marked
  `⊘ SKIPPED`) rather than gate — so "green" only means "verified" where the tools exist. This is
  intentional honesty, not a bug.

### ✅ Done since validation (merged to `main` via PR #5)

1. ~~Cut the escalation tax~~ — done (`b479e57`): binary/image → `asset_worker`, single-file script salvage.
2. ~~Fix the OpenClaw bot~~ — done (2026-06-04): Haiku router, replies confirmed live.
3. ~~Heal-loop convergence detection~~ — done (`056ad67`): `heal_metrics.py` similarity + escalate-then-stop.
4. ~~Phase 2 — movies pipeline~~ — done (`056ad67`): `generate_video` data-flow fix, real director→renderer prompts, music backend gate, honest frame/sync checks. *(Still needs a live render test — see below.)*
5. **Sprint A** — worker ladder (`qwen3:8b → qwen2.5-coder:14b → sonnet`; rung-1 upgraded to `deepseek-coder-v2:16b` 2026-06-12) + paid-call budget + dispatch timeouts + bounded heal loop. *(`ac3bdce`)*
6. **Pre-merge review fixes** — failure-handoff phase tracking made functional; worker-timeout liveness limitation documented. *(`7c7656e`)*

### Remaining work to finalize (priority order, updated 2026-06-13/14 end of fifth session)

~~DAG-stage decomposition guard~~ — **done** (PR #39).
~~Honor Gemini 429 retry delay~~ — **done** (PR #39).
~~Emergency cross-provider fallback~~ — **done** (PR #40).
~~LLM layer test suite~~ — **done** (PR #41, 25/25 tests green).
~~Dashboard telemetry + state wiring~~ — **done** (PRs #44, #47).
~~orchestrator.txt render + HTML rules~~ — **done** (PRs #45, #46).
~~Gemini timeout / APITimeoutError~~ — **done** (PR #48).
~~CDN stack unit-test guard~~ — **done** (PR #49).
~~One-file-per-task ≤150 line limit~~ — **done** (PR #50).
~~Unique file ownership + monolithic file ban~~ — **done** (PR #51).
~~completeness.py stripping order~~ — **done** (PR #52).
~~ID/class coordination + JS toggle class rules~~ — **done** (PR #53).
~~rmtree read-only .git objects~~ — **done** (PR #54).

1. **Tony Montana v8 validation** — restart from Telegram (v7 failed before any tasks ran due to `.git PermissionError`, fixed in PR #54). Confirm: CSS split across named files, sections carry both `id` and `class`, dark mode toggle depends on CSS task defining `html.light-mode {}`, build completes PASS.
2. **Factory rehearsal — items #2–7 (binding acceptance test):** from Telegram only —
   - ~~#1 website `/run`~~ — **done** (2026-06-13): Netlify URL deployed, Telegram push received ✅
   - **#2 `/continue` a feature** (dark mode toggle) — 🔄 in progress
   - **#3 `/run` a film** — aggregate push with per-scene clips + `final.mp4`
   - **#4 impossible intent** — honest FAIL push (no crash)
   - **#5 kill Ollama mid-build** — crash push, pipeline recovers
   - **#6 two queued builds** — strict FIFO, both complete and push
   - **#7 reboot + repeat** — no interactive auth anywhere
   All 7 green → "factory" status declared.
3. **Carry-overs (not blocking):** native mobile CI runner; Playwright runner task type in the DAG; IPFS/on-chain CI deploy hook; LemonSqueezy / Stripe Connect; worker-timeout hard bound; prune stale `worktree-agent-*` branches + dangling Ollama manifests.

~~Anthropic credits / Google key / NETLIFY_AUTH_TOKEN~~ — **all resolved 2026-06-12**.
~~Duration honesty gap~~ — **closed** (PR #25). ~~PRs #30–#48~~ — **all merged**.

---

## Known Limitations

- **Projects directory is gitignored** — generated output is local only.
- **Final code review requires `ANTHROPIC_API_KEY`** — without it, `REVIEW.md` and `HANDOFF.md` won't contain a real verdict.
- **claude CLI stamp is optional** — OpenClaw verdict in the dashboard only appears if `claude` is installed and on PATH.
- **ComfyUI/Piper/Ollama must be running** — the pipeline degrades gracefully (SVG/silent-WAV/OpenRouter fallbacks) but local services need to be up for full capability.
- **Full-stack projects split into sub-projects** — when the spec is "React + FastAPI", the orchestrator emits FORMAT 5 and the harness runs a `backend_api/` sub-project then a `frontend_react/` sub-project in sequence. Both land under `harness/projects/<slug>/`.
- **Windows Defender exclusion required** — Defender locks `.git/objects/` at write time as the harness does `git init` and commits inside `harness/projects/`. Without this exclusion every build crashes at the git commit step with `PermissionError: [WinError 5]`. Run once in admin PowerShell:
  ```powershell
  Add-MpPreference -ExclusionPath "C:\Users\Tyler\Desktop\Jarvis-Claw\harness\projects"
  ```
- **vitest must be installed globally** — QA tasks for web projects use `vitest run`; if vitest isn't on PATH the task fails all 4 retry attempts and burns heal cycles. Run once: `npm install -g vitest`

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

**Verification** (`verification.py`): Auto-detects ecosystem (Node, Python, FastAPI, React+Vite, Phaser, vanilla, web3, electron, socket-io, three-js, Godot). Runs appropriate checks. Security: bandit (Python) / npm audit (Node) — FAIL on HIGH/CRITICAL. Lighthouse: Playwright static server + headless Lighthouse — FAIL if perf < 0.5 or a11y < 0.7. Godot headless: `godot --headless --check-only`. HTML meta: WARN on missing lang, description, img alt. Expo: `npx expo export --platform web`. Validates PWA files (`manifest.json` + `sw.js`) for vanilla/react-vite projects. **Honesty (Phase 1):** a check that returns `True` only because its tool/runner is unavailable now begins its message with the `SKIP_PREFIX` sentinel, so the HANDOFF report can render it as `⊘ SKIPPED` instead of a verified pass. The Playwright project/game check fails on a zero-size canvas and observes a 1.5s window so loop-time runtime errors are caught, not just init errors.

**Self-healing loop** (`main.py`): After all tasks complete, runs the final Claude review **and** the dynamic checks (E2E + project Playwright) each cycle. The project only passes if review AND the dynamic gates pass — an E2E/Playwright failure now blocks the project and is injected into the issue list so the orchestrator generates fix tasks for it (previously the E2E result was computed and silently ignored). On `ISSUES FOUND`, calls orchestrator in `REVIEW_FAILED`, re-runs scheduler. Up to 2 cycles. **Convergence detection (`heal_metrics.py`):** issue sets are compared round-over-round (Jaccard similarity over normalized issue tokens); a `regressing`/`stalled` trend first escalates the fix round (stronger worker rung + a sharper "address the root cause, don't reintroduce removed frameworks" hint), and a second consecutive non-converging signal stops the loop early instead of burning the budget regressing.

---

## License

MIT
