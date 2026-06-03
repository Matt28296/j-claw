# J-Claw — Local-First Autonomous Coding Pipeline

J-Claw is a self-contained agentic software production system. You describe a project in plain English; the pipeline plans it, writes all the code, verifies the output, reviews the result with Claude, self-heals if issues are found, and hands off a signed report — with no human in the loop beyond the initial intent.

It runs entirely on your local machine. The worker model is a local Ollama LLM. The orchestrator is Claude (via Anthropic or OpenRouter API), with a manual fallback mode that requires no API key.

---

## What it does

```
"Build a Phaser 3 snake game with score tracking"
            │
            ▼  INIT
    Orchestrator generates a project spec (FORMAT 1)
            │  (auto-accepted with --yes, or you review and revise)
            ▼  SPEC_ACCEPTED
    Orchestrator emits a task DAG (FORMAT 2) — up to 50 tasks
            │
            ▼  Execute tasks in topological order
            │   └─ Worker (Ollama) writes each file
            │   └─ Harness runs verification (lint / unit_test / build / html_auto)
            │   └─ On failure → EXECUTION_ERROR → Orchestrator rewrites task → retry
            │
            ▼  PROJECT_REVIEW
    Orchestrator inspects all outputs — pass or add follow-up tasks
            │
            ▼  Final Code Review (Claude API)
    Claude reads every output file and checks for stubs, broken imports,
    missing files, and obvious runtime errors
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
                ▼  Write HANDOFF.md
    Optional: invoke claude CLI for autonomous final stamp
            │
            ▼  Done — output files in harness/projects/<name>/
                       REVIEW.md, HANDOFF.md co-located with the project
```

Generated projects include: browser games (Phaser 3), React+Vite SPAs, FastAPI REST APIs, and vanilla HTML/JS apps.

---

## Architecture

```
j-claw/
├── orchestrator.txt          System prompt — the "brain" that plans and reviews
├── run.bat                   Entry point (Windows) — sets UTF-8, activates venv
├── dashboard.py              Mission Control dashboard server (port 8765)
├── dashboard/
│   └── index.html            Live pipeline dashboard (dark theme, auto-polling)
└── harness/
    ├── main.py               CLI + top-level pipeline loop + healing loop
    ├── orchestrator.py       Orchestrator (Claude/OpenRouter) + ManualOrchestrator
    ├── scheduler.py          DAG scheduler — runs tasks, handles errors, PROJECT_REVIEW
    ├── worker.py             Sends tasks to Ollama; validates and cleans JSON output
    ├── verification.py       Ecosystem detection + verification runners
    ├── final_review.py       Claude API code review — detects stubs, broken imports
    ├── handoff.py            Writes HANDOFF.md; optionally invokes claude CLI stamp
    ├── state_writer.py       Singleton event bus — writes mission_control.json
    ├── validator.py          JSON schema + DAG integrity checks for all formats
    ├── project.py            ProjectInstance and Task data classes
    ├── config.py             .env loading — models, paths, limits
    └── projects/             Generated project output (gitignored)
```

### Components

**Orchestrator (`orchestrator.py`)** — Three implementations behind the same interface:
- `Orchestrator`: calls Claude via the Anthropic API.
- `OpenRouterOrchestrator`: calls any model via OpenRouter (`openrouter/auto` by default). Supports cascading fallback models when the primary is rate-limited.
- `ManualOrchestrator`: writes `orchestrator_input.json`, waits for you to fill `orchestrator_response.json`, and continues. No API key needed.

**Worker (`worker.py`)** — Sends a task to the local Ollama model and gets back `{files: [{path, content}]}`. Applies stack-specific prompt instructions (vanilla JS, React+Vite, FastAPI, Phaser). Detects truncated or suspiciously short output. Fixes literal `\n` sequences the model sometimes emits instead of real newlines.

**Scheduler (`scheduler.py`)** — Executes the task DAG in topological order. On verification failure, sends `EXECUTION_ERROR` to the orchestrator for a refined task (modify / split / deprecate), then retries up to `MAX_RETRIES_PER_TASK`. After all tasks complete, calls `PROJECT_REVIEW` and applies any follow-up tasks. Emits state events to the state writer at every stage.

**Verification (`verification.py`)** — Detects the project ecosystem (Node, Python, FastAPI, React+Vite, Phaser, vanilla) and runs the appropriate check. Bare HTML projects (only `.html/.css/.js` files, no build step) are verified with a headless structure check — no manual gate, no browser required. Phaser projects without `package.json` use the same headless check. If `package.json` exists, npm scripts run normally.

**Final Review (`final_review.py`)** — After PROJECT_REVIEW passes, sends all output files to Claude for a code quality gate. Checks for stub placeholders, hollow functions, broken imports, and missing files. Writes `REVIEW.md` with a structured `VERDICT: PASS / ISSUES FOUND` response. Returns `True` (pass) or `False` (issues found) to drive the healing loop.

**Healing Loop (`main.py`)** — When the final review returns `ISSUES FOUND`, parses the `ISSUES:` bullet list from `REVIEW.md`, calls the orchestrator in `REVIEW_FAILED` state with the issue list, gets targeted fix tasks, and re-runs the scheduler. Repeats up to 2 cycles before giving up. Healing cycles are tracked in `HANDOFF.md`.

**Handoff (`handoff.py`)** — Always runs at pipeline end (pass or fail). Writes `HANDOFF.md` to the project output directory with: status, heal cycles used, final review verdict, test results, and instructions for manual follow-up. If the `claude` CLI is on PATH, runs `claude --print` autonomously for a final quality stamp and appends it to `HANDOFF.md` as `## Claude Code Verdict`.

**State Writer (`state_writer.py`)** — Singleton event bus. Every pipeline event (task start/done/fail, agent calls, file writes, verification results) calls a hook that updates `mission_control.json` at the repo root. The dashboard polls this file every second.

**Validator (`validator.py`)** — Validates every orchestrator response against its JSON schema before acting. Also checks DAG integrity: no duplicate IDs, no missing dependency references, no cycles, no two tasks writing the same file without a dependency edge.

---

## Mission Control Dashboard

```
python dashboard.py
```

Opens `http://localhost:8765/dashboard/index.html` in your browser. Leave it running while the pipeline executes — it updates every second.

```
┌─────────────────────── J-CLAW MISSION CONTROL ─────────────────────────┐
│  [● EXECUTING]  A Phaser 3 snake game…   elapsed: 2m 14s   [📋 Copy Logs]│
├─────────────────────────────────────────────────────────────────────────┤
│  REVIEW BANNER (green: PASS / red: ISSUES FOUND — appears when done)   │
│  OPENCLAW BANNER (purple: APPROVED / amber: issues — if claude CLI used) │
├──────────────────────────────┬──────────────────────────────────────────┤
│  ACTIVE AGENT                │  EVENTS (live feed)                      │
│  orchestrator · INIT · 4s    │  ✓ task-003 done [qwen2.5-coder:14b]    │
│                              │  📄 js/snake.js                          │
│  TASKS                       │  ▶ task-003 started                      │
│  ● task-001  ✓ done          ├──────────────────────────────────────────┤
│  ● task-002  ✓ done          │  TEST RESULTS                            │
│  ▶ task-003  ↻ running       │  ✓ build/node  task-003  PASS            │
│  ○ task-004  pending         ├──────────────────────────────────────────┤
│                              │  WORK LOG                                │
│                              │  ORCH  DAG  Planned 4 task(s)            │
│                              │  WORKER  task-001  index.html written    │
└──────────────────────────────┴──────────────────────────────────────────┘
```

**Panels:**
- **Active Agent** — which agent is calling which API, with a live elapsed timer
- **Tasks** — one card per task, color-coded by status (pending / running / done / failed), retry badge, file pills
- **Events** — live feed of every pipeline event
- **Test Results** — per-task verification results with method + ecosystem badges; Playwright and pytest log output is colorized (✓ green / ✗ red)
- **Work Log** — chronological record of what each agent did: ORCH (orchestrator, purple) vs WORKER (purple/blue), model name, action, detail

**Copy Logs button** — copies a structured plain-text snapshot of all pipeline data to the clipboard for pasting into chat or a bug report.

---

## State Machine & Message Formats

| State | Format | Description |
|-------|--------|-------------|
| `INIT` | FORMAT 1 | Project spec: type, complexity, goal, features, constraints, architecture, modules |
| `SPEC_REVISION` | FORMAT 1 | Re-emits spec with `revision_feedback` applied |
| `SPEC_ACCEPTED` | FORMAT 2 | Full task DAG — up to 50 tasks with dependencies, files, acceptance criteria, verification type |
| `EXECUTION_ERROR` | FORMAT 3 | Fix for a failed task: `modify`, `split`, or `deprecate` |
| `PROJECT_REVIEW` | FORMAT 4 | Final orchestrator verdict: `pass` or `needs_followup` with additional tasks |
| `REVIEW_FAILED` | FORMAT 4 | Self-healing: orchestrator receives the Claude review's issue list and returns fix tasks |

FORMAT 5 (oversize) is available from INIT/SPEC_ACCEPTED: if a project is too large, the orchestrator emits a sub-project graph and the harness runs each sub-project as its own pipeline instance in topological order.

### FORMAT 1 — Project Spec

```json
{
  "project_type": "web | app | game",
  "complexity": "low | medium",
  "goal": "One-sentence description",
  "features": ["Feature A", "Feature B"],
  "constraints": ["No external APIs", "SQLite only"],
  "architecture": {
    "frontend": "...", "backend": "...", "database": "...", "deployment": "..."
  },
  "modules": [
    { "name": "auth", "responsibility": "JWT login and session management" }
  ]
}
```

### FORMAT 2 — Task DAG

```json
{
  "tasks": [
    {
      "id": "task-001",
      "type": "frontend",
      "objective": "Write index.html containing the complete counter page...",
      "files": ["index.html"],
      "dependencies": [],
      "priority": "high",
      "acceptance_criteria": ["button increments counter", "count displayed on screen"],
      "verification": "none"
    }
  ]
}
```

Verification options: `lint` `unit_test` `build` `smoke` `none`
(`manual` is accepted but automatically redirected to headless HTML check for static projects)

### FORMAT 3 — Execution Error Refinement

```json
{
  "refinement_target_task_id": "task-003",
  "reason_for_refinement": "Worker produced stub — wrote '// Implementation unchanged'",
  "action": "modify",
  "updated_tasks": [{ "...revised task..." : "..." }]
}
```

Actions: `modify` (rewrite), `split` (decompose into subtasks), `deprecate` (mark done and skip)

### FORMAT 4 — Project Review / Review Failed

```json
{
  "review_result": "needs_followup",
  "summary": "qa_check.py checks for id='count-display' but HTML uses id='count'",
  "followup_tasks": [{ "...fix task..." : "..." }]
}
```

`review_result` is either `pass` or `needs_followup`. The same schema is used for both `PROJECT_REVIEW` and `REVIEW_FAILED` states.

---

## DAG Rules

The validator enforces these rules on every FORMAT 2, FORMAT 3 split, and FORMAT 4 follow-up:

- No duplicate task IDs within a project instance
- Every dependency reference must point to an existing task ID
- No cycles
- No two tasks write the same file unless one depends (directly or transitively) on the other
- Total Active DAG size never exceeds 50 tasks

---

## Verification Matrix

| Ecosystem | Detected by | Verification commands |
|-----------|-------------|----------------------|
| `vanilla` | no package.json, no game.js | headless HTML structure check (no gate) |
| `phaser` | game.js present, no package.json | headless HTML structure check (no gate) |
| `phaser` (with npm) | game.js + package.json | npm scripts |
| `node` | package.json | npm run lint, npm test, npm install |
| `react-vite` | vite.config.js/ts | npm install && npm run build |
| `python` | requirements.txt / pyproject.toml | pytest |
| `fastapi` | requirements.txt with fastapi | pip install, pytest |

The headless HTML check confirms `<html>` and `<body>` tags are present in all `.html` files. It replaces the old manual yes/no gate, allowing the pipeline to run fully unattended for all static web projects.

---

## Hardware Context

J-Claw is designed around running a 13–14B 4-bit quantized coding model locally on a GPU. The orchestrator prompt encodes these constraints so the orchestrator never plans work the worker can't handle:

- Tasks are atomic: 1–3 files each
- Objectives are self-contained — the worker sees one task at a time
- Architecture is kept flat; no sprawling abstractions
- Upper model bound: ~14B 4-bit (fits in 8–16 GB VRAM)

Default worker: `qwen2.5-coder:7b` (configurable via `WORKER_MODEL` in `.env`). Tested with `qwen2.5-coder:14b`.

---

## Setup

### Requirements

- Windows 10/11
- Python 3.10+
- [Ollama](https://ollama.com/download/windows)
- Anthropic or OpenRouter API key *(optional — only needed for automated orchestrator mode)*

### Install

```powershell
git clone https://github.com/Matt28296/j-claw.git
cd j-claw\harness

# Allow script execution (once)
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Pull a worker model

```
ollama pull qwen2.5-coder:7b    # comfortable on 8 GB VRAM
ollama pull qwen2.5-coder:14b   # 8–16 GB VRAM at Q4
```

### Configure

```
copy harness\.env.example harness\.env
```

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for auto orchestrator mode (and final code review) |
| `OPENROUTER_API_KEY` | — | Alternative to Anthropic — set `ORCHESTRATOR_PROVIDER=openrouter` |
| `WORKER_MODEL` | `qwen2.5-coder:7b` | Ollama model for code writing |
| `ORCHESTRATOR_MODEL` | `claude-sonnet-4-6` | Claude model for orchestration and final review |
| `ORCHESTRATOR_PROVIDER` | `anthropic` | `anthropic` or `openrouter` |
| `ORCHESTRATOR_API_MODEL` | `openrouter/auto` | Model string passed to OpenRouter |
| `PROJECTS_DIR` | `./projects` | Output directory for generated projects |
| `MAX_RETRIES_PER_TASK` | `3` | EXECUTION_ERROR retries before halting |
| `MAX_FORMAT5_DEPTH` | `3` | Max recursion depth for sub-projects |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint |

---

## Usage

From the repo root (`j-claw/`):

**Run with auto-accept (fully zero-touch):**
```
.\run.bat --yes "A Phaser 3 snake game with score tracking and a game over screen"
```

**Interactive mode (review and accept spec before execution):**
```
.\run.bat "A single-page todo app"
```

**Manual orchestrator mode (no API key required):**
```
.\run.bat --manual
```

In manual mode, the harness writes `harness/orchestrator_input.json` at each pipeline state, waits for you to fill `harness/orchestrator_response.json`, and continues when you press Enter.

**Mission Control dashboard (open in a second terminal):**
```
python dashboard.py
```
Then open `http://localhost:8765/dashboard/index.html`.

**With a specific output directory:**
```
.\run.bat --yes "A counter app" --output .\harness\projects\my-counter
```

---

## Pipeline Output

For every project, the pipeline writes to `harness/projects/<slug>/`:

| File | Description |
|------|-------------|
| `index.html`, `*.js`, `*.py`, … | Generated source files |
| `REVIEW.md` | Final code review verdict — `VERDICT: PASS` or `VERDICT: ISSUES FOUND` with issue list |
| `HANDOFF.md` | Pipeline completion report — status, heal cycles, test results, instructions for follow-up |

If the `claude` CLI is on your PATH, `HANDOFF.md` will also contain a `## Claude Code Verdict` section written by an autonomous Claude Code session reviewing the final output.

---

## Supported Stacks

| Stack | Use case | Build requirement |
|---|---|---|
| `vanilla` | Static HTML/JS apps, no build step | None |
| `phaser` | Browser games (Phaser 3 CDN) | None |
| `fastapi` | Python REST API + SQLite | `pip install` |
| `react-vite` | React + Vite + Tailwind SPA | Node.js + npm |

The stack is set in the FORMAT 1 spec's `architecture` section. The worker receives stack-specific instructions (e.g. `window.*` globals for Phaser, no ES modules for vanilla, `yield` in FastAPI dependencies).

---

## Orchestrator Prompt

`orchestrator.txt` is the system prompt loaded at runtime. Edit it to change how the orchestrator plans projects, decomposes tasks, or handles errors. Key sections:

- **Hardware context** — model size limits, VRAM ceiling, concurrency rules
- **State machine** — which format to emit for each `system_state`
- **Format schemas** — explicit field-by-field spec for each format
- **Task writing rules** — atomic tasks, 1–3 files, no stubs, complete file content
- **Verification rules** — never assign `manual` for static projects; use `none` instead
- **REVIEW_FAILED handling** — how to produce targeted fix tasks from a list of review issues
- **Anti-patterns** — common failure modes to avoid

---

## What Was Built (Session Log)

This section documents the major features added in the most recent development push, in build order.

### 1. Windows Unicode Fix
`run.bat` sets `PYTHONUTF8=1` and runs `chcp 65001`. Without this, Rich's `LegacyWindowsTerm` used Windows codepage 1252 and crashed on `▶` and `✓` characters when running in a subprocess.

### 2. Zero-Touch Verification for Static Projects
`verification.py` gained `_is_bare_html_task()` and `_run_html_auto()`. Any task whose files are all static web types (`.html`, `.css`, `.js`, etc.) now bypasses the manual yes/no gate entirely and is verified with a headless check for `<html>` and `<body>` tags. The package.json presence check was deliberately removed — a task that only writes static files is static by definition, and stale package.json files from previous runs were causing false negatives.

Phaser projects without `package.json` also use the headless check instead of falling through to the manual gate.

### 3. Mission Control Dashboard
`dashboard.py` is a Python `http.server` that serves the repo root on port 8765 and auto-opens the browser. `dashboard/index.html` is the live dashboard:

- Polls `mission_control.json` every second
- Active agent card with live elapsed timer
- Task cards (color-coded, retry badge, file pills)
- Streaming event log
- Test results panel with colorized Playwright/pytest log parsing
- Work log panel distinguishing orchestrator (ORCH) vs worker actions
- Review banner (green PASS / red ISSUES FOUND) fetched from REVIEW.md on completion
- OpenClaw banner (purple) fetched from HANDOFF.md when claude CLI verdict is present
- Copy Logs button — formats all state as plain text and writes to clipboard

### 4. State Writer Hooks
`state_writer.py` was wired into `scheduler.py` at every execution point: task start, task done, task failed, file written, verification result. Added `work_log[]` and `test_results[]` to the state. Fixed `output_url` path resolution so the dashboard can locate project files relative to the server root.

### 5. Final Code Review
`final_review.py` calls Claude with all project output files after PROJECT_REVIEW passes. It checks for stubs, hollow functions, broken imports, and missing files. Writes `REVIEW.md` with a structured verdict. Returns a boolean that drives the healing loop.

### 6. Self-Healing Loop
When `final_review` returns `ISSUES FOUND`, `main.py` parses the `ISSUES:` bullet points from `REVIEW.md` and calls the orchestrator in `REVIEW_FAILED` state with the issue list. The orchestrator returns targeted fix tasks. The scheduler re-runs. The review runs again. Up to 2 heal cycles before giving up. `REVIEW_FAILED` was added to `validator.py` and `orchestrator.txt` as a first-class pipeline state.

### 7. HANDOFF.md + OpenClaw Stamp
`handoff.py` always runs at pipeline end. It writes `HANDOFF.md` with the run summary, test results (pulled from `mission_control.json`), review verdict, and heal cycle count. If `claude` is on PATH, it invokes `claude --print` in the project directory with a quality-check prompt. The verdict (`OPENCLAW: APPROVED` or `OPENCLAW: ISSUES FOUND`) is appended to `HANDOFF.md` and shown in the dashboard's OpenClaw banner.

### 8. Self-Healing Loop — Battle Tested
Ran a Phaser 3 snake game end-to-end. The final code review caught a real bug (path mismatch: `index.html` referenced `GameScene.js` at root but task-003 wrote it to `js/GameScene.js`). The healing loop fired automatically, generated 3 fix tasks, the worker rewrote the files, and the review passed on the second cycle. The game shipped zero-touch.

Discovered and fixed three bugs during this test:
- Heal-cycle fix tasks were invisible in the dashboard (added `on_tasks_added()` hook to state_writer, called from main.py)
- Orchestrator's fix tasks conflicted with the DAG validator (added rule: fix tasks must declare the original task as a dependency when rewriting an existing file)
- Fix tasks were moving files to new paths instead of fixing the referencing file (added orchestrator rule: always fix in place, never change a file's path)

### 9. Output Directory Cleanup
`run_project()` now wipes the output directory at the start of every run via `shutil.rmtree`. Without this, stale files from a previous run on the same slug contaminated the new run's final review — a re-run of the snake game left `main.js` and root-level `GameScene.js` from the first run, causing the review to flag them as orphan files even though the new run never wrote them.

### 10. HANDOFF.md Excluded from Review
Added `HANDOFF.md` to `final_review.py`'s `_SKIP_FILES` set so the review never reads a previous run's handoff report as a project source file.

---

## Roadmap

### Completed

| Item | Status |
|------|--------|
| Eliminate manual verification gate for HTML/Phaser projects | ✓ Done — headless HTML structure check |
| Mission Control live dashboard | ✓ Done |
| Test results panel in dashboard | ✓ Done |
| Work log panel in dashboard | ✓ Done |
| Copy Logs button in dashboard | ✓ Done |
| Final code review (Claude API) after pipeline completes | ✓ Done |
| Self-healing loop — auto-fix issues flagged by final review | ✓ Done |
| Self-healing loop — battle tested on Phaser 3 snake game | ✓ Done — loop caught real path mismatch, fixed and passed |
| Heal-cycle fix tasks visible in dashboard | ✓ Done — on_tasks_added() hook |
| DAG conflict prevention for heal tasks | ✓ Done — orchestrator prompt rules |
| Output directory cleanup before each run | ✓ Done — shutil.rmtree at run start |
| HANDOFF.md excluded from final code review | ✓ Done |
| HANDOFF.md pipeline completion report | ✓ Done |
| OpenClaw autonomous stamp via claude CLI | ✓ Done (requires claude CLI on PATH) |
| OpenRouter orchestrator support with fallback models | ✓ Done |
| Windows UTF-8 encoding fix | ✓ Done |

### Open

| # | Issue | Impact |
|---|-------|--------|
| — | **Dashboard CSS redesign** — current styling is functional but plain; needs pipeline stage tracker, task progress bar, heal cycle badge, and a visual refresh | Dashboard is harder to read at a glance during long runs |
| — | **Playwright browser testing for game projects** — headless HTML check confirms file structure but cannot run the game loop, check canvas rendering, or detect runtime JS errors | Full correctness testing of Phaser/vanilla games requires a real browser |
| — | **Stub detection pre-check** — add a post-write scanner in `scheduler.py` that checks for known stub patterns before verification runs; fail the task immediately instead of waiting for the final review to catch it | Stub output wastes a full review cycle before being caught |
| — | **Parallel task execution** — `MAX_PARALLEL_WORKERS` is configurable but the scheduler runs tasks sequentially; independent DAG branches could run concurrently | Pipeline is slower than necessary on multi-task DAGs with no inter-task dependencies |
| — | **OpenClaw stamp without CLI** — `try_claude_stamp` silently skips if `claude` is not on PATH; a direct Anthropic API fallback would give all users the autonomous verdict | Final quality stamp is optional rather than guaranteed |

### Priority Order

1. **Dashboard redesign** — visual refresh + stage tracker + progress bar + heal badge makes the dashboard actually informative at a glance.
2. **Playwright integration** — add headless Chromium check for game projects; the last quality gap for zero-touch game verification.
3. **Stub detection** — post-write scan catches hollow output before it wastes a review cycle.
4. **Parallel scheduler** — wire `MAX_PARALLEL_WORKERS` into actual concurrent dispatch; 30–50% wall time reduction on larger DAGs.

---

## Known Limitations

- **Projects directory is gitignored** — generated output is local only and not committed to this repo.
- **Final code review requires `ANTHROPIC_API_KEY`** — if the key is not set, `final_review.py` auto-passes and neither `REVIEW.md` nor `HANDOFF.md` will contain a real verdict.
- **claude CLI stamp is optional** — OpenClaw verdict in the dashboard only appears if `claude` is installed and on PATH.

---

## License

MIT
