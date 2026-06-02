# J-Claw — Local-First Autonomous Coding Pipeline

J-Claw is a self-contained agentic software production system. You describe a project in plain English; the pipeline plans it, writes all the code, verifies the output, and fixes its own mistakes — with no human in the loop beyond the initial intent.

It runs entirely on your local machine. The worker model is a local Ollama LLM. The orchestrator is either Claude (via API) or you acting as orchestrator yourself (no API key required in manual mode).

---

## What it does

```
"Build a todo app with FastAPI and SQLite"
            │
            ▼  INIT
    Orchestrator generates a project spec (FORMAT 1)
            │  (you review and accept, or give revision feedback)
            ▼  SPEC_ACCEPTED
    Orchestrator emits a task DAG (FORMAT 2) — up to 30 tasks
            │
            ▼  Execute tasks in topological order
            │   └─ Worker (Ollama) writes each file
            │   └─ Harness runs verification (lint / unit test / build / smoke / manual)
            │   └─ On failure → EXECUTION_ERROR → Orchestrator rewrites task → retry
            │
            ▼  PROJECT_REVIEW
    Orchestrator inspects all outputs — pass or add follow-up tasks
            │
            ▼  Done — output files in projects/<name>/
```

Generated projects have been: browser games (Phaser 3), React+Vite SPAs, FastAPI REST APIs, and vanilla HTML/JS apps.

---

## Architecture

```
j-claw/
├── orchestrator.txt        System prompt — the "brain" that decides what to build and how
├── run.bat                 Entry point (Windows)
└── harness/
    ├── main.py             CLI + top-level pipeline loop
    ├── orchestrator.py     Orchestrator class (Claude API) and ManualOrchestrator
    ├── scheduler.py        DAG scheduler — runs tasks, handles errors, calls PROJECT_REVIEW
    ├── worker.py           Sends tasks to Ollama, validates JSON output, fixes literal \n
    ├── verification.py     Runs lint / unit_test / build / smoke per ecosystem
    ├── validator.py        JSON schema + DAG integrity checks for all orchestrator formats
    ├── project.py          ProjectInstance and Task data classes
    ├── config.py           .env loading — models, paths, limits
    └── projects/           Generated project output (gitignored)
```

### Components

**Orchestrator (`orchestrator.py`)** — Two implementations behind the same interface:
- `Orchestrator`: calls Claude via the Anthropic API. Uses the system prompt in `orchestrator.txt`.
- `ManualOrchestrator`: writes `orchestrator_input.json`, waits for you to fill `orchestrator_response.json`, validates it, and continues. No API key needed.

**Worker (`worker.py`)** — Sends a task to the local Ollama model and gets back a JSON blob of `{files: [{path, content}]}`. Applies stack-specific prompt instructions (vanilla JS, React+Vite, FastAPI, Phaser). Detects and warns about truncated or suspiciously short output. Fixes literal `\n` sequences the model sometimes emits instead of real newlines.

**Scheduler (`scheduler.py`)** — Executes the task DAG in topological order. On verification failure, sends an `EXECUTION_ERROR` to the orchestrator to get a refined task (modify / split / deprecate), then retries. After all tasks complete, calls `PROJECT_REVIEW` and applies any follow-up tasks the orchestrator adds.

**Validator (`validator.py`)** — Validates every orchestrator response against its JSON schema before the pipeline acts on it. Also checks DAG integrity: no duplicate IDs, no missing dependency references, no cycles, no two tasks writing the same file without a dependency edge between them.

**Verification (`verification.py`)** — Detects the project ecosystem (Node, Python, FastAPI, React+Vite, Phaser) and runs the appropriate commands: `npm test`, `pytest`, `npm run build`, `pip install`, etc. Falls back to a manual yes/no gate for Phaser games and unknown types.

---

## State Machine & Message Formats

The orchestrator communicates through five typed JSON formats, one per pipeline state:

| State | Format | Description |
|-------|--------|-------------|
| `INIT` | FORMAT 1 | Project spec: type, complexity, goal, features, constraints, architecture, modules |
| `SPEC_REVISION` | FORMAT 1 | Re-emits spec with `revision_feedback` applied |
| `SPEC_ACCEPTED` | FORMAT 2 | Full task DAG — list of up to 30 tasks with dependencies, files, acceptance criteria, verification type |
| `EXECUTION_ERROR` | FORMAT 3 | Fix for a failed task: `modify` (rewrite), `split` (decompose into subtasks), or `deprecate` (skip) |
| `PROJECT_REVIEW` | FORMAT 4 | Final verdict: `pass` or `needs_followup` with additional tasks |

FORMAT 5 (oversize) is an escape hatch available from INIT/SPEC_ACCEPTED: if the project is too large for the task budget, the orchestrator emits a sub-project graph and the harness runs each sub-project as its own pipeline instance in topological order.

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
      "type": "code",
      "objective": "Write js/physics.js — resolvePhysics(char, input, delta) with...",
      "files": ["js/physics.js"],
      "dependencies": [],
      "priority": "high",
      "acceptance_criteria": ["window.resolvePhysics is exported", "gravity applied"],
      "verification": "unit_test"
    }
  ]
}
```

Verification options: `lint` `unit_test` `build` `smoke` `manual` `none`

### FORMAT 3 — Execution Error Refinement

```json
{
  "refinement_target_task_id": "task-003",
  "reason_for_refinement": "Worker produced stub — wrote '// Implementation unchanged'",
  "action": "modify",
  "updated_tasks": [{ ...revised task... }]
}
```

Actions: `modify` (rewrite one task), `split` (decompose into multiple), `deprecate` (mark done and skip)

### FORMAT 4 — Project Review

```json
{
  "review_result": "needs_followup",
  "summary": "unit.js tests 1-4 are placeholder stubs — no assertions run",
  "followup_tasks": [{ ...new tasks... }]
}
```

---

## DAG Rules

The validator enforces these rules on every FORMAT 2, FORMAT 3 split, and FORMAT 4 follow-up:

- No duplicate task IDs within a project instance
- Every dependency reference must point to an existing task ID
- No cycles
- No two tasks write the same file unless one depends (directly or transitively) on the other
- Total Active DAG size never exceeds 30 tasks

---

## Hardware Context

J-Claw is designed around running a 13–14B 4-bit quantized coding model locally on a GPU. The orchestrator prompt (`orchestrator.txt`) encodes these constraints so the orchestrator never plans work the worker can't handle:

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
- Anthropic API key *(optional — only needed for automated orchestrator mode)*

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
ollama pull qwen2.5-coder:7b    # 8 GB VRAM comfortable
ollama pull qwen2.5-coder:14b   # 8–16 GB VRAM at Q4
```

### Configure

```
copy harness\.env.example harness\.env
```

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required only for auto orchestrator mode |
| `WORKER_MODEL` | `qwen2.5-coder:7b` | Ollama model for code writing |
| `ORCHESTRATOR_MODEL` | `claude-sonnet-4-6` | Claude model for orchestration |
| `PROJECTS_DIR` | `./projects` | Output directory for generated projects |
| `MAX_RETRIES_PER_TASK` | `3` | EXECUTION_ERROR retries before halting |
| `MAX_FORMAT5_DEPTH` | `3` | Max recursion depth for sub-projects |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint |

---

## Usage

From the repo root (`j-claw/`):

**Automated mode** (Claude orchestrates autonomously):
```
.\run.bat
```

**Manual mode** (you act as the orchestrator — no API key):
```
.\run.bat --manual
```

**With a specific output directory:**
```
.\run.bat --output .\harness\projects\my-app
```

In manual mode, the harness writes `harness/orchestrator_input.json` at each pipeline state, waits for you to fill `harness/orchestrator_response.json`, and continues when you press Enter. This lets you run the full pipeline on any machine with just Ollama installed.

---

## Supported Stacks

| Stack | Use case | Build requirement |
|---|---|---|
| `vanilla` | Static HTML/JS apps with Tailwind CDN | None |
| `phaser` | Browser games (Phaser 3 CDN) | None |
| `fastapi` | Python REST API + SQLite | `pip install` |
| `react-vite` | React + Vite + Tailwind SPA | Node.js + npm |

The stack is set in the FORMAT 1 spec's `architecture` section. The worker receives stack-specific instructions so it uses the correct idioms (e.g. `window.*` globals for Phaser, no ES modules for vanilla, `yield` in FastAPI dependencies).

---

## Orchestrator Prompt

`orchestrator.txt` is the system prompt that defines the orchestrator's behavior. It is loaded at runtime — edit it to change how the orchestrator plans projects, decomposes tasks, or handles errors. Key sections:

- **Hardware context** — model size limits, VRAM ceiling, concurrency rules
- **State machine** — which format to emit for each `system_state`
- **Format schemas** — explicit field-by-field spec for each format
- **Task writing rules** — atomic tasks, 1–3 files, no stubs, complete file content
- **Anti-patterns** — common failure modes to avoid (stub output, overly abstract architectures, circular dependencies)

---

## Known Limitations

- **14B models produce stubs when asked to "keep existing logic"** — The worker will replace full implementations with `// Existing draw logic` placeholder comments if the objective says to preserve parts of a file. Fix: always write the complete new file content in the objective. Never say "keep existing."
- **Phaser verification is manual** — Browser game correctness can't be checked automatically without a headless browser. A Playwright integration would close this gap.
- **Projects directory is gitignored** — Generated output is local only and not committed to this repo.

---

## Roadmap

These are the open issues blocking fully autonomous, unattended operation. See [GitHub Issues](https://github.com/Matt28296/j-claw/issues) for full details.

| # | Issue | Impact |
|---|-------|--------|
| [#1](https://github.com/Matt28296/j-claw/issues/1) | **unit.js tests 1–4 are placeholder stubs** — suite exits 0 even when code is broken | Broken code ships as "passing" through `unit_test` verification |
| [#2](https://github.com/Matt28296/j-claw/issues/2) | **smoke.js does not detect stub placeholder comments** | `// Existing draw logic` stubs pass smoke; silent failure undetected until manual play |
| [#3](https://github.com/Matt28296/j-claw/issues/3) | **Phaser verification falls back to manual gate** — pauses pipeline for human input | Game projects cannot run unattended; every task requires human browser check |
| [#4](https://github.com/Matt28296/j-claw/issues/4) | **Worker model produces stubs when modifying existing files** | Root cause of repeated hollow output; needs orchestrator rule + validator enforcement |

### Priority order

1. **Issues #1 + #2** (one pipeline run, 2 tasks) — fix unit tests and add stub detection. After this, the QA layer catches hollow output automatically and routes it back as `EXECUTION_ERROR`.
2. **Issue #4** (orchestrator rule + validator warning) — prevents the problem at the source, before stubs are even written.
3. **Issue #3** (Playwright integration) — eliminates the last manual gate and makes game projects fully autonomous.

Once all four are resolved, the pipeline can build, verify, detect failures, and self-correct for all supported stacks without any human intervention.

---

## License

MIT
