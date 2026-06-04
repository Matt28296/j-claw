#!/usr/bin/env python3
"""Surface Anthropic prompt-cache hit/miss stats per call.

Lets a run *prove* prompt caching is working: the first call that includes a
`cache_control` breakpoint shows `cache_creation_input_tokens > 0` (a write);
every subsequent call within the TTL shows `cache_read_input_tokens > 0` (a
HIT, billed at ~0.1x). If both stay 0, caching isn't engaging — investigate.

Pure logging, no I/O beyond stdout. Safe to call on any Anthropic
`response.usage`; tolerates None / missing fields.
"""
from __future__ import annotations

from rich.console import Console

_console = Console()


def log_cache_usage(usage, label: str) -> None:
    """Print prompt-cache token stats from an Anthropic `response.usage`."""
    if usage is None:
        return
    created = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    if not created and not read:
        return  # caching not engaged for this call — stay quiet
    state = "HIT" if read else "write"
    color = "green" if read else "yellow"
    _console.print(
        f"  [{color}]· cache[{label}] {state}[/{color}] "
        f"[dim]read={read} created={created} in={inp} out={out}[/dim]"
    )
