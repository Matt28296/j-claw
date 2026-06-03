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
