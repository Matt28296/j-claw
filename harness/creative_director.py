from __future__ import annotations
from rich.console import Console

from config import CREATIVE_DIRECTOR_PROMPT_PATH

console = Console()

# The CREATIVE_BRIEF contract the Technical Architect consumes downstream (creative_director.txt).
# These mirror the prompt's declared enums; kept module-level (like technical_architect._ALLOWED_STACKS)
# so the validator is a pure, directly-testable function.
_ALLOWED_OUTPUT_TYPES = {"film", "game", "app", "website", "code"}
_ALLOWED_SCALES = {"prototype", "mvp", "production"}
# An over-inflated brief (LLM run-on) should escalate for a tighter interpretation, not poison the DAG.
_MAX_FEATURES = 30


def _validate(brief: dict) -> None:
    """Fallback boundary for the Codex-first planning ladder.

    A thin or malformed brief must escalate (Codex → one retry → Sonnet → Opus) rather than pass
    silently and mis-route the whole downstream build. The Technical Architect keys its TECH_SPEC on
    output_type + scale, and Phase 4 difficulty routing keys on scale — so both are validated against
    their enums here, not merely checked for presence.
    """
    if not isinstance(brief, dict):
        raise ValueError("CREATIVE_BRIEF must be a JSON object")

    output_type = brief.get("output_type")
    if output_type not in _ALLOWED_OUTPUT_TYPES:
        raise ValueError(
            f"CREATIVE_BRIEF output_type {output_type!r} not in {sorted(_ALLOWED_OUTPUT_TYPES)}"
        )

    scale = brief.get("scale")
    if scale not in _ALLOWED_SCALES:
        raise ValueError(
            f"CREATIVE_BRIEF scale {scale!r} not in {sorted(_ALLOWED_SCALES)}"
        )

    features = brief.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("CREATIVE_BRIEF missing required field: features (non-empty array)")
    if len(features) > _MAX_FEATURES:
        raise ValueError(
            f"CREATIVE_BRIEF features count {len(features)} exceeds {_MAX_FEATURES} "
            "(over-inflated brief — escalate for a tighter interpretation)"
        )

    # Non-code outputs feed the asset/visual pipeline, which reads visual_identity. Code prompts are
    # explicitly allowed minimal defaults by the prompt (creative_director.txt), so they are exempt.
    if output_type != "code":
        visual = brief.get("visual_identity")
        if not isinstance(visual, dict) or not visual:
            raise ValueError(
                "CREATIVE_BRIEF missing non-empty 'visual_identity' for non-code output_type"
            )


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

        brief = planning_call(self._system_prompt, intent, _validate, role="creative")

        console.print(
            f"[bold cyan]Creative Brief:[/bold cyan] "
            f"output_type=[green]{brief['output_type']}[/green]  "
            f"scale=[green]{brief.get('scale', 'mvp')}[/green]  "
            f"features=[green]{len(brief['features'])}[/green]"
        )
        return brief
