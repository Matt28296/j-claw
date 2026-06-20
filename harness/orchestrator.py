from __future__ import annotations
import json
import time
from pathlib import Path
import anthropic
from rich.console import Console
from rich.syntax import Syntax

from config import (
    ORCHESTRATOR_MODEL, ANTHROPIC_API_KEY, ORCHESTRATOR_PROMPT_PATH,
    ORCHESTRATOR_API_MODEL, ORCHESTRATOR_FALLBACK_MODELS, OPENROUTER_API_KEY,
    ORCHESTRATOR_MAX_TOKENS, EXECUTION_ERROR_MODEL, ORCHESTRATOR_TIMEOUT,
    GOOGLE_API_KEY, GEMINI_ORCHESTRATOR_MODEL,
    ORCHESTRATOR_EMERGENCY_PROVIDER, EMERGENCY_ORCHESTRATOR_MODEL,
    GEMINI_QUOTA_FAILFAST, CODEX_PLANNING_RESERVE, OPUS_MODEL, HAIKU_MODEL,
    PAID_ORCH_ENABLED,
)
from validator import validate_response, OrchestratorOutputError
from cache_telemetry import log_cache_usage
from cost import record_usage, record_role_event, check_cost_ceiling

console = Console()


_RESPONSE_FILE = Path("orchestrator_response.json")
_INPUT_FILE = Path("orchestrator_input.json")


# ── Run-level Gemini quota latch ──────────────────────────────────────────────
# A quota-class 429 (daily RESOURCE_EXHAUSTED, not a transient per-minute throttle) means Gemini
# is out for the rest of the run. Mirrors the worker's _codex_disabled / _grok_disabled pattern:
# the first quota hit raises fast AND latches this flag so every subsequent orchestrator call skips
# Gemini and falls straight to the emergency chain instead of re-discovering the outage (and re-
# burning the 30-60s retryDelay) on each of the ~6-8 calls in a build. Reset in reset_orchestrator_run().
_gemini_quota_disabled = False

# Per-run count of orchestrator Codex planning calls, bounded by CODEX_PLANNING_RESERVE so planning
# can't drain the shared worker-rescue Codex capacity. Module-level (one budget per run regardless of
# how many CodexOrchestrator instances exist) so it is actually cleared by reset_orchestrator_run() —
# a per-instance counter would leak across an in-process run reuse. Reset in reset_orchestrator_run().
_codex_planning_calls = 0


# Per-MINUTE throttle signatures. CRITICAL: Gemini returns the SAME HTTP 429 + RESOURCE_EXHAUSTED
# status + QuotaFailure detail for a transient per-minute rate-limit as it does for a daily/lifetime
# outage — they differ ONLY in the violated quota metric's PERIOD, which appears in the QuotaFailure
# violation id (e.g. "...PerMinutePerProjectPerModel-FreeTier" vs "...PerDay..."). A per-minute
# throttle clears within the run, so it must NEVER latch Gemini off; matching it here short-circuits
# the quota-class check below to False.
_PER_MINUTE_MARKERS = (
    "perminute",
    "per minute",
    "per_minute",
    "per-minute",
    "/ minute",
)

# Quota-class signatures: a daily/lifetime exhaustion that won't clear within the run. Evaluated only
# AFTER the per-minute short-circuit, so an ordinary burst throttle is not misclassified.
_QUOTA_CLASS_MARKERS = (
    "resource_exhausted",
    "quota",
    "daily limit",
    "per day",
    "/ day",
    "exceeded your current quota",
    "free_tier",
    "free tier",
    "billing",
)


def _is_quota_class_429(exc: Exception) -> bool:
    """True ONLY for a daily/lifetime quota exhaustion that won't clear within the run — NOT a
    transient per-minute throttle. Builds ONE combined blob from the exception text AND the
    structured error body (so the QuotaFailure violation ids are always in scope — the two are
    additive, not an either/or early-return), short-circuits to False on any per-minute marker, and
    only then treats a structured RESOURCE_EXHAUSTED / QuotaFailure or an explicit daily/free-tier
    phrase as quota-class. Biased toward NOT latching: a wrong 'quota' verdict disables free Gemini
    for the whole run, whereas a wrong 'transient' verdict only costs one retry/backoff."""
    blob = str(exc).lower()
    structured_quota = False
    try:
        body = exc.response.json()
        blob += " " + json.dumps(body).lower()
        err = body.get("error", {})
        if "resource_exhausted" in str(err.get("status", "")).lower():
            structured_quota = True
        for detail in err.get("details", []):
            if "quotafailure" in str(detail.get("@type", "")).lower():
                structured_quota = True
    except Exception:
        pass
    # Per-minute throttle → transient, never latch (even with RESOURCE_EXHAUSTED / QuotaFailure).
    if any(m in blob for m in _PER_MINUTE_MARKERS):
        return False
    if structured_quota:
        return True
    return any(m in blob for m in _QUOTA_CLASS_MARKERS)


def reset_orchestrator_run() -> None:
    """Reset per-run orchestrator latches. Call at project start, alongside worker.reset_paid_budget()."""
    global _gemini_quota_disabled, _codex_planning_calls
    _gemini_quota_disabled = False
    _codex_planning_calls = 0


class ManualOrchestrator:
    """
    You are the orchestrator.
    For each state the harness writes the input to orchestrator_input.json,
    opens orchestrator_response.json in Notepad for you to fill in,
    then reads and validates your response when you press Enter.
    """

    def call(self, payload: dict, max_retries: int = 2) -> dict:
        state = payload.get("system_state", "INIT")

        _INPUT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _RESPONSE_FILE.write_text("{}\n", encoding="utf-8")

        console.rule(f"[bold magenta]ORCHESTRATOR INPUT — {state}[/bold magenta]")
        console.print(Syntax(json.dumps(payload, indent=2), "json", theme="monokai"))
        console.rule()
        console.print(
            f"\n[bold magenta]Your turn — state: {state}[/bold magenta]\n"
            f"  Input written to: {_INPUT_FILE.resolve()}\n"
            f"  Write your JSON response to: {_RESPONSE_FILE.resolve()}\n"
            "  Then press Enter here to continue.\n"
        )

        while True:
            input("  >> Press Enter once orchestrator_response.json is ready...")
            try:
                raw = _RESPONSE_FILE.read_text(encoding="utf-8-sig").strip()
                raw = _sanitize(raw)
                raw = _strip_fences(raw)
                parsed = json.loads(raw)
                validate_response(state, parsed)
                return parsed
            except (json.JSONDecodeError, OrchestratorOutputError) as exc:
                console.print(f"\n  [red]Invalid response: {exc}[/red]")
                console.print(f"  Fix {_RESPONSE_FILE} and press Enter again:\n")


class Orchestrator:
    def __init__(self, model: str | None = None) -> None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self._system_prompt = ORCHESTRATOR_PROMPT_PATH.read_text(encoding="utf-8")
        # Optional pin: an emergency-chain rung forces a specific model (e.g. Sonnet then Opus)
        # rather than the module default. None = use the default ORCHESTRATOR_MODEL / per-state model.
        self._pinned_model = model

    def call(self, payload: dict, max_retries: int = 2) -> dict:
        """
        Send payload to the orchestrator, validate the response, and return parsed JSON.
        Retries up to max_retries times on invalid output before raising.
        """
        state = payload.get("system_state", "INIT")
        user_message = json.dumps(payload)
        last_error: Exception | None = None

        # Master kill-switch for the metered Anthropic rung. When paid orchestration is disabled
        # (default on a $0-credit box — see config.PAID_ORCH_ENABLED) refuse BEFORE spending the
        # call: raise RuntimeError so any wrapping CompositeOrchestrator falls through to the next
        # free rung, and a bare/last rung fails CLOSED with a clear message + failure handoff rather
        # than crashing on the inevitable 400 "credit balance too low" (the 2026-06-19 D4 crash).
        if not PAID_ORCH_ENABLED:
            record_role_event(f"orch:{state}", provider="anthropic", model=self._pinned_model or "paid",
                              success=False, latency_s=0.0)
            raise RuntimeError(
                f"Paid orchestrator disabled (PAID_ORCH_ENABLED=false) — refusing metered Anthropic "
                f"orchestration (orch:{state}). Relying on free OAuth rungs; set PAID_ORCH_ENABLED=true "
                f"only on a box whose ANTHROPIC_API_KEY account has credit.")

        for attempt in range(max_retries + 1):
            # Per-build cost circuit-breaker: refuse before spending if the ceiling
            # was crossed. Placed BEFORE the try so it fails closed (propagates out)
            # instead of being swallowed by the retry handlers below.
            check_cost_ceiling()
            # Control-plane paid-call budget: bound the number of metered orchestrator calls per
            # build (the emergency Sonnet→Opus fallback is otherwise uncapped — a latched-free-rung
            # run racked up 38 paid orchestrator calls on 2026-06-19). Reserve per actual API call;
            # when exhausted, raise so CompositeOrchestrator falls through / the build fails closed
            # rather than silently overspending. Shared with planning_call's Anthropic tiers.
            import worker
            if not worker._reserve_paid_orch_call():
                raise RuntimeError(
                    f"Paid orchestrator budget (MAX_PAID_ORCH_CALLS) exhausted — refusing further "
                    f"metered orchestration (orch:{state}). Free OAuth rungs latched or unavailable.")
            try:
                # A pinned model (emergency-chain rung) intentionally wins over the per-state
                # EXECUTION_ERROR_MODEL: once we're on the paid Sonnet→Opus fallback the rung's
                # fixed tier is the policy, so EXECUTION_ERROR recovery there runs on the pinned
                # model, not the cheaper Haiku the primary path would use.
                _model = (self._pinned_model
                          or (EXECUTION_ERROR_MODEL if state == "EXECUTION_ERROR" else ORCHESTRATOR_MODEL))
                _t0 = time.monotonic()
                response = self._client.messages.create(
                    model=_model,
                    max_tokens=ORCHESTRATOR_MAX_TOKENS,
                    system=[{"type": "text", "text": self._system_prompt, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": user_message}],
                    timeout=ORCHESTRATOR_TIMEOUT,
                )
                log_cache_usage(response.usage, f"orch:{state}")
                record_usage(response.usage, _model, f"orch:{state}")
                if response.stop_reason == "max_tokens":
                    console.print(
                        f"[yellow]⚠ Orchestrator hit max_tokens ({ORCHESTRATOR_MAX_TOKENS}) — "
                        "response truncated. Raise ORCHESTRATOR_MAX_TOKENS in .env or shorten DAG.[/yellow]"
                    )
                text = response.content[0].text.strip()
                text = _strip_fences(text)
                text = _fix_json_strings(text)
                parsed = json.loads(text)
                validate_response(state, parsed)
                record_role_event(f"orch:{state}", provider="anthropic", model=_model,
                                  success=True, latency_s=time.monotonic() - _t0)
                return parsed

            except anthropic.APITimeoutError as exc:
                last_error = exc
                console.print(
                    f"[yellow]Orchestrator timed out after {ORCHESTRATOR_TIMEOUT}s "
                    f"(attempt {attempt + 1}/{max_retries + 1}) — retrying...[/yellow]"
                )
                if attempt < max_retries:
                    time.sleep(2 + attempt)

            except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as exc:
                # 429 rate limits and transient server errors (529 overloaded, 5xx,
                # dropped connections) resolve on their own — back off and retry
                # instead of crashing the sub-project.
                last_error = exc
                wait = 20 * (attempt + 1)
                console.print(
                    f"[yellow]Orchestrator unavailable ({type(exc).__name__}) — "
                    f"waiting {wait}s (attempt {attempt + 1}/{max_retries + 1})…[/yellow]"
                )
                if attempt < max_retries:
                    time.sleep(wait)

            except anthropic.BadRequestError as exc:
                # 400s are NON-retryable and NON-transient: the dominant case is "credit balance too
                # low" (a $0 metered account), but also malformed-request bugs. Retrying just re-hits
                # the same wall, and letting it propagate crashes the whole build (the 2026-06-19 D4
                # failure). Record the failed attempt and convert to RuntimeError so a wrapping
                # CompositeOrchestrator falls through to the next free rung and a bare/last rung fails
                # CLOSED (failure handoff) instead of dumping a raw anthropic traceback.
                record_role_event(f"orch:{state}", provider="anthropic", model=_model,
                                  success=False, latency_s=time.monotonic() - _t0)
                raise RuntimeError(
                    f"Paid orchestrator rung unavailable (orch:{state}): {type(exc).__name__}: {exc}"
                ) from exc

            except anthropic.APIError as exc:
                # Catch-all for every OTHER anthropic API error that is NOT one of the transient,
                # already-retried families above (APITimeout / RateLimit / InternalServer / APIConnection)
                # and not the BadRequest credit-balance case. This covers AuthenticationError (401),
                # PermissionDeniedError (403), NotFoundError (404), UnprocessableEntityError (422), bare
                # APIStatusError, etc. — all non-retryable here. Without this, such an error would escape
                # Orchestrator.call as a raw anthropic.* exception (the same crash shape as the D4
                # BadRequestError) and bypass CompositeOrchestrator (which only catches RuntimeError from
                # the primary). Convert to RuntimeError so the chain falls through / the build fails closed.
                # MUST stay AFTER the specific handlers — anthropic.APIError is their common base class.
                record_role_event(f"orch:{state}", provider="anthropic", model=_model,
                                  success=False, latency_s=time.monotonic() - _t0)
                raise RuntimeError(
                    f"Paid orchestrator rung unavailable (orch:{state}): {type(exc).__name__}: {exc}"
                ) from exc

            except (json.JSONDecodeError, OrchestratorOutputError) as exc:
                last_error = exc
                record_role_event(f"orch:{state}", provider="anthropic", model=_model,
                                  success=False, schema_fail=True,
                                  latency_s=time.monotonic() - _t0)
                if attempt < max_retries:
                    console.print(
                        f"[yellow]Orchestrator output invalid (attempt {attempt + 1}/{max_retries + 1}): "
                        f"{exc}  — retrying...[/yellow]"
                    )
                    time.sleep(1 + attempt)

        raise RuntimeError(f"Orchestrator failed after {max_retries + 1} attempts: {last_error}") from last_error


class _OpenAICompatOrchestrator:
    """Orchestrator over any OpenAI-compatible chat-completions endpoint.

    Subclasses supply credentials, base_url, and the model fallback chain;
    the call/validate/retry logic is identical across providers.
    """

    def __init__(self, api_key: str, base_url: str, model_chain: list[str],
                 headers: dict | None = None) -> None:
        from openai import OpenAI
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=headers or {},
        )
        self._model_chain = model_chain
        self._system_prompt = ORCHESTRATOR_PROMPT_PATH.read_text(encoding="utf-8")
        self._provider_name = "openai-compat"
        # Only the Gemini subclass opts into quota fail-fast + the run latch; other OpenAI-compat
        # providers (OpenRouter) keep the legacy chain-walk + backoff behaviour untouched.
        self._quota_failfast = False

    def call(self, payload: dict, max_retries: int = 3) -> dict:
        from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
        global _gemini_quota_disabled
        state = payload.get("system_state", "INIT")
        user_message = json.dumps(payload)
        last_error: Exception | None = None

        # Run-level latch: once Gemini's daily quota is exhausted, every later orchestrator call
        # raises immediately so CompositeOrchestrator falls straight to the emergency chain — no
        # re-probe, no retryDelay sleep. Cost-neutral when quota is healthy (flag stays False).
        if self._quota_failfast and GEMINI_QUOTA_FAILFAST and _gemini_quota_disabled:
            raise RuntimeError(
                f"{type(self).__name__} skipped — Gemini quota latched off for this run "
                "(prior RESOURCE_EXHAUSTED)"
            )

        model_chain = self._model_chain
        current_model = model_chain[0]
        model_idx = 0

        for attempt in range(max_retries + 1):
            # Per-build cost circuit-breaker: refuse before spending if the ceiling
            # was crossed. Mirrors the base Orchestrator; placed BEFORE the try so it
            # fails closed (propagates out) instead of being swallowed by the retry
            # handlers below. $0 OAuth/local rungs price to zero so a healthy free
            # build never trips this — only real Anthropic dollars do.
            check_cost_ceiling()
            try:
                _t0 = time.monotonic()
                response = self._client.chat.completions.create(
                    model=current_model,
                    max_tokens=ORCHESTRATOR_MAX_TOKENS,
                    messages=[
                        {"role": "system", "content": self._system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    response_format={"type": "json_object"},
                    timeout=ORCHESTRATOR_TIMEOUT,
                )
                text = response.choices[0].message.content.strip()
                text = _strip_fences(text)
                text = _fix_json_strings(text)
                parsed = json.loads(text)
                validate_response(state, parsed)
                record_role_event(f"orch:{state}", provider=getattr(self, "_provider_name", "openai-compat"),
                                  model=current_model, success=True,
                                  latency_s=time.monotonic() - _t0)
                return parsed

            except (RateLimitError, InternalServerError, APIConnectionError, APITimeoutError) as exc:
                # 429 rate limits, transient server-side failures (503 UNAVAILABLE,
                # 5xx, dropped connections), and timeouts all get the same treatment:
                # try the next fallback model, then back off. Gemini free tier in
                # particular throws intermittent 503s and can hang past the timeout.
                last_error = exc

                # QUOTA-class 429 (daily RESOURCE_EXHAUSTED, not a transient throttle): the outage
                # won't clear within the run, so walking the chain + sleeping the 30-60s retryDelay
                # only stalls the build. Latch Gemini off for the rest of the run and raise NOW so
                # CompositeOrchestrator falls through to the free-first emergency chain on attempt 1.
                if (self._quota_failfast and GEMINI_QUOTA_FAILFAST
                        and isinstance(exc, RateLimitError) and _is_quota_class_429(exc)):
                    _gemini_quota_disabled = True
                    console.print(
                        f"[bold red]Gemini quota exhausted (RESOURCE_EXHAUSTED) — latching it off "
                        f"for this run and failing fast to the emergency chain (no retry sleep).[/bold red]"
                    )
                    raise RuntimeError(
                        f"{type(self).__name__} quota exhausted (quota-class 429): {exc}"
                    ) from exc

                # Transient (non-quota) rate-limit / 5xx / timeout — unchanged:
                # try next fallback model before waiting
                model_idx += 1
                if model_idx < len(model_chain):
                    current_model = model_chain[model_idx]
                    console.print(f"[yellow]Orchestrator unavailable ({type(exc).__name__}) — switching to fallback: {current_model}[/yellow]")
                else:
                    # All models exhausted for this attempt — wait then reset
                    model_idx = 0
                    current_model = model_chain[0]
                    wait = _parse_retry_delay(exc, attempt)
                    console.print(f"[yellow]All orchestrator models unavailable — waiting {wait}s (attempt {attempt + 1}/{max_retries + 1})…[/yellow]")
                    if attempt < max_retries:
                        time.sleep(wait)

            except (json.JSONDecodeError, OrchestratorOutputError) as exc:
                last_error = exc
                record_role_event(f"orch:{state}", provider=getattr(self, "_provider_name", "openai-compat"),
                                  model=current_model, success=False, schema_fail=True,
                                  latency_s=time.monotonic() - _t0)
                if attempt < max_retries:
                    console.print(
                        f"[yellow]Orchestrator output invalid (attempt {attempt + 1}/{max_retries + 1}): "
                        f"{exc}  — retrying...[/yellow]"
                    )
                    time.sleep(2 + attempt)

        raise RuntimeError(
            f"{type(self).__name__} failed after {max_retries + 1} attempts: {last_error}"
        ) from last_error


class OpenRouterOrchestrator(_OpenAICompatOrchestrator):
    def __init__(self) -> None:
        if not OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is not set. Add it to harness/.env.")
        super().__init__(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            model_chain=[ORCHESTRATOR_API_MODEL] + ORCHESTRATOR_FALLBACK_MODELS,
            headers={"X-Title": "J-Claw"},
        )
        self._provider_name = "openrouter"


class GeminiOrchestrator(_OpenAICompatOrchestrator):
    """Gemini via Google's OpenAI-compatible endpoint, called directly so the
    AI Studio free tier applies (routing the same model through OpenRouter bills)."""

    def __init__(self) -> None:
        if not GOOGLE_API_KEY:
            raise RuntimeError("GOOGLE_API_KEY is not set. Add it to harness/.env.")
        super().__init__(
            api_key=GOOGLE_API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            model_chain=[GEMINI_ORCHESTRATOR_MODEL, "gemini-2.5-flash-lite"],
        )
        self._provider_name = "gemini"
        self._quota_failfast = True


class CodexOrchestrator:
    """Free-first ($0 OAuth) orchestrator rung: wraps worker._call_codex + validate_response with
    ONE same-tier retry. Codex CLI has no response_format, so the single retry covers output-
    wrapping / truncation (the same reason worker.planning_call retries Codex once). Draws the
    SHARED OAuth reservation + disable-latch (worker._reserve_oauth_call / _codex_disabled) so it
    never bypasses the run's Codex quota, and is additionally bounded by CODEX_PLANNING_RESERVE so
    a long planning/heal run can't starve worker rescue. Records role telemetry like the other
    orchestrators. Raises RuntimeError (so CompositeOrchestrator falls through) when Codex is
    unavailable / capacity-reserved-out / fails validation on both attempts."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model
        self._system_prompt = ORCHESTRATOR_PROMPT_PATH.read_text(encoding="utf-8")
        self._provider_name = "codex"
        # NB: the per-run planning-call budget lives in the module-level _codex_planning_calls
        # (reset by reset_orchestrator_run()), NOT on the instance — see its definition above.

    def call(self, payload: dict, max_retries: int = 2) -> dict:
        import worker
        state = payload.get("system_state", "INIT")
        user_message = json.dumps(payload)

        def reserve_attempt():
            # Preserve the exact original order: check the planning reserve, THEN reserve shared OAuth
            # capacity, THEN increment — so a failed OAuth reserve doesn't burn a planning slot.
            global _codex_planning_calls
            if _codex_planning_calls >= CODEX_PLANNING_RESERVE:
                raise worker._CodexTierUnavailable(
                    f"CODEX_PLANNING_RESERVE ({CODEX_PLANNING_RESERVE}) exhausted for this run")
            if not worker._reserve_oauth_call("codex"):
                raise worker._CodexTierUnavailable(
                    "Codex latched off or OAuth capacity exhausted")
            _codex_planning_calls += 1

        # Delegate the Codex protocol (reserve, one retry, parse, validate, latch, telemetry) to the
        # shared worker._codex_tier; convert its outcomes into the RuntimeError CompositeOrchestrator
        # expects so the emergency chain falls through to the next (paid) rung.
        try:
            return worker._codex_tier(
                self._system_prompt, user_message,
                lambda parsed: validate_response(state, parsed),
                role=f"orch:{state}", model=self._model, reserve_attempt=reserve_attempt)
        except worker._CodexTierUnavailable as exc:
            raise RuntimeError(f"CodexOrchestrator unavailable: {exc}") from exc
        except worker._CodexTierInvalid as exc:
            raise RuntimeError(f"CodexOrchestrator failed validation: {exc}") from exc


class ClaudeCliOrchestrator:
    """Free-first ($0 OAuth) orchestrator rung backed by the operator's Claude Max subscription via
    `claude -p`. Wraps worker._claude_cli_tier (reserve → one same-tier retry → parse → validate →
    latch → telemetry), the same protocol CodexOrchestrator uses.

    Slotted in the emergency chain AFTER CodexOrchestrator and BEFORE the metered Anthropic rungs, so
    planning/heal re-plans get a SECOND $0 attempt before any spend. It goes after Codex (not first)
    because the Max pool is SHARED with the operator's interactive Claude Code AND the worker
    claude_cli rung, whereas Codex's ChatGPT sub is independent; CLAUDE_CLI_MAX_CALLS caps the TOTAL
    Max use per run across all three, so this rung is deliberately conservative. Raises RuntimeError
    (so CompositeOrchestrator falls through to the paid rungs) when Claude CLI is unavailable /
    capacity-reserved-out / fails validation on both attempts."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model
        self._system_prompt = ORCHESTRATOR_PROMPT_PATH.read_text(encoding="utf-8")
        self._provider_name = "claude_cli"

    def call(self, payload: dict, max_retries: int = 2) -> dict:
        import worker
        state = payload.get("system_state", "INIT")
        user_message = json.dumps(payload)
        # Delegate the claude_cli protocol (reserve, one retry, parse, validate, latch, telemetry) to
        # the shared worker._claude_cli_tier; convert its outcomes into the RuntimeError that
        # CompositeOrchestrator expects so the chain falls through to the next (paid) rung. The shared
        # CLAUDE_CLI_MAX_CALLS cap (enforced inside the default reserve) bounds Max use, so no separate
        # planning reserve is needed (unlike Codex's CODEX_PLANNING_RESERVE).
        try:
            return worker._claude_cli_tier(
                self._system_prompt, user_message,
                lambda parsed: validate_response(state, parsed),
                role=f"orch:{state}", model=self._model)
        except worker._CodexTierUnavailable as exc:
            raise RuntimeError(f"ClaudeCliOrchestrator unavailable: {exc}") from exc
        except worker._CodexTierInvalid as exc:
            raise RuntimeError(f"ClaudeCliOrchestrator failed validation: {exc}") from exc


class CompositeOrchestrator:
    """Wraps a primary orchestrator with an emergency cross-provider fallback.

    When the primary exhausts all its retries and raises RuntimeError, we log
    a loud warning and route the same payload through a secondary provider at
    the same capability tier (Sonnet, not Opus — orchestrator work is
    planning/JSON, not capability-bound; Opus would just multiply cost during
    outages when EVERY call hits the emergency rung).

    Availability failures go sideways to another provider (this class).
    Capability failures escalate up the worker ladder (PR #36 — separate concern).

    The emergency is an ORDERED LIST of fallbacks, tried free-before-paid (Codex $0 → Sonnet →
    Opus): each rung is attempted in turn and the first whose .call() returns (i.e. validated)
    wins. Backward-compatible with the legacy 2-arg (primary, single-emergency) construction.
    """

    def __init__(self, primary, emergency) -> None:
        self._primary = primary
        # Accept either a single emergency orchestrator (legacy) or an ordered list of fallbacks.
        if isinstance(emergency, (list, tuple)):
            self._emergency_chain = list(emergency)
        else:
            self._emergency_chain = [emergency]

    def call(self, payload: dict, max_retries: int = 2) -> dict:
        try:
            return self._primary.call(payload, max_retries=max_retries)
        except RuntimeError as exc:
            # Do NOT record a telemetry event here: each emergency orchestrator's own .call()
            # records the real attempt (success/schema_fail + latency) under the same
            # orch:<state> role. A pre-call record_role_event would phantom-success on emergency
            # failure and double-count on success (Codex review fix). The primary's recorded
            # schema_fails already signal that a fallback occurred.
            console.print(
                f"\n[bold red]EMERGENCY: Primary orchestrator exhausted all retries — "
                f"walking the free-first fallback chain "
                f"({' → '.join(type(e).__name__ for e in self._emergency_chain)})[/bold red]\n"
                f"  Primary failure: {exc}\n"
            )
            last_error: Exception = exc
            for rung in self._emergency_chain:
                try:
                    return rung.call(payload, max_retries=max_retries)
                except Exception as rung_exc:  # noqa: BLE001 — try the next cheaper-to-costlier rung
                    last_error = rung_exc
                    console.print(
                        f"[yellow]Emergency rung {type(rung).__name__} failed — "
                        f"trying next fallback: {rung_exc}[/yellow]"
                    )
            raise RuntimeError(
                f"All orchestrator fallbacks exhausted: {last_error}"
            ) from last_error


def _emergency_chain() -> list:
    """Build the free-first ordered emergency fallback chain: Codex ($0 OAuth) → Claude Max ($0 OAuth)
    → Sonnet (paid) → Opus (paid). Each $0 OAuth rung is included only when enabled; the paid Anthropic
    rungs only when ORCHESTRATOR_EMERGENCY_PROVIDER is anthropic and a key is present. Claude Max goes
    AFTER Codex (Codex's sub is independent; Max is shared with interactive use). Grok is NOT in the
    chain — it's evidence-gated per the plan (added only if a shadow test proves valid orch JSON).
    """
    chain: list = []
    try:
        import worker
        if worker._oauth_enabled("codex"):
            chain.append(CodexOrchestrator())
        # Second $0 tier before any spend: Claude Max via `claude -p`, after Codex.
        if worker._oauth_enabled("claude_cli"):
            chain.append(ClaudeCliOrchestrator())
    except Exception:  # noqa: BLE001 — worker import/flag issues must not break orchestrator setup
        pass
    if ORCHESTRATOR_EMERGENCY_PROVIDER == "anthropic" and ANTHROPIC_API_KEY and PAID_ORCH_ENABLED:
        # Sonnet first (the existing emergency tier), then Opus as the costliest last resort.
        # Gated by PAID_ORCH_ENABLED: on a $0-credit box the metered rungs would only refuse-at-call,
        # so omit them entirely and let the chain be free-only (fails closed when free rungs exhaust).
        chain.append(Orchestrator(model=EMERGENCY_ORCHESTRATOR_MODEL))
        chain.append(Orchestrator(model=OPUS_MODEL))
    return chain


class _FailClosedOrchestrator:
    """A terminal no-op rung returned by make_orchestrator when NO usable rung exists — i.e. paid
    orchestration is disabled (PAID_ORCH_ENABLED=false) AND no free OAuth rung is enabled. Its .call()
    raises RuntimeError so the build fails CLOSED with a clear, actionable message and the standard
    failure-handoff path (main._write_failure_handoff) — never a raw anthropic credit-balance traceback.
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def call(self, payload: dict, max_retries: int = 2) -> dict:
        raise RuntimeError(f"No usable orchestrator rung: {self._reason}")


def _free_only_or_failclosed(reason: str):
    """Build a free-OAuth-only orchestrator (Codex $0 → Claude Max $0), or a _FailClosedOrchestrator
    when no free rung is enabled. Used by the paid-disabled fall-throughs in make_orchestrator so the
    None/simple/medium/complex paths still orchestrate on the subscription rungs and otherwise fail
    closed honestly instead of returning a bare paid Orchestrator that can only refuse-at-call."""
    free: list = []
    try:
        import worker
        if worker._oauth_enabled("codex"):
            free.append(CodexOrchestrator())
        if worker._oauth_enabled("claude_cli"):
            free.append(ClaudeCliOrchestrator())
    except Exception:  # noqa: BLE001 — worker import/flag issues must not break orchestrator setup
        pass
    if not free:
        return _FailClosedOrchestrator(reason)
    if len(free) == 1:
        return free[0]
    return CompositeOrchestrator(free[0], free[1:])


def make_orchestrator(provider: str | None = None, *, manual: bool = False,
                      difficulty: str | None = None):
    """Factory that returns the right orchestrator for ORCHESTRATOR_PROVIDER,
    wrapped with an emergency fallback when configured.

    difficulty (Phase 4): "simple" | "medium" | "complex" | None
      Only applies when provider is "anthropic" (the default). Selects the cost/capability
      trade-off appropriate for the project scale derived from the creative brief:
        - "simple"  → Haiku (cheapest) with Codex emergency chain
        - "medium"  → CodexOrchestrator primary → Sonnet emergency (Codex-first, paid fallback)
        - "complex" → Sonnet → Opus chain (full capability, highest cost)
        - None      → existing behaviour (plain Orchestrator() on Sonnet)
      Non-anthropic providers (gemini, openrouter) ignore difficulty and use their own routing.
    """
    from config import ORCHESTRATOR_PROVIDER
    p = provider or ORCHESTRATOR_PROVIDER

    if manual:
        return ManualOrchestrator()

    if p == "gemini":
        primary = GeminiOrchestrator()
        chain = _emergency_chain()
        if chain:
            return CompositeOrchestrator(primary, chain)
        return primary

    if p == "openrouter":
        return OpenRouterOrchestrator()

    # ── Anthropic provider with difficulty routing (Phase 4) ─────────────────
    if difficulty == "simple":
        # Prototype builds: Haiku is sufficient for JSON orchestration at this scale.
        # Emergency chain (Codex $0 → Sonnet → Opus) handles Haiku outages.
        if PAID_ORCH_ENABLED and ANTHROPIC_API_KEY:
            primary = Orchestrator(model=HAIKU_MODEL)
            chain = _emergency_chain()
            if chain:
                return CompositeOrchestrator(primary, chain)
            return primary
        # Paid disabled: drop the paid Haiku primary and lead with the free OAuth rungs.
        return _free_only_or_failclosed(
            "simple build: paid orchestrator disabled (PAID_ORCH_ENABLED=false) and no free OAuth rung enabled")

    if difficulty == "medium":
        # MVP builds: free-first ($0 OAuth) primary with a paid Anthropic backstop. Mirrors the
        # planning_call ladder — the cheapest reliable tier handles the common case, Anthropic is the
        # backstop. Both $0 rungs (Codex, then Claude Max) lead; the first enabled is the primary and
        # any remaining free rung sits ahead of the paid Sonnet→Opus fallback.
        try:
            import worker
            free_rungs = []
            if worker._oauth_enabled("codex"):
                free_rungs.append(CodexOrchestrator())
            if worker._oauth_enabled("claude_cli"):
                free_rungs.append(ClaudeCliOrchestrator())
            if free_rungs:
                paid_fallback = ([Orchestrator(model=EMERGENCY_ORCHESTRATOR_MODEL),
                                  Orchestrator(model=OPUS_MODEL)]
                                 if (PAID_ORCH_ENABLED and ANTHROPIC_API_KEY) else [])
                return CompositeOrchestrator(free_rungs[0], free_rungs[1:] + paid_fallback)
        except Exception:  # noqa: BLE001 — worker import issues fall through to Sonnet
            pass
        # No $0 OAuth rung enabled: fall through to paid Sonnet, or fail closed if paid is disabled.
        if PAID_ORCH_ENABLED and ANTHROPIC_API_KEY:
            return Orchestrator()
        return _free_only_or_failclosed(
            "medium build: paid orchestrator disabled (PAID_ORCH_ENABLED=false) and no free OAuth rung enabled")

    if difficulty == "complex":
        # Production builds: maximum capability, but STILL free-first (operator directive
        # 2026-06-19 — "paid only when free is unavailable"). Lead with the $0 OAuth rungs
        # (Codex, then Claude Max) and fall back to the paid Sonnet→Opus ladder ONLY when every
        # free rung is exhausted/unavailable. Previously this branch went straight to paid Sonnet
        # with no free rung, which silently bypassed available $0 orchestration during heal loops.
        try:
            import worker
            free_rungs = []
            if worker._oauth_enabled("codex"):
                free_rungs.append(CodexOrchestrator())
            if worker._oauth_enabled("claude_cli"):
                free_rungs.append(ClaudeCliOrchestrator())
            if free_rungs:
                paid_fallback = ([Orchestrator(model=ORCHESTRATOR_MODEL),
                                  Orchestrator(model=OPUS_MODEL)]
                                 if (PAID_ORCH_ENABLED and ANTHROPIC_API_KEY) else [])
                return CompositeOrchestrator(free_rungs[0], free_rungs[1:] + paid_fallback)
        except Exception:  # noqa: BLE001 — worker import issues fall through to the paid ladder
            pass
        if PAID_ORCH_ENABLED and ANTHROPIC_API_KEY:
            sonnet = Orchestrator(model=ORCHESTRATOR_MODEL)
            opus_fallback = Orchestrator(model=OPUS_MODEL)
            return CompositeOrchestrator(sonnet, [opus_fallback])
        return _free_only_or_failclosed(
            "complex build: paid orchestrator disabled (PAID_ORCH_ENABLED=false) and no free OAuth rung enabled")

    # anthropic default (difficulty=None): paid Sonnet when enabled, else free-first / fail closed.
    # The legacy bare Orchestrator() here was paid-only — a latent bypass of available $0 rungs; the
    # paid-disabled branch now routes the CONTINUE path (_continue_run calls make_orchestrator()) and
    # any None-difficulty caller onto the free OAuth rungs.
    if PAID_ORCH_ENABLED and ANTHROPIC_API_KEY:
        return Orchestrator()
    return _free_only_or_failclosed(
        "default orchestrator: paid orchestrator disabled (PAID_ORCH_ENABLED=false) and no free OAuth rung enabled")


def _parse_retry_delay(exc: Exception, attempt: int) -> int:
    """Extract the server-recommended retry delay from a rate-limit / quota error.

    Handles three shapes in order of preference:
      1. Google / Gemini: error.details[].@type == RetryInfo with retryDelay "Ns"
      2. OpenRouter: error.metadata.retry_after_seconds (integer)
      3. Fallback: plain-text "retry in N" or "retry in N.Ms" anywhere in the message
      4. Blind default: 35 * (attempt + 1) seconds
    """
    import re
    try:
        body = exc.response.json()
        err = body.get("error", {})
        # Shape 1: Google RetryInfo (details array)
        for detail in err.get("details", []):
            if "RetryInfo" in detail.get("@type", ""):
                delay_str = detail.get("retryDelay", "")
                m = re.match(r"(\d+(?:\.\d+)?)", delay_str)
                if m:
                    return max(2, int(float(m.group(1))) + 2)
        # Shape 2: OpenRouter metadata
        val = err.get("metadata", {}).get("retry_after_seconds")
        if val is not None:
            return int(val) + 2
    except Exception:
        pass
    # Shape 3: plain-text in the exception message
    m = re.search(r"retry in (\d+(?:\.\d+)?)", str(exc), re.IGNORECASE)
    if m:
        return max(2, int(float(m.group(1))) + 2)
    # Shape 4: blind default — scale with attempt so repeated failures back off
    return 35 * (attempt + 1)


def _sanitize(text: str) -> str:
    """Strip control characters that Notepad or copy-paste can silently insert."""
    import re
    # Keep only tab (\x09), newline (\x0a), carriage return (\x0d), and printable chars
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def _fix_json_strings(text: str) -> str:
    """Replace literal newlines/tabs inside JSON string values that break json.loads."""
    result = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == '\\' and in_string:
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif in_string and ch == '\n':
            result.append('\\n')
        elif in_string and ch == '\r':
            pass  # strip bare CR
        elif in_string and ch == '\t':
            result.append('\\t')
        else:
            result.append(ch)
    return ''.join(result)


def _strip_fences(text: str) -> str:
    """Remove accidental ```json ... ``` wrapping that the model sometimes adds."""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    # drop first line (```json or ```) and last line (```)
    inner = lines[1:] if lines[-1].strip() == "```" else lines[1:]
    if inner and inner[-1].strip() == "```":
        inner = inner[:-1]
    return "\n".join(inner).strip()
