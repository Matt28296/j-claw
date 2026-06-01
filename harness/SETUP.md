# J-Claw Harness — Setup Guide

## Requirements

- Windows 10/11
- Python 3.10+
- [Ollama](https://ollama.com/download/windows) (local worker)
- Anthropic API key (for automated orchestrator) — optional, see step 4

---

## 1. Clone the repo

```
git clone https://github.com/Matt28296/j-claw.git
cd j-claw
```

## 2. Install Ollama and pull the worker model

Download from https://ollama.com/download/windows and install.

Then pull the code worker model (choose one):

```
ollama pull qwen2.5-coder:7b      # fits comfortably in 8 GB VRAM
ollama pull qwen2.5-coder:14b     # better quality, fits in 8 GB at Q4
```

Verify:
```
ollama list
```

## 3. Python environment

Allow script execution (run once in PowerShell):
```
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Then create the virtual environment:
```
cd harness
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 4. Configure .env

```
copy .env.example .env
```

Edit `.env` with your settings:

| Variable               | Default                   | Description                                  |
|------------------------|---------------------------|----------------------------------------------|
| `ANTHROPIC_API_KEY`    | (required for auto mode)  | Your Anthropic API key                       |
| `ORCHESTRATOR_MODEL`   | `claude-sonnet-4-6`       | Claude model used for orchestration          |
| `WORKER_MODEL`         | `qwen2.5-coder:7b`        | Ollama model for code writing                |
| `PROJECTS_DIR`         | `./projects`              | Where generated project files are written    |
| `MAX_RETRIES_PER_TASK` | `3`                       | EXECUTION_ERROR retries before halting       |
| `MAX_FORMAT5_DEPTH`    | `3`                       | Max recursion depth for oversize sub-projects|
| `OLLAMA_HOST`          | `http://localhost:11434`  | Ollama API endpoint                          |

## 5. Run

From the **project root** (`j-claw/`), not from inside `harness/`:

**Automated mode** (requires API key):
```
.\run.bat
```

**Manual mode** (you act as the orchestrator — no API key needed):
```
.\run.bat --manual
```

The pipeline will prompt you to describe your project, then generate a spec for your approval before executing.

---

## How it works

```
User intent
    │
    ▼ INIT
  FORMAT 1 spec  ◄── SPEC_REVISION (if you reject)
    │ (accepted)
    ▼ SPEC_ACCEPTED
  FORMAT 2 DAG (up to 20 tasks)
    │
    ▼ execute tasks in topological order
    │   worker (Ollama) writes files
    │   verification runs (lint/build/test/manual)
    │   on failure → EXECUTION_ERROR → FORMAT 3 refinement → retry
    │
    ▼ PROJECT_REVIEW
  FORMAT 4 pass / needs_followup
    │ (pass)
    ▼ done — output in projects/<name>/
```

If any phase produces FORMAT 5 (oversize), the project splits into sequential
sub-projects each run as their own pipeline instance.

## Supported stacks

| Stack        | Use case                          | Requirements          |
|--------------|-----------------------------------|-----------------------|
| `vanilla`    | Static web apps, Tailwind CDN     | None (browser only)   |
| `fastapi`    | Python REST API + SQLite backend  | pip install           |
| `phaser`     | Browser games (Phaser 3 CDN)      | None (browser only)   |
| `react-vite` | React + Vite + Tailwind frontend  | Node.js + npm         |
