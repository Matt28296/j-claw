"""Local experience tracker — logs EXECUTION_ERROR outcomes to a JSONL file.
No external APIs. No network calls. Fully local.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

EXPERIENCE_FILE = Path(os.getenv("EXPERIENCE_LOG", "experience.jsonl"))


def log_outcome(
    task_id: str,
    task_type: str,
    error_summary: str,
    fix_action: str,
    fix_objective: str,
    succeeded: bool,
    stack: str = "",
) -> None:
    """Append one JSON line recording the outcome of an EXECUTION_ERROR fix attempt."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "task_type": task_type,
        "error_summary": error_summary[:200],
        "fix_action": fix_action,
        "fix_objective": fix_objective[:200],
        "succeeded": succeeded,
        "stack": stack,
    }
    try:
        with EXPERIENCE_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        # Never crash the pipeline over a logging failure.
        pass


def _word_set(text: str) -> set[str]:
    """Return lowercase words longer than 2 chars from text (simple tokeniser)."""
    return {w.lower() for w in text.replace("_", " ").split() if len(w) > 2}


def get_relevant_hints(
    task_type: str,
    error_summary: str,
    max_hints: int = 3,
) -> list[str]:
    """Return up to max_hints human-readable hints from past successful fixes.

    Filters to entries where:
      - succeeded is True
      - task_type matches exactly
      - error_summary has word overlap with the current error

    Scored by word-intersection size (descending).  No embeddings, no network.
    """
    if not EXPERIENCE_FILE.exists():
        return []

    query_words = _word_set(error_summary)
    if not query_words:
        return []

    candidates: list[tuple[int, str]] = []

    try:
        with EXPERIENCE_FILE.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                if not entry.get("succeeded"):
                    continue
                if entry.get("task_type") != task_type:
                    continue

                entry_words = _word_set(entry.get("error_summary", ""))
                overlap = len(query_words & entry_words)
                if overlap == 0:
                    continue

                hint = (
                    f"For {task_type} tasks with similar error: "
                    f"{entry.get('fix_action', 'unknown')} worked — "
                    f"{entry.get('fix_objective', '').strip()}"
                )
                candidates.append((overlap, hint))

    except OSError:
        return []

    # Sort by overlap score descending, deduplicate hint text, return top N
    candidates.sort(key=lambda x: x[0], reverse=True)
    seen: set[str] = set()
    hints: list[str] = []
    for _, hint in candidates:
        if hint not in seen:
            seen.add(hint)
            hints.append(hint)
        if len(hints) >= max_hints:
            break

    return hints


def get_stack_lessons(stack: str, max_lessons: int = 6, min_count: int = 2) -> list[str]:
    """Aggregate recurring failure patterns into planning-time lessons for the
    orchestrator (INIT / SPEC_ACCEPTED payloads).

    Deterministic — counts + most-recent successful exemplar per task type, no
    LLM, no network. Entries from other stacks are excluded; legacy entries
    without a stack field count for every stack (they predate stack tagging).
    Patterns seen fewer than min_count times are noise and skipped. Output is
    bounded: max_lessons lines of ~2 sentences (~500 tokens total).
    """
    if not EXPERIENCE_FILE.exists():
        return []

    by_type: dict[str, dict] = {}
    try:
        with EXPERIENCE_FILE.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                entry_stack = entry.get("stack", "")
                if entry_stack and stack and entry_stack != stack:
                    continue

                t = entry.get("task_type", "unknown")
                rec = by_type.setdefault(t, {"failures": 0, "actions": {}, "exemplar": ""})
                rec["failures"] += 1
                if entry.get("succeeded"):
                    action = entry.get("fix_action", "unknown")
                    rec["actions"][action] = rec["actions"].get(action, 0) + 1
                    if entry.get("fix_objective"):
                        rec["exemplar"] = entry["fix_objective"][:160]  # most recent wins
    except OSError:
        return []

    lessons: list[str] = []
    for t, rec in sorted(by_type.items(), key=lambda kv: kv[1]["failures"], reverse=True):
        if rec["failures"] < min_count:
            continue
        top_actions = sorted(rec["actions"].items(), key=lambda kv: kv[1], reverse=True)[:2]
        actions = ", ".join(f"{a} x{n}" for a, n in top_actions) or "none succeeded"
        lesson = (
            f"'{t}' tasks needed {rec['failures']} EXECUTION_ERROR fix(es) in past builds "
            f"(successful fix actions: {actions}). Specify these tasks completely — exact "
            f"files, function signatures, and acceptance criteria — to avoid stub/partial output."
        )
        if rec["exemplar"]:
            lesson += f' Example fix that worked: "{rec["exemplar"]}"'
        lessons.append(lesson)
        if len(lessons) >= max_lessons:
            break

    return lessons
