from __future__ import annotations
from rich.console import Console

from config import CREATIVE_DIRECTOR_PROMPT_PATH

console = Console()


class CreativeDirector:
    def __init__(self) -> None:
        # No ANTHROPIC_API_KEY requirement: interpret() plans Codex-first ($0) via planning_call.
        # The Anthropic fallback inside planning_call raises only if it is actually reached without
        # a key — so the Creative Director can run key-free whenever Codex is available (Codex-review
        # fix: the old hard requirement blocked Codex-first planning when no Anthropic key was set).
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
