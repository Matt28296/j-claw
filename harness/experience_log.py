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


def log_escalation(
    task_type: str,
    stack: str,
    failed_model: str,
    succeeded_model: str,
    error_summary: str,
    objective_summary: str,
) -> None:
    """Append one JSON line recording a within-chain escalation outcome.

    Called when a weaker model fails and a stronger model succeeds on the same
    task within a single execute_task() call. Used to pre-emptively warn future
    weak-model attempts about known failure patterns.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "escalation",
        "task_type": task_type,
        "stack": stack,
        "failed_model": failed_model,
        "succeeded_model": succeeded_model,
        "error_summary": error_summary[:200],
        "objective_summary": objective_summary[:150],
    }
    try:
        with EXPERIENCE_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def get_worker_hints(task_type: str, stack: str, limit: int = 3) -> list[str]:
    """Return pre-emptive hints for a worker based on past escalation patterns.

    When qwen3 previously failed a task of this type and a stronger model
    succeeded, this surfaces the common error so the weak model can avoid it
    on its first attempt. Returns [] if no history exists (safe no-op).
    """
    if not EXPERIENCE_FILE.exists():
        return []

    patterns: dict[frozenset, dict] = {}
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

                if entry.get("event") != "escalation":
                    continue
                if entry.get("task_type") != task_type:
                    continue
                entry_stack = entry.get("stack", "")
                if entry_stack and stack and entry_stack != stack:
                    continue

                err = entry.get("error_summary", "")
                obj = entry.get("objective_summary", "")
                words = frozenset(_word_set(err))
                if not words:
                    continue
                rec = patterns.setdefault(words, {"count": 0, "error": err, "objective": obj})
                rec["count"] += 1
                rec["objective"] = obj  # most recent wins

    except OSError:
        return []

    if not patterns:
        return []

    sorted_patterns = sorted(patterns.values(), key=lambda r: r["count"], reverse=True)
    hints: list[str] = []
    for rec in sorted_patterns[:limit]:
        hint = (
            f"Past {task_type} tasks on {stack} commonly fail with: "
            f'"{rec["error"][:120]}". '
            f'A successful approach had objective: "{rec["objective"][:120]}".'
        )
        hints.append(hint)
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

    # Second pass: escalation entries — local models that consistently needed a stronger
    # model signal that the orchestrator should plan these tasks with extra specificity.
    if len(lessons) < max_lessons:
        esc_by_type: dict[str, dict] = {}
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
                    if entry.get("event") != "escalation":
                        continue
                    entry_stack = entry.get("stack", "")
                    if entry_stack and stack and entry_stack != stack:
                        continue
                    t = entry.get("task_type", "unknown")
                    rec = esc_by_type.setdefault(t, {"count": 0, "error": ""})
                    rec["count"] += 1
                    if entry.get("error_summary"):
                        rec["error"] = entry["error_summary"][:120]
        except OSError:
            pass

        remaining = max_lessons - len(lessons)
        for t, rec in sorted(esc_by_type.items(), key=lambda kv: kv[1]["count"], reverse=True):
            if remaining <= 0:
                break
            if rec["count"] < min_count:
                continue
            lesson = (
                f"[escalation] '{t}' tasks required a stronger model {rec['count']}x on "
                f"{stack!r} stack — local models struggled. Plan these tasks with maximum "
                f"specificity (exact selectors, function signatures, file paths)."
            )
            if rec["error"]:
                lesson += f' Common failure: "{rec["error"]}"'
            lessons.append(lesson)
            remaining -= 1

    return lessons
