"""Technical Architect — translates CREATIVE_BRIEF → TECH_SPEC and seeds project_memory/."""
from __future__ import annotations
import json
import time
from pathlib import Path
import anthropic
from rich.console import Console

from config import (
    ANTHROPIC_API_KEY, ORCHESTRATOR_MAX_TOKENS,
    TECHNICAL_ARCHITECT_MODEL, TECHNICAL_ARCHITECT_PROMPT_PATH,
)
from project_memory import ProjectMemory
from cache_telemetry import log_cache_usage
from cost import record_usage

console = Console()

_ALLOWED_STACKS = {
    "vanilla", "react-vite", "fastapi", "phaser", "full-stack", "web3",
    "react-native", "socket-io", "three-js", "electron", "film", "video-editor",
    "tauri", "godot", "websocket-sse",
}


class TechnicalArchitect:
    """Runs before the orchestrator INIT to own all technical decisions."""

    def __init__(self) -> None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self._system_prompt = TECHNICAL_ARCHITECT_PROMPT_PATH.read_text(encoding="utf-8")

    def review(self, brief: dict, intent: str, output_dir: Path, max_retries: int = 2) -> dict:
        """Produce a TECH_SPEC dict and seed project_memory/ in output_dir.

        Returns the validated tech_spec dict.
        Raises RuntimeError if all retries fail.
        """
        user_message = json.dumps({"creative_brief": brief, "user_intent": intent})
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                response = self._client.messages.create(
                    model=TECHNICAL_ARCHITECT_MODEL,
                    max_tokens=ORCHESTRATOR_MAX_TOKENS,
                    system=[{
                        "type": "text",
                        "text": self._system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user_message}],
                )
                log_cache_usage(response.usage, "architect")
                record_usage(response.usage, TECHNICAL_ARCHITECT_MODEL, "architect")
                text = response.content[0].text.strip()
                text = _strip_fences(text)
                tech_spec = json.loads(text)
                _validate(tech_spec)

                ProjectMemory(output_dir).initialize(tech_spec, intent)

                console.print(
                    f"[bold cyan]Technical Architect:[/bold cyan] "
                    f"stack=[green]{tech_spec['confirmed_stack']}[/green]  "
                    f"files=[green]{len(tech_spec.get('file_structure', []))}[/green]  "
                    f"ADRs=[green]{len(tech_spec.get('adrs_to_create', []))}[/green]"
                )
                return tech_spec

            except (json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                if attempt < max_retries:
                    console.print(
                        f"[yellow]Technical Architect output invalid "
                        f"(attempt {attempt + 1}/{max_retries + 1}): {exc} — retrying...[/yellow]"
                    )
                    time.sleep(1 + attempt)

        raise RuntimeError(
            f"TechnicalArchitect failed after {max_retries + 1} attempts: {last_error}"
        ) from last_error


def _validate(spec: dict) -> None:
    if not spec.get("confirmed_stack"):
        raise ValueError("TECH_SPEC missing 'confirmed_stack'")
    if spec["confirmed_stack"] not in _ALLOWED_STACKS:
        raise ValueError(
            f"TECH_SPEC confirmed_stack {spec['confirmed_stack']!r} not in allowed set"
        )
    if not isinstance(spec.get("file_structure"), list) or not spec["file_structure"]:
        raise ValueError("TECH_SPEC missing 'file_structure' array")
    if not isinstance(spec.get("adrs_to_create"), list) or not spec["adrs_to_create"]:
        raise ValueError("TECH_SPEC must include at least one ADR in 'adrs_to_create'")


def _strip_fences(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    inner = lines[1:]
    if inner and inner[-1].strip() == "```":
        inner = inner[:-1]
    return "\n".join(inner).strip()
