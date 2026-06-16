from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def spec_stack(spec: dict) -> str:
    """Stack of a FORMAT 1 spec. The orchestrator nests it under architecture
    ('architecture': {'stack': 'film'}) with the top-level 'stack' usually
    absent — reading only spec['stack'] silently disabled the film honesty
    gates (observed live). All stack reads must go through this helper."""
    return spec.get("stack", "") or spec.get("architecture", {}).get("stack", "")

ORCHESTRATOR_MODEL: str = "claude-sonnet-4-6"
ORCHESTRATOR_PROVIDER: str = os.getenv("ORCHESTRATOR_PROVIDER", "anthropic")  # "anthropic" | "openrouter" | "gemini"
ORCHESTRATOR_API_MODEL: str = os.getenv("ORCHESTRATOR_API_MODEL", "openrouter/auto")

# Gemini orchestrator (ORCHESTRATOR_PROVIDER=gemini) — calls Google's OpenAI-compatible
# endpoint directly with GOOGLE_API_KEY, so the AI Studio free tier applies (unlike routing
# the same model through OpenRouter, which always bills).
GEMINI_ORCHESTRATOR_MODEL: str = os.getenv("GEMINI_ORCHESTRATOR_MODEL", "gemini-2.5-flash")

# Comma-separated fallback models tried in order when the primary is rate-limited
# e.g. "nvidia/nemotron-3-super-120b-a12b:free,meta-llama/llama-3.3-70b-instruct:free"
ORCHESTRATOR_FALLBACK_MODELS: list[str] = [
    m.strip() for m in os.getenv("ORCHESTRATOR_FALLBACKS", "nvidia/nemotron-3-super-120b-a12b:free,meta-llama/llama-3.3-70b-instruct:free").split(",") if m.strip()
]

WORKER_PROVIDER: str = os.getenv("WORKER_PROVIDER", "ollama")  # "ollama" | "groq" | "openrouter"
WORKER_MODEL: str = os.getenv("WORKER_MODEL", "qwen2.5-coder:14b")

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
    # Default local fallback is qwen3:8b — the previously-defaulted qwen2.5-coder:7b is not
    # installed on this host, so it would error rather than fall back.
    os.getenv("WORKER_FALLBACKS", "openrouter::qwen/qwen-2.5-coder-32b-instruct:free,ollama::qwen3:8b")
)

# Ordered worker model ladder, weakest → strongest. The router (worker.route_task) picks a
# starting rung by task complexity; on each retry the effective rung escalates one step up this
# ladder (see worker.routed_rung). Empty/unset disables laddering and falls back to the legacy
# WORKER_PROVIDER + WORKER_FALLBACKS chain. Env override uses the same "provider::model" syntax.
WORKER_LADDER: list[tuple[str, str]] = _parse_fallbacks(
    os.getenv(
        "WORKER_LADDER",
        "ollama::qwen3:8b,ollama::qwen2.5-coder:14b,anthropic::claude-sonnet-4-6",
    )
)

# Hard cap on the number of PAID (non-ollama) worker calls allowed per project run. Escalation
# to a cloud rung is gated by this budget; once spent, tasks clamp to the strongest local rung
# instead of paying. Prevents a multi-task project from silently burning the API budget.
MAX_PAID_WORKER_CALLS: int = int(os.getenv("MAX_PAID_WORKER_CALLS", "15"))

# Maximum tasks to run in parallel (1 = sequential, 2-4 = parallel)
MAX_PARALLEL_WORKERS: int = int(os.getenv("MAX_PARALLEL_WORKERS", "4"))

# Task types that prefer local Ollama (simple, low-token tasks)
# Everything else prefers cloud (complex logic, multi-file, reasoning-heavy)
LOCAL_FIRST_TASK_TYPES: set[str] = set(
    os.getenv("LOCAL_FIRST_TASK_TYPES", "scaffold,style,data,config").split(",")
)

ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")
GROQ_API_KEY: str | None = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY: str | None = os.getenv("OPENROUTER_API_KEY")
GOOGLE_API_KEY: str | None = os.getenv("GOOGLE_API_KEY")

# Default JWT secret injected into generated full-stack projects that include auth tasks.
# Workers read JWT_SECRET from this env var at code-gen time so .env.example is pre-populated.
JWT_SECRET: str = os.getenv(
    "JWT_SECRET",
    "change-me-to-a-long-random-secret-at-least-32-chars",
)
SD_API_URL: str = os.getenv("SD_API_URL", "http://localhost:7860")   # AUTOMATIC1111 / Forge
ASSET_PROVIDER: str = os.getenv("ASSET_PROVIDER", "sd")  # "sd" | "comfyui" | "none"
COMFYUI_API_URL: str = os.getenv("COMFYUI_API_URL", "http://localhost:8188")
COMFYUI_CHECKPOINT: str = os.getenv("COMFYUI_CHECKPOINT", "")  # auto-detect if empty
COMFYUI_WIDTH: int = int(os.getenv("COMFYUI_WIDTH", "768"))
COMFYUI_HEIGHT: int = int(os.getenv("COMFYUI_HEIGHT", "512"))
COQUI_API_URL: str = os.getenv("COQUI_API_URL", "http://localhost:5002")

# Piper TTS — local narration (replaces Coqui when PIPER_BINARY is set)
PIPER_BINARY: str = os.getenv("PIPER_BINARY", r"E:\tools\piper\piper\piper.exe")
PIPER_VOICE: str = os.getenv("PIPER_VOICE", r"E:\tools\piper\voices\en_US-ryan-high.onnx")

# FluidSynth — local music rendering from MIDI
FLUIDSYNTH_BINARY: str = os.getenv("FLUIDSYNTH_BINARY", r"E:\tools\fluidsynth\fluidsynth-v2.5.5-win10-x64-cpp11\bin\fluidsynth.exe")
FLUIDSYNTH_SOUNDFONT: str = os.getenv("FLUIDSYNTH_SOUNDFONT", r"E:\tools\soundfonts\FluidR3_GM.sf2")

# Optional deployment hook: command to run in the output dir after git commit.
# Example: "vercel --prod --yes" or "netlify deploy --prod --dir=dist"
DEPLOY_HOOK: str | None = os.getenv("DEPLOY_HOOK")  # None = no deployment
DEPLOY_TIMEOUT: int = int(os.getenv("DEPLOY_TIMEOUT", "120"))  # seconds
PROJECTS_DIR: Path = Path(os.getenv("PROJECTS_DIR", "./projects"))
MAX_RETRIES_PER_TASK: int = int(os.getenv("MAX_RETRIES_PER_TASK", "3"))
MAX_TASKS: int = int(os.getenv("MAX_TASKS", "75"))
ORCHESTRATOR_MAX_TOKENS: int = int(os.getenv("ORCHESTRATOR_MAX_TOKENS", "16384"))
CREATIVE_DIRECTOR_MODEL: str = os.getenv("CREATIVE_DIRECTOR_MODEL", "claude-haiku-4-5-20251001")
EXECUTION_ERROR_MODEL: str = os.getenv("EXECUTION_ERROR_MODEL", "claude-haiku-4-5-20251001")
FINAL_REVIEW_MODEL: str = os.getenv("FINAL_REVIEW_MODEL", "claude-haiku-4-5-20251001")

# Emergency orchestrator fallback: when the primary provider (e.g. Gemini) exhausts all
# retries, route through this secondary provider instead of dying. "anthropic" uses the
# Orchestrator class (Sonnet); set to "" to disable and propagate the original RuntimeError.
ORCHESTRATOR_EMERGENCY_PROVIDER: str = os.getenv(
    "ORCHESTRATOR_EMERGENCY_PROVIDER",
    "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "",
)
EMERGENCY_ORCHESTRATOR_MODEL: str = os.getenv("EMERGENCY_ORCHESTRATOR_MODEL", "claude-sonnet-4-6")
CREATIVE_DIRECTOR_PROMPT_PATH: Path = Path(__file__).parent.parent / "creative_director.txt"
MAX_FORMAT5_DEPTH: int = int(os.getenv("MAX_FORMAT5_DEPTH", "3"))
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EXPERIENCE_LOG: str = os.getenv("EXPERIENCE_LOG", "experience.jsonl")

# Path to the orchestrator system prompt (one level up from this file)
ORCHESTRATOR_PROMPT_PATH: Path = Path(__file__).parent.parent / "orchestrator.txt"

TECHNICAL_ARCHITECT_ENABLED: bool = os.getenv("TECHNICAL_ARCHITECT_ENABLED", "true") == "true"
TECHNICAL_ARCHITECT_MODEL: str = os.getenv("TECHNICAL_ARCHITECT_MODEL", "claude-haiku-4-5-20251001")
TECHNICAL_ARCHITECT_PROMPT_PATH: Path = Path(__file__).parent.parent / "technical_architect.txt"
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8765"))
DASHBOARD_AUTOOPEN: bool = os.getenv("DASHBOARD_AUTOOPEN", "true") == "true"
GODOT_PATH: str = os.getenv("GODOT_PATH", "godot")
PINATA_API_KEY: str = os.getenv("PINATA_API_KEY", "")
PINATA_SECRET_KEY: str = os.getenv("PINATA_SECRET_KEY", "")
IPFS_AUTO_PIN: bool = os.getenv("IPFS_AUTO_PIN", "false").lower() == "true"

# Pipeline resilience
PIPELINE_MAX_RETRIES: int = int(os.getenv("PIPELINE_MAX_RETRIES", "2"))
HEAL_MAX_CYCLES: int = int(os.getenv("HEAL_MAX_CYCLES", "3"))
ORCHESTRATOR_TIMEOUT: int = int(os.getenv("ORCHESTRATOR_TIMEOUT", "300"))   # seconds per API call
WORKER_TASK_TIMEOUT: int = int(os.getenv("WORKER_TASK_TIMEOUT", "600"))     # seconds per task batch
