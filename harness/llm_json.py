"""Shared parsing of LLM JSON output: fence-stripping, in-string escape repair, and tolerant
object loading.

Single source of truth so the worker rungs, the `planning_call` helper, and the orchestrator's
Codex tier don't drift in how they recover a JSON object from a model's raw text (PR #105
follow-up #6). Kept dependency-free (no provider/budget state) so any layer can import it without
pulling in worker.py.
"""
from __future__ import annotations

import json


def strip_fences(text: str) -> str:
    """Remove accidental ```json ... ``` wrapping that models sometimes add."""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    inner = lines[1:]  # drop the opening ``` / ```json line
    if inner and inner[-1].strip() == "```":
        inner = inner[:-1]
    return "\n".join(inner).strip()


def fix_json_strings(text: str) -> str:
    """Escape literal newlines/tabs that appear INSIDE JSON string values and would otherwise break
    json.loads (a Gemini-class failure mode). Characters outside strings are left untouched."""
    result = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == "\\" and in_string:
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif in_string and ch == "\n":
            result.append("\\n")
        elif in_string and ch == "\r":
            pass  # strip bare CR
        elif in_string and ch == "\t":
            result.append("\\t")
        else:
            result.append(ch)
    return "".join(result)


def loads_tolerant(raw: str):
    """Parse JSON, tolerating trailing prose/data after the first object. Returns the parsed value
    (dict/list/scalar) or None when nothing parseable is found."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    if start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(raw[start:])
            return obj
        except json.JSONDecodeError:
            return None
    return None


def loads_llm_json_object(raw: str) -> dict:
    """Best-effort recovery of a JSON OBJECT from a model response, combining both tolerances:
    fence-strip then a tolerant load; if that doesn't yield a dict, retry after repairing literal
    newlines/tabs inside strings. Raises ValueError if no JSON object can be recovered."""
    text = strip_fences(raw.strip())

    parsed = loads_tolerant(text)
    if isinstance(parsed, dict):
        return parsed

    start = text.find("{")
    fixed = fix_json_strings(text[start:] if start >= 0 else text)
    parsed = loads_tolerant(fixed)
    if isinstance(parsed, dict):
        return parsed

    raise ValueError("no JSON object found in model output")
