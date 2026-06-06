from __future__ import annotations
import json
from pathlib import Path
import anthropic
from rich.console import Console

from config import CREATIVE_DIRECTOR_MODEL, ANTHROPIC_API_KEY, CREATIVE_DIRECTOR_PROMPT_PATH
from cache_telemetry import log_cache_usage
from cost import record_usage

console = Console()


class CreativeDirector:
    def __init__(self) -> None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self._system_prompt = CREATIVE_DIRECTOR_PROMPT_PATH.read_text(encoding="utf-8")

    def interpret(self, intent: str) -> dict:
        """
        Send raw user intent to the Creative Director model.
        Returns a validated CREATIVE_BRIEF dict.
        """
        response = self._client.messages.create(
            model=CREATIVE_DIRECTOR_MODEL,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": self._system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": intent}],
        )
        log_cache_usage(response.usage, "creative")
        record_usage(response.usage, CREATIVE_DIRECTOR_MODEL, "creative")

        text = response.content[0].text.strip()
        text = _strip_fences(text)
        brief = json.loads(text)

        if "output_type" not in brief:
            raise ValueError("CREATIVE_BRIEF missing required field: output_type")
        if not isinstance(brief.get("features"), list) or not brief["features"]:
            raise ValueError("CREATIVE_BRIEF missing required field: features (non-empty array)")

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
