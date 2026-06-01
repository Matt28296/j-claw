from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ORCHESTRATOR_MODEL: str = "claude-sonnet-4-6"
WORKER_MODEL: str = os.getenv("WORKER_MODEL", "qwen2.5-coder:7b")
ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")
PROJECTS_DIR: Path = Path(os.getenv("PROJECTS_DIR", "./projects"))
MAX_RETRIES_PER_TASK: int = int(os.getenv("MAX_RETRIES_PER_TASK", "3"))
MAX_FORMAT5_DEPTH: int = int(os.getenv("MAX_FORMAT5_DEPTH", "3"))
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# Path to the orchestrator system prompt (one level up from this file)
ORCHESTRATOR_PROMPT_PATH: Path = Path(__file__).parent.parent / "orchestrator.txt"
