from __future__ import annotations
import json
import time
from pathlib import Path
import anthropic
from rich.console import Console

from config import CREATIVE_DIRECTOR_MODEL, ANTHROPIC_API_KEY, CREATIVE_DIRECTOR_PROMPT_PATH
from cache_telemetry import log_cache_usage
from cost import record_usage, record_role_event

console = Console()


class CreativeDirector:
    def __init__(self) -> None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self._system_prompt = CREATIVE_DIRECTOR_PROMPT_PATH.read_text(encoding="utf-8")

    def interpret(self, intent: str) -> dict:
        """
        Send raw user intent through the Codex-first planning ladder (Phase 3).
        Returns a validated CREATIVE_BRIEF dict.

        Routes through worker.planning_call: Codex (free OAuth) → one same-tier retry → Anthropic
        Sonnet → Opus, gated by the required-field validation below (preserved as the fallback
        boundary, not mere parse success). planning_call handles telemetry + fallback; Codex
        unavailability/quota never hard-fails — it falls through to Anthropic.
        """
        from worker import planning_call

        def _validate(brief):
            if not isinstance(brief, dict):
                raise ValueError("CREATIVE_BRIEF must be a JSON object")
            if "output_type" not in brief:
                raise ValueError("CREATIVE_BRIEF missing required field: output_type")
            if not isinstance(brief.get("features"), list) or not brief["features"]:
                raise ValueError("CREATIVE_BRIEF missing required field: features (non-empty array)")

        brief = planning_call(self._system_prompt, intent, _validate, role="creative")

        console.print(
            f"[bold cyan]Creative Brief:[/bold cyan] "
            f"output_type=[green]{brief['output_type']}[/green]  "
            f"scale=[green]{brief.get('scale', 'mvp')}[/green]  "
            f"features=[green]{len(brief['features'])}[/green]"
        )
        return brief


def _strip_fences(text: str) -> str:
    """Remove accidental ```json ... ``` wrapping that the model sometimes adds."""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    inner = lines[1:] if lines[-1].strip() == "```" else lines[1:]
    if inner and inner[-1].strip() == "```":
        inner = inner[:-1]
    return "\n".join(inner).strip()
