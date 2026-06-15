#!/usr/bin/env python3
"""Per-run Anthropic cost accounting.

Sits on top of the same `response.usage` objects that cache_telemetry reads.
Each paid Claude call records its tokens here; at the end of a build the
accumulator yields a dollar estimate broken down by label and model.

Pricing is per 1M tokens (cached 2026-05-26). Cache reads bill at ~0.1x the
input rate; 5-minute cache writes at ~1.25x. Output bills at the output rate.
Local (ollama) workers cost nothing and are simply skipped.

Pure (no I/O beyond an optional summary string). Module-level accumulator,
mirroring worker.reset_paid_budget(): call reset_costs() at run start.
Self-test: PYTHONUTF8=1 python harness/cost.py
"""
from __future__ import annotations

# (input_per_mtok, output_per_mtok) keyed by a substring of the model id.
_PRICING: dict[str, tuple[float, float]] = {
    "opus":   (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku":  (1.0,  5.0),
}
_CACHE_READ_MULT = 0.1     # cache hit: ~0.1x input rate
_CACHE_WRITE_MULT = 1.25   # 5-minute ephemeral write: ~1.25x input rate

_MTOK = 1_000_000.0


def _family(model: str | None) -> str | None:
    """Pricing family ('haiku'/'sonnet'/'opus') for a model id, or None if not a
    paid Anthropic model (local/ollama/unknown → no cost)."""
    if not model:
        return None
    m = model.lower()
    for key in _PRICING:
        if key in m:
            return key
    return None


def _rates(model: str | None) -> tuple[float, float] | None:
    """Return (input_rate, output_rate) for a model id, or None if not billable."""
    fam = _family(model)
    return _PRICING[fam] if fam else None


# ── module-level accumulator ───────────────────────────────────────────────────

_total_usd: float = 0.0
_by_label: dict[str, float] = {}
_by_model: dict[str, float] = {}
_tokens: dict[str, int] = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
_calls: int = 0
_ollama_tokens: dict[str, int] = {"input": 0, "output": 0}


def reset_costs() -> None:
    """Zero the accumulator for a fresh run."""
    global _total_usd, _calls
    _total_usd = 0.0
    _calls = 0
    _by_label.clear()
    _by_model.clear()
    for k in _tokens:
        _tokens[k] = 0
    for k in _ollama_tokens:
        _ollama_tokens[k] = 0


def call_cost(usage, model: str | None) -> float:
    """Dollar cost of a single call's `usage` on `model`. 0.0 if not billable.
    Pure — does not touch the accumulator."""
    rates = _rates(model)
    if rates is None or usage is None:
        return 0.0
    in_rate, out_rate = rates
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    created = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (
        inp * in_rate
        + read * in_rate * _CACHE_READ_MULT
        + created * in_rate * _CACHE_WRITE_MULT
        + out * out_rate
    ) / _MTOK


def record_usage(usage, model: str | None, label: str) -> None:
    """Accumulate one paid Claude call. Safe to call on any `response.usage`
    (tolerates None / missing fields) and on local models (recorded as $0)."""
    global _total_usd, _calls
    cost = call_cost(usage, model)
    _calls += 1
    _total_usd += cost
    if cost:
        _by_label[label] = _by_label.get(label, 0.0) + cost
        mkey = _family(model) or "other"
        _by_model[mkey] = _by_model.get(mkey, 0.0) + cost
    if usage is not None:
        _tokens["input"] += getattr(usage, "input_tokens", 0) or 0
        _tokens["output"] += getattr(usage, "output_tokens", 0) or 0
        _tokens["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
        _tokens["cache_creation"] += getattr(usage, "cache_creation_input_tokens", 0) or 0


def record_ollama_usage(prompt_tokens: int, eval_tokens: int) -> None:
    """Accumulate token counts from a local Ollama call (no cost — free)."""
    _ollama_tokens["input"] += prompt_tokens or 0
    _ollama_tokens["output"] += eval_tokens or 0


def cost_summary() -> dict:
    """Snapshot of accumulated cost for this run."""
    return {
        "total_usd": round(_total_usd, 4),
        "paid_calls": _calls,
        "by_label": {k: round(v, 4) for k, v in sorted(_by_label.items(), key=lambda kv: -kv[1])},
        "by_model": {k: round(v, 4) for k, v in sorted(_by_model.items(), key=lambda kv: -kv[1])},
        "tokens": dict(_tokens),
        "ollama_tokens": dict(_ollama_tokens),
    }


def format_cost_line() -> str:
    """One-line human summary, e.g. 'est. cost $2.41 over 18 paid call(s) — sonnet $2.10, haiku $0.31'."""
    s = cost_summary()
    parts = [f"{m} ${c:.2f}" for m, c in s["by_model"].items()]
    by = (" — " + ", ".join(parts)) if parts else ""
    return f"est. cost ${s['total_usd']:.2f} over {s['paid_calls']} paid call(s){by}"


# ── self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    class _U:  # minimal stand-in for an Anthropic response.usage
        def __init__(self, i=0, o=0, r=0, c=0):
            self.input_tokens = i
            self.output_tokens = o
            self.cache_read_input_tokens = r
            self.cache_creation_input_tokens = c

    reset_costs()

    # Sonnet: 1M uncached input ($3) + 1M output ($15) = $18.00
    record_usage(_U(i=1_000_000, o=1_000_000), "claude-sonnet-4-6", "orch:INIT")
    assert abs(cost_summary()["total_usd"] - 18.0) < 1e-6, cost_summary()

    # Haiku cache read: 1M read at 0.1x of $1 = $0.10
    record_usage(_U(r=1_000_000), "claude-haiku-4-5", "router")
    assert abs(cost_summary()["total_usd"] - 18.10) < 1e-6, cost_summary()

    # Cache write: 1M created at 1.25x of $1 (haiku) = $1.25
    record_usage(_U(c=1_000_000), "anthropic/claude-haiku-4-5-20251001", "router")
    assert abs(cost_summary()["total_usd"] - 19.35) < 1e-6, cost_summary()

    # Local/ollama worker → $0, but still counts as a call and tallies no money
    record_usage(_U(i=5000, o=2000), "ollama/qwen2.5-coder:14b", "worker")
    assert abs(cost_summary()["total_usd"] - 19.35) < 1e-6, cost_summary()

    # None usage / None model are safe
    record_usage(None, "claude-opus-4-8", "x")
    record_usage(_U(i=1), None, "x")

    s = cost_summary()
    assert s["paid_calls"] == 6, s
    assert "sonnet" in s["by_model"] and "haiku" in s["by_model"], s
    assert s["tokens"]["input"] == 1_000_000 + 5000 + 1, s
    print("cost self-test passed ✓ |", format_cost_line())
