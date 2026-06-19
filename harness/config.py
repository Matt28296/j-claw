from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _float_env(name: str, default: float, lo: float | None = None) -> float:
    """Parse a float env var, falling back to ``default`` on empty/non-numeric
    values instead of crashing the harness at import (bare ``float(os.getenv())``
    raises ValueError on ``NAME=`` or ``NAME=abc``). When ``lo`` is given the
    result is clamped UP to that floor so a malformed/too-low override can't
    disable a guard (e.g. a depth that must stay >= 1)."""
    raw = os.getenv(name)
    try:
        val = float(raw) if raw is not None and raw.strip() != "" else default
    except (ValueError, TypeError):
        val = default
    if lo is not None and val < lo:
        val = lo
    return val


def _int_env(name: str, default: int, lo: int | None = None) -> int:
    """Integer counterpart of ``_float_env`` — same try/except + floor clamp.
    Used for caps/depths so ``NAME=`` (empty) or ``NAME=abc`` can't crash import
    and a sub-floor override (e.g. ``MAX_FORMAT5_DEPTH=0``) can't silently
    disable the feature it bounds."""
    raw = os.getenv(name)
    try:
        val = int(raw) if raw is not None and raw.strip() != "" else default
    except (ValueError, TypeError):
        val = default
    if lo is not None and val < lo:
        val = lo
    return val


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

WORKER_PROVIDER: str = os.getenv("WORKER_PROVIDER", "ollama")  # "ollama" | "openrouter"
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
        "ollama::qwen3:8b,ollama::qwen2.5-coder:14b,grok::grok-build,codex::gpt-5.5,anthropic::claude-sonnet-4-6",
    )
)

# Hard cap on the number of PAID (non-ollama) worker calls allowed per project run. Escalation
# to a cloud rung is gated by this budget; once spent, tasks clamp to the strongest local rung
# instead of paying. Prevents a multi-task project from silently burning the API budget.
MAX_PAID_WORKER_CALLS: int = _int_env("MAX_PAID_WORKER_CALLS", 15, lo=0)

# Hard cap on PAID (metered Anthropic) ORCHESTRATION / PLANNING calls per project run — the
# control-plane analog of MAX_PAID_WORKER_CALLS. Bounds the paid orchestrator emergency-fallback
# rungs (Sonnet→Opus in orchestrator.Orchestrator.call) AND the planning_call Anthropic tiers
# (Creative Director / Technical Architect / orchestrator). These had NO call-count cap before:
# in the 2026-06-19 D1 take-2 run, both free orchestrator rungs latched off and every heal re-plan
# fell through to paid Sonnet — 38 paid calls / $0.81 on a build that should have stayed $0. Once
# spent, paid orchestration is refused (fails closed); the latch fix keeps the free rungs up so this
# ceiling is a belt-and-suspenders backstop, not the primary defense. 0 = disabled (no cap).
MAX_PAID_ORCH_CALLS: int = _int_env("MAX_PAID_ORCH_CALLS", 12, lo=0)

# Per-build HARD cost ceiling (USD). When cumulative METERED spend (cost._total_usd, which
# counts only paid Anthropic dollars — $0 OAuth/local rungs never count) reaches this, the
# build FAILS CLOSED: the next metered call is refused (cost.BuildCostCeilingExceeded) and the
# run halts with a logged failure-handoff. This is the unattended-spend circuit-breaker that
# stops a hung/looping build from silently draining the API budget. 0 = disabled (no ceiling).
MAX_BUILD_COST_USD: float = _float_env("MAX_BUILD_COST_USD", 5.0, lo=0)
# Soft warning threshold as a fraction of the ceiling — logged once, does NOT halt. 0 = no warn.
BUILD_COST_WARN_FRAC: float = _float_env("BUILD_COST_WARN_FRAC", 0.75, lo=0)
# Optional absolute metered-token backstop (input+output), independent of pricing drift.
# 0 = disabled (rely on the USD ceiling).
MAX_BUILD_TOKENS: int = _int_env("MAX_BUILD_TOKENS", 0, lo=0)

# Codex CLI worker rung — a flat-rate OAuth (ChatGPT Plus/Pro) escalation tier that sits
# ABOVE local Ollama and BELOW Anthropic in WORKER_LADDER. Because it bills against a
# subscription rather than per token, escalations that would otherwise cost Anthropic dollars
# are caught here for free first; Anthropic becomes the true last resort. Codex calls are
# shelled out to `codex exec` in non-interactive mode (see worker._call_codex).
CODEX_CLI_ENABLED: bool = os.getenv("CODEX_CLI_ENABLED", "false").lower() == "true"
CODEX_HOME: str = os.getenv("CODEX_HOME", "")          # empty = use codex's own default (~/.codex)
CODEX_MODEL: str = os.getenv("CODEX_MODEL", "gpt-5.5")
CODEX_EFFORT: str = os.getenv("CODEX_EFFORT", "")      # empty = don't override codex's configured reasoning effort
# Per-run capacity cap. NOT a dollar budget — Codex is flat-rate. This protects the
# subscription's flat-rate rate-limit window so a multi-task run can't exhaust the quota and
# trip an interactive reauth (which would hang the build).
CODEX_CLI_MAX_CALLS: int = _int_env("CODEX_CLI_MAX_CALLS", 20, lo=0)
CODEX_TIMEOUT: int = _int_env("CODEX_TIMEOUT", 300, lo=0)  # seconds per codex exec subprocess

# Grok Build CLI worker rung — a second flat-rate OAuth (SuperGrok / X Premium+) escalation tier
# that sits in WORKER_LADDER ABOVE local Ollama and BELOW Codex. Grok-first ordering: grok-build
# is the abundant, weaker, $0 first-line rescue, so Codex's scarcer per-run capacity is preserved
# for the harder tasks Grok fails. Headless `grok -p` authenticates via the cached OAuth token in
# ~/.grok/auth.json — NO metered xAI API key — so calls are $0 marginal (live-probe confirmed).
# Shelled out in non-interactive mode (see worker._call_grok).
GROK_CLI_ENABLED: bool = os.getenv("GROK_CLI_ENABLED", "false").lower() == "true"
GROK_HOME: str = os.getenv("GROK_HOME", "")            # empty = use grok's own default (~/.grok)
GROK_MODEL: str = os.getenv("GROK_MODEL", "grok-build")  # agentic coding model (vs default grok-composer)
# Per-run capacity cap (build-safety bound), NOT a dollar budget — Grok is flat-rate. The plan's
# true rolling DAILY quota (survives process restarts) is deferred; this in-memory counter resets
# each run via reset_paid_budget(), sized conservatively below the subscription's daily quota.
GROK_MAX_CALLS: int = _int_env("GROK_MAX_CALLS", 40, lo=0)
GROK_TIMEOUT: int = _int_env("GROK_TIMEOUT", 300, lo=0)  # seconds per grok -p subprocess

# Claude (Max subscription) CLI worker rung — a third flat-rate OAuth escalation tier. Headless
# `claude -p` runs the same Sonnet/Opus models you'd otherwise reach via the metered Anthropic API,
# but billed against the operator's Claude Max subscription ($0 marginal). It sits in WORKER_LADDER
# ABOVE the metered `anthropic` rungs so Claude-tier escalations are caught for free first, with the
# paid API rung kept only as the overflow safety net. CRITICAL OPERATIONAL NOTE: unlike Codex/Grok
# (separate subscriptions), this draws on the SAME Max usage pool as the operator's interactive
# Claude Code — a heavy build can throttle interactive use and vice-versa. So it's placed BELOW
# Codex/Grok and its per-run cap defaults LOW. Shelled out in non-interactive mode (see
# worker._call_claude_cli). Ships INERT (disabled by default); `claude -p` is the Claude Code AGENT,
# not a bare model call, so it is constrained hard (no tools, MCP off, safe-mode, worker system
# prompt via --system-prompt-file) and its credentials env is scrubbed so it uses the subscription.
#
# LIVE-VALIDATION CHECKLIST (mirrors how the Codex rung was validated in PR #84, which found a
# live-only bug — and this one found that --safe-mode is rejected by claude 2.1.179):
#   1. AUTH — ✓ smoke-tested 2026-06-17: with ANTHROPIC_API_KEY scrubbed the call still succeeded
#      (returncode 0), i.e. it used the subscription OAuth, not the metered API. (Claude Code still
#      PRINTS an informational total_cost_usd estimate even on a subscription session — confirm on
#      the Max usage dashboard that no metered API spend appears.)
#   2. CONTRACT — ✓ smoke-tested 2026-06-17: with --tools "" + --setting-sources "" + the worker
#      --system-prompt-file it returned a clean {"files":[...]} object, is_error=false, num_turns=1
#      (no agent loop, no tool attempts, no markdown fences).
#   3. LATCH — NOT exercised live (can't force a usage-limit on demand); classifier is unit-tested
#      only. Confirm in a real run that a usage-limit response trips _claude_cli_disabled.
#   4. ToS — operator decision: confirm this scale of automated subscription use is acceptable under
#      Anthropic's current Consumer Terms (a personal Max sub powering an automated build farm is a
#      risk boundary — for team/commercial use prefer Team/Enterprise or Console API billing).
# Still ships INERT: enable + add to WORKER_LADDER only after you accept #4 and have seen #3 in a run.
CLAUDE_CLI_ENABLED: bool = os.getenv("CLAUDE_CLI_ENABLED", "false").lower() == "true"
CLAUDE_CLI_HOME: str = os.getenv("CLAUDE_CLI_HOME", "")  # empty = use claude's default config dir (sets CLAUDE_CONFIG_DIR)
CLAUDE_CLI_MODEL: str = os.getenv("CLAUDE_CLI_MODEL", "sonnet")  # alias the Claude CLI accepts (sonnet|opus|haiku) or a full id
# Per-run capacity cap — deliberately LOW because this shares the operator's interactive Max quota.
CLAUDE_CLI_MAX_CALLS: int = _int_env("CLAUDE_CLI_MAX_CALLS", 10, lo=0)
CLAUDE_CLI_TIMEOUT: int = _int_env("CLAUDE_CLI_TIMEOUT", 300, lo=0)  # seconds per claude -p subprocess

# How many subprocess TIMEOUTS an OAuth rung may incur before it latches off for the rest of the run.
# A real auth/quota/rate-limit/exe failure latches IMMEDIATELY (re-probing within a run is pointless),
# but a subprocess timeout is transient — often the shared Max pool being momentarily busy with the
# operator's own interactive session. A single transient stall must NOT take a free rung out for the
# whole build and cascade downstream work onto paid. Counter is per-provider and resets on any success
# (consecutive timeouts), so scattered stalls across a long build never accumulate into a latch.
# Set to 1 to restore the old "latch on the first timeout" behavior.
OAUTH_TIMEOUT_LATCH_THRESHOLD: int = _int_env("OAUTH_TIMEOUT_LATCH_THRESHOLD", 2, lo=1)

# Provider-class sets that make the worker's budget logic declarative. METERED providers bill
# per token and consume the dollar budget MAX_PAID_WORKER_CALLS; OAUTH providers are flat-rate
# subscriptions that consume a SEPARATE per-run capacity counter (CODEX_CLI_MAX_CALLS) instead,
# so they never draw down the dollar budget but are still bounded against quota exhaustion.
METERED_PROVIDERS: set[str] = {"anthropic", "openrouter"}
OAUTH_PROVIDERS: set[str] = {"codex", "grok", "claude_cli"}

# Modeled rate-limit windows (seconds) used ONLY by the dashboard's rung-status countdown, and ONLY
# for a subscription-cap-class latch (rate_limit / usage / quota). When an OAuth rung latches off
# for that reason, the dashboard estimates "back in action" as disabled_at + this window. It is an
# ESTIMATE (the providers don't publish an exact reset epoch over the CLI), surfaced as "~est." — it
# does NOT auto-re-enable the rung in-process (the latch still holds until the next run's
# reset_paid_budget()); it tells the operator when a fresh run could expect the rung back. Auth /
# exe-missing / per-run-capacity latches get NO countdown (different, non-time-based states). Codex
# (ChatGPT Plus) and Claude Max both enforce ~5h rolling windows; SuperGrok's is shorter.
OAUTH_RATE_WINDOW_S: dict[str, int] = {
    "codex": _int_env("CODEX_RATE_WINDOW_S", 18000, lo=0),       # ~5h ChatGPT Plus rolling window
    "grok": _int_env("GROK_RATE_WINDOW_S", 7200, lo=0),          # ~2h SuperGrok (shorter window)
    "claude_cli": _int_env("CLAUDE_CLI_RATE_WINDOW_S", 18000, lo=0),  # ~5h Claude Max rolling window
}

# Maximum tasks to run in parallel (1 = sequential, 2-4 = parallel)
MAX_PARALLEL_WORKERS: int = _int_env("MAX_PARALLEL_WORKERS", 4, lo=1)

# Task types that prefer local Ollama (simple, low-token tasks)
# Everything else prefers cloud (complex logic, multi-file, reasoning-heavy)
LOCAL_FIRST_TASK_TYPES: set[str] = set(
    os.getenv("LOCAL_FIRST_TASK_TYPES", "scaffold,style,data,config").split(",")
)

# Phase 4 (minimal slice): start integration-heavy tasks — a non-trivial type that
# DEPENDS on sibling modules — at the first $0 OAuth cloud rung (grok) instead of the
# weak local model. Gate 3 showed the local model cannot hold cross-module interface
# contracts (wrong import paths, function-name drift, prose-instead-of-JSON), so it
# only burns heal cycles before escalating anyway. OAuth rungs are $0, so this skips
# the doomed first attempt at no dollar cost. Standalone (no-dep) code tasks stay
# local — the model handles isolated files fine. Toggle off to restore strict
# local-first base routing.
INTEGRATION_FIRST_ROUTING: bool = os.getenv("INTEGRATION_FIRST_ROUTING", "true").lower() == "true"

ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")
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
# Explicit checkpoint override — when set, used for ALL styles (auto-detect if empty).
COMFYUI_CHECKPOINT: str = os.getenv("COMFYUI_CHECKPOINT", "")
# Style-aware checkpoints: the asset worker detects whether the brief wants a
# realistic or anime/cartoon look and selects the matching model. Realistic is
# the default when the style is ambiguous.
COMFYUI_CHECKPOINT_REALISTIC: str = os.getenv(
    "COMFYUI_CHECKPOINT_REALISTIC", "RealVisXL_V5.0_fp16.safetensors"
)
COMFYUI_CHECKPOINT_ANIME: str = os.getenv(
    "COMFYUI_CHECKPOINT_ANIME", "animagine-xl-3.1.safetensors"
)
COMFYUI_WIDTH: int = _int_env("COMFYUI_WIDTH", 768, lo=1)
COMFYUI_HEIGHT: int = _int_env("COMFYUI_HEIGHT", 512, lo=1)
# Sampling quality knobs (dpmpp_2m + karras gives better detail than euler at no
# extra cost; steps trade quality for CPU render time).
COMFYUI_STEPS: int = _int_env("COMFYUI_STEPS", 26, lo=1)
COMFYUI_SAMPLER: str = os.getenv("COMFYUI_SAMPLER", "dpmpp_2m")
COMFYUI_SCHEDULER: str = os.getenv("COMFYUI_SCHEDULER", "karras")
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
DEPLOY_TIMEOUT: int = _int_env("DEPLOY_TIMEOUT", 120, lo=0)  # seconds
PROJECTS_DIR: Path = Path(os.getenv("PROJECTS_DIR", "./projects"))
MAX_RETRIES_PER_TASK: int = _int_env("MAX_RETRIES_PER_TASK", 3, lo=0)
MAX_TASKS: int = _int_env("MAX_TASKS", 75, lo=1)
ORCHESTRATOR_MAX_TOKENS: int = _int_env("ORCHESTRATOR_MAX_TOKENS", 16384, lo=1)
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
# Last-resort Opus model id, shared by the emergency orchestrator chain and planning_call's Opus
# tier so a model-id roll is a single config change, not a hunt for hardcoded literals.
OPUS_MODEL: str = os.getenv("OPUS_MODEL", "claude-opus-4-8")

# Gemini free-tier quota fail-fast (orchestrator). When True, a QUOTA-CLASS 429 from the Gemini
# orchestrator (RESOURCE_EXHAUSTED / daily-limit, distinct from a transient per-minute rate-limit)
# raises immediately instead of walking the model chain + sleeping the parsed retryDelay, and sets
# a module-level run latch so every subsequent orchestrator call in the run skips Gemini and falls
# straight to the emergency chain. Set False to restore the old retry-on-every-call behaviour.
GEMINI_QUOTA_FAILFAST: bool = os.getenv("GEMINI_QUOTA_FAILFAST", "true").lower() == "true"

# Per-run cap on the number of OAuth Codex calls the ORCHESTRATOR emergency rung may consume,
# carved out of the shared CODEX_CLI_MAX_CALLS capacity so a long planning/heal run can't starve
# worker rescue of Codex. The CodexOrchestrator stops drawing Codex once this many orchestrator
# Codex calls have been made in the run and falls through to paid Sonnet/Opus instead.
CODEX_PLANNING_RESERVE: int = _int_env("CODEX_PLANNING_RESERVE", 6, lo=0)

# Hard per-run sub-cap for WORKER RESCUE Codex calls: the capacity left in the Codex budget
# after the orchestrator planning reserve is carved out. This is a computed constant (not an
# env-var override) so the two reserves always sum to exactly CODEX_CLI_MAX_CALLS. No lending:
# a planning overflow routes to Sonnet (not the worker budget); a worker overflow routes to
# Sonnet (not the planning budget). The outer CODEX_CLI_MAX_CALLS bound still applies.
CODEX_WORKER_RESERVE: int = max(0, CODEX_CLI_MAX_CALLS - CODEX_PLANNING_RESERVE)

# Model id for the cheapest Anthropic orchestrator tier (difficulty="simple" routing).
# Haiku is used when the creative brief signals a low-complexity prototype build where a
# cheaper model is sufficient for planning/JSON orchestration. Override via HAIKU_MODEL.
HAIKU_MODEL: str = os.getenv("HAIKU_MODEL", "claude-haiku-4-5-20251001")
CREATIVE_DIRECTOR_PROMPT_PATH: Path = Path(__file__).parent.parent / "creative_director.txt"
# Floored at 1: MAX_FORMAT5_DEPTH=0 would make _subproject_decomposition_allowed(0)
# compute 0 < 0 == False, silently disabling ALL decomposition (top-level included).
MAX_FORMAT5_DEPTH: int = _int_env("MAX_FORMAT5_DEPTH", 3, lo=1)
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EXPERIENCE_LOG: str = os.getenv("EXPERIENCE_LOG", "experience.jsonl")

# Path to the orchestrator system prompt (one level up from this file)
ORCHESTRATOR_PROMPT_PATH: Path = Path(__file__).parent.parent / "orchestrator.txt"

TECHNICAL_ARCHITECT_ENABLED: bool = os.getenv("TECHNICAL_ARCHITECT_ENABLED", "true") == "true"
TECHNICAL_ARCHITECT_MODEL: str = os.getenv("TECHNICAL_ARCHITECT_MODEL", "claude-haiku-4-5-20251001")
TECHNICAL_ARCHITECT_PROMPT_PATH: Path = Path(__file__).parent.parent / "technical_architect.txt"
DASHBOARD_PORT: int = _int_env("DASHBOARD_PORT", 8765, lo=0)
DASHBOARD_AUTOOPEN: bool = os.getenv("DASHBOARD_AUTOOPEN", "true") == "true"
GODOT_PATH: str = os.getenv("GODOT_PATH", "godot")
PINATA_API_KEY: str = os.getenv("PINATA_API_KEY", "")
PINATA_SECRET_KEY: str = os.getenv("PINATA_SECRET_KEY", "")
IPFS_AUTO_PIN: bool = os.getenv("IPFS_AUTO_PIN", "false").lower() == "true"

# Pipeline resilience
PIPELINE_MAX_RETRIES: int = _int_env("PIPELINE_MAX_RETRIES", 2, lo=0)
HEAL_MAX_CYCLES: int = _int_env("HEAL_MAX_CYCLES", 3, lo=0)
ORCHESTRATOR_TIMEOUT: int = _int_env("ORCHESTRATOR_TIMEOUT", 300, lo=0)   # seconds per API call
WORKER_TASK_TIMEOUT: int = _int_env("WORKER_TASK_TIMEOUT", 600, lo=0)     # seconds per task batch
