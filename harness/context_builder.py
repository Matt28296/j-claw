"""Context Builder — deterministic context selection for workers.

Reads project_memory/ and selects a relevant ~4K-token subset for each task.
Returns structured JSON — not raw text injection.
No LLM calls. Pure Python rules + keyword matching.
"""
from __future__ import annotations
import re
from pathlib import Path

from project_memory import ProjectMemory, RuntimeMemory


# Target rough token budget per context build (~4 chars per token)
_TARGET_CHARS = 16_000


def _extract_keywords(text: str) -> list[str]:
    """Extract content words from a task objective."""
    stop = {
        "a", "an", "the", "and", "or", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "that", "this", "it", "its", "is",
        "are", "was", "will", "be", "has", "have", "do", "does", "not",
        "all", "any", "each", "make", "build", "create", "write", "add",
        "implement", "generate", "set", "up", "using", "use", "should",
        "must", "ensure", "include", "return", "get", "put", "post",
    }
    words = re.findall(r"[a-zA-Z]{3,}", text.lower())
    return [w for w in words if w not in stop]


class ContextBuilder:
    """Builds structured context dicts for worker tasks."""

    def build(self, task, project_dir: Path) -> dict:
        """Return a structured context dict for the given task.

        Args:
            task: Task dataclass with .type, .objective, .dependencies attributes
            project_dir: Project output directory containing project_memory/ and runtime_memory/

        Returns:
            Structured dict with required_context, related_files, recent_decisions,
            known_risks, relevant_adrs, base_version.
        """
        pm = ProjectMemory(project_dir)
        rm = RuntimeMemory(project_dir)

        if not pm.exists():
            return {"base_version": 0, "required_context": {}, "related_files": [],
                    "recent_decisions": [], "known_risks": [], "relevant_adrs": []}

        task_type = getattr(task, "type", "code")
        objective = getattr(task, "objective", "")
        keywords = _extract_keywords(objective)

        ctx: dict = {}

        # ── Always include ───────────────────────────────────────────────────
        state = rm.get_current_state()
        ctx["required_context"] = {
            "coding_standards": pm.read_coding_standards()[:3000],
            "current_phase": state.get("phase", "unknown"),
            "completed_tasks": state.get("completed_tasks", 0),
        }
        ctx["base_version"] = pm.get_version()

        # ── By task type ─────────────────────────────────────────────────────
        related_files: list[str] = []

        if task_type in ("code", "backend", "database"):
            api = pm.read_api_contracts()
            if api and "*No contracts defined yet.*" not in api:
                ctx["required_context"]["api_contracts"] = api[:4000]
        elif task_type in ("frontend",):
            api = pm.read_api_contracts()
            if api and "*No contracts defined yet.*" not in api:
                ctx["required_context"]["api_contracts"] = api[:4000]
        elif task_type == "devops":
            ctx["required_context"]["architecture_summary"] = pm.read_architecture_head(lines=60)
        elif task_type == "documentation":
            path = pm.root / "project_summary.md"
            if path.exists():
                ctx["required_context"]["project_summary"] = path.read_text(encoding="utf-8")

        # Add dependency file hints from task
        for dep in getattr(task, "dependencies", []):
            related_files.append(dep)
        ctx["related_files"] = related_files[:10]

        # ── Architecture head (always, capped) ───────────────────────────────
        arch_head = pm.read_architecture_head(lines=60)
        if arch_head and "required_context" in ctx:
            ctx["required_context"].setdefault("architecture", arch_head)

        # ── Recent decisions ─────────────────────────────────────────────────
        ctx["recent_decisions"] = pm.read_recent_decisions(n=10, keywords=keywords)

        # ── Known risks ──────────────────────────────────────────────────────
        issues_text = pm.read_known_issues(keywords=keywords, max_issues=5)
        if issues_text and "*No issues recorded yet.*" not in issues_text:
            ctx["known_risks"] = _parse_issue_blocks(issues_text)
        else:
            ctx["known_risks"] = []

        # ── ADR index ────────────────────────────────────────────────────────
        ctx["relevant_adrs"] = pm.read_adr_index(keywords=keywords)

        return ctx


def _parse_issue_blocks(text: str) -> list[dict]:
    """Convert known_issues.md text into a list of {issue, workaround} dicts."""
    result = []
    current_issue = None
    current_workaround = ""

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current_issue:
                result.append({"issue": current_issue, "workaround": current_workaround.strip()})
            current_issue = stripped[3:].strip()
            current_workaround = ""
        elif stripped.startswith("**Workaround:**"):
            current_workaround = stripped.replace("**Workaround:**", "").strip()

    if current_issue:
        result.append({"issue": current_issue, "workaround": current_workaround.strip()})

    return result
