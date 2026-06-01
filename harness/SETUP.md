# J-Claw Harness — Setup Guide

## 1. Install Ollama

Download and install from https://ollama.com/download/windows  
After install, pull the worker model:

```
ollama pull qwen2.5-coder:7b
```

Verify it's running:
```
ollama list
```

## 2. Python environment

Requires Python 3.10+.

```
cd harness
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 3. API key

```
copy .env.example .env
```

Edit `.env` and set your Anthropic API key:
```
ANTHROPIC_API_KEY=sk-ant-...
```

## 4. Run

```
python main.py "A simple to-do web app"
```

Or pass intent as a flag:
```
python main.py "A CLI tool that renames files by date" -o ./projects/renamer
```

## Configuration

All options live in `.env`:

| Variable              | Default                  | Description                          |
|-----------------------|--------------------------|--------------------------------------|
| `ANTHROPIC_API_KEY`   | (required)               | Your Anthropic API key               |
| `WORKER_MODEL`        | `qwen2.5-coder:7b`       | Ollama model for code writing        |
| `PROJECTS_DIR`        | `./projects`             | Where generated project files go     |
| `MAX_RETRIES_PER_TASK`| `3`                      | EXECUTION_ERROR retries before halt  |
| `MAX_FORMAT5_DEPTH`   | `3`                      | Max recursion depth for sub-projects |
| `OLLAMA_HOST`         | `http://localhost:11434` | Ollama API endpoint                  |

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
  FORMAT 4 pass/needs_followup
    │ (pass)
    ▼ done — files in ./projects/<name>/
```

If any phase produces a FORMAT 5 (oversize), the project is split into sequential
sub-projects and each is run as its own pipeline.
