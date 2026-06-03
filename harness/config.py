from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ORCHESTRATOR_MODEL: str = "claude-sonnet-4-6"
ORCHESTRATOR_PROVIDER: str = os.getenv("ORCHESTRATOR_PROVIDER", "anthropic")  # "anthropic" | "openrouter"
ORCHESTRATOR_API_MODEL: str = os.getenv("ORCHESTRATOR_API_MODEL", "openrouter/auto")

# Comma-separated fallback models tried in order when the primary is rate-limited
# e.g. "nvidia/nemotron-3-super-120b-a12b:free,meta-llama/llama-3.3-70b-instruct:free"
ORCHESTRATOR_FALLBACK_MODELS: list[str] = [
    m.strip() for m in os.getenv("ORCHESTRATOR_FALLBACKS", "nvidia/nemotron-3-super-120b-a12b:free,meta-llama/llama-3.3-70b-instruct:free").split(",") if m.strip()
]

WORKER_PROVIDER: str = os.getenv("WORKER_PROVIDER", "ollama")  # "ollama" | "groq" | "openrouter"
WORKER_MODEL: str = os.getenv("WORKER_MODEL", "qwen2.5-coder:7b")

# Fallback worker providers tried in order on rate limit / error.
# Format: "provider::model"  (double-colon separates provider from model so model colons don't confuse parsing)
# e.g. "openrouter::openrouter/auto,ollama::qwen2.5-coder:7b"
def _parse_fallbacks(raw: str) -> list[tuple[str, str]]:
    result = []
    for entry in raw.split(","):
        entry = entry.strip()
        if "::" not in entry:
            continue
        provider, model = entry.split("::", 1)
        result.append((provider.strip(), model.strip()))
    return result

WORKER_FALLBACKS: list[tuple[str, str]] = _parse_fallbacks(
    os.getenv("WORKER_FALLBACKS", "openrouter::qwen/qwen-2.5-coder-32b-instruct:free,ollama::qwen2.5-coder:7b")
)

# Maximum tasks to run in parallel (1 = sequential, 2-4 = parallel)
MAX_PARALLEL_WORKERS: int = int(os.getenv("MAX_PARALLEL_WORKERS", "2"))

# Task types that prefer local Ollama (simple, low-token tasks)
# Everything else prefers cloud (complex logic, multi-file, reasoning-heavy)
LOCAL_FIRST_TASK_TYPES: set[str] = set(
    os.getenv("LOCAL_FIRST_TASK_TYPES", "scaffold,style,data,config").split(",")
)

ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")
GROQ_API_KEY: str | None = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY: str | None = os.getenv("OPENROUTER_API_KEY")
PROJECTS_DIR: Path = Path(os.getenv("PROJECTS_DIR", "./projects"))
MAX_RETRIES_PER_TASK: int = int(os.getenv("MAX_RETRIES_PER_TASK", "3"))
MAX_TASKS: int = int(os.getenv("MAX_TASKS", "50"))
MAX_FORMAT5_DEPTH: int = int(os.getenv("MAX_FORMAT5_DEPTH", "3"))
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# Path to the orchestrator system prompt (one level up from this file)
ORCHESTRATOR_PROMPT_PATH: Path = Path(__file__).parent.parent / "orchestrator.txt"
