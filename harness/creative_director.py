from __future__ import annotations
from rich.console import Console

from config import CREATIVE_DIRECTOR_PROMPT_PATH, OPUS_MODEL
from interpretation_risk import score_interpretation_risk, HIGH_RISK_THRESHOLD

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
        Send raw user intent through the planning ladder. Returns a validated CREATIVE_BRIEF dict.

        Phase 4: routes by interpretation risk score before calling the planning ladder.
          - Low risk (< HIGH_RISK_THRESHOLD): Codex-first via planning_call (existing Phase 3 path)
          - High risk (>= HIGH_RISK_THRESHOLD): Sonnet primary (not Codex-first) — Codex is less
            reliable on ambiguous / novel / constraint-heavy intents, so we skip straight to Sonnet
            to avoid wasting a Codex slot on output that will fail validation and require escalation.
            Opus is the final fallback (Amendment #3: risk > 0.75 Opus escalation).

        planning_call handles telemetry + fallback; its Codex path is still available for low-risk
        intents as before. Raises RuntimeError only if every tier fails.
        """
        from worker import planning_call, _call_anthropic

        risk = score_interpretation_risk(intent)
        console.print(
            f"  [dim]Interpretation risk: {risk:.2f} "
            f"({'high — routing to Sonnet' if risk >= HIGH_RISK_THRESHOLD else 'low — Codex-first'})[/dim]"
        )

        if risk >= HIGH_RISK_THRESHOLD:
            # High-risk path: skip Codex, go directly to Sonnet primary.
            # Codex is less reliable on ambiguous / novel / constraint-heavy intents,
            # so we avoid wasting an OAuth slot on output likely to fail validation.
            # Amendment #3: escalate to Opus only when risk > 0.75; else Sonnet-only.
            import time as _time
            from cost import record_role_event as _record
            from llm_json import loads_llm_json_object
            from orchestrator import _strip_fences

            sonnet_model = "claude-sonnet-4-6"
            opus_model = OPUS_MODEL

            # Build the model list: always Sonnet; add Opus only when risk is very high.
            _models_to_try = [sonnet_model]
            if risk > 0.75:
                _models_to_try.append(opus_model)

            last_err: Exception | None = None
            brief: dict | None = None
            for _model in _models_to_try:
                _t0 = _time.monotonic()
                try:
                    raw = _call_anthropic(_model, self._system_prompt, intent, label="creative")
                    try:
                        parsed = loads_llm_json_object(raw)
                    except Exception:
                        import json as _j
                        parsed = _j.loads(_strip_fences(raw))
                    _validate(parsed)
                    _record("creative", provider="anthropic", model=_model, success=True,
                            fallback=(_model != sonnet_model),
                            latency_s=_time.monotonic() - _t0)
                    brief = parsed
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    _record("creative", provider="anthropic", model=_model, success=False,
                            schema_fail=True, fallback=(_model != sonnet_model),
                            latency_s=_time.monotonic() - _t0)

            if brief is None:
                # All high-risk models failed — fall through to planning_call ladder as backstop.
                console.print(
                    f"  [yellow]High-risk Sonnet path failed ({last_err}) — "
                    f"falling through to planning_call ladder[/yellow]"
                )
                brief = planning_call(self._system_prompt, intent, _validate, role="creative")
        else:
            # Low-risk path: existing Codex-first planning_call (Phase 3 behavior).
            brief = planning_call(self._system_prompt, intent, _validate, role="creative")

        console.print(
            f"[bold cyan]Creative Brief:[/bold cyan] "
            f"output_type=[green]{brief['output_type']}[/green]  "
            f"scale=[green]{brief.get('scale', 'mvp')}[/green]  "
            f"features=[green]{len(brief['features'])}[/green]"
        )
        return brief
