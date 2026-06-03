"""Project Memory layer — persistent context for all agents within a project run.

Two sub-systems:
  ProjectMemory  — long-lived architecture/contract documents (survives --continue)
  RuntimeMemory  — ephemeral execution state (wiped on completion)
"""
from __future__ import annotations
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


# ── helpers ──────────────────────────────────────────────────────────────────

def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── ProjectMemory ─────────────────────────────────────────────────────────────

class ProjectMemory:
    """Manages project_memory/ — architecture docs, API contracts, ADRs, decision log."""

    def __init__(self, project_dir: Path) -> None:
        self.root = project_dir / "project_memory"
        self.adr_dir = self.root / "architecture_decisions"
        self._meta_path = self.root / "_meta.json"

    # ── init ─────────────────────────────────────────────────────────────────

    def initialize(self, tech_spec: dict, intent: str) -> None:
        """Seed project_memory/ from a TECH_SPEC produced by TechnicalArchitect.

        Safe to call on a fresh directory only — will not overwrite existing memory.
        """
        if self.root.exists():
            return  # already initialized (e.g. --continue run)

        self.root.mkdir(parents=True, exist_ok=True)
        self.adr_dir.mkdir(parents=True, exist_ok=True)

        # _meta.json — version counter + ownership tracking
        meta = {"version": 0, "last_modified": _ts(), "last_patch_by": "technical_architect"}
        _write_atomic(self._meta_path, json.dumps(meta, indent=2))

        # architecture.md
        notes = tech_spec.get("architecture_notes", [])
        risks = tech_spec.get("risks", [])
        arch_lines = [f"# Architecture\n", f"**Intent:** {intent}\n",
                      f"**Stack:** {tech_spec.get('confirmed_stack', 'unknown')}\n",
                      f"**Build system:** {tech_spec.get('build_system', 'n/a')}\n",
                      f"**Test strategy:** {tech_spec.get('test_strategy', 'n/a')}\n"]
        if notes:
            arch_lines.append("\n## Decisions\n")
            arch_lines.extend(f"- {n}\n" for n in notes)
        if risks:
            arch_lines.append("\n## Known Risks\n")
            arch_lines.extend(f"- {r}\n" for r in risks)
        _write_atomic(self.root / "architecture.md", "".join(arch_lines))
        _write_atomic(self.root / "architecture.md.meta.json",
                      json.dumps({"owner": "technical_architect", "last_modified": _ts(), "patch_count": 0}))

        # api_contracts.md
        _write_atomic(self.root / "api_contracts.md",
                      "# API Contracts\n\n*No contracts defined yet.*\n")
        _write_atomic(self.root / "api_contracts.md.meta.json",
                      json.dumps({"owner": "technical_architect", "last_modified": _ts(), "patch_count": 0}))

        # coding_standards.md
        standards = tech_spec.get("coding_standards", "No specific standards defined.")
        _write_atomic(self.root / "coding_standards.md",
                      f"# Coding Standards\n\n{standards}\n")

        # project_summary.md
        deps = ", ".join(tech_spec.get("dependencies", []))
        fs_items = "\n".join(f"- `{f}`" for f in tech_spec.get("file_structure", []))
        summary = (
            f"# Project Summary\n\n"
            f"**Goal:** {intent}\n\n"
            f"**Stack:** {tech_spec.get('confirmed_stack', 'unknown')}\n\n"
            f"**Dependencies:** {deps or 'none specified'}\n\n"
            f"## Planned File Structure\n\n{fs_items or '*not specified*'}\n"
        )
        _write_atomic(self.root / "project_summary.md", summary)
        _write_atomic(self.root / "project_summary.md.meta.json",
                      json.dumps({"owner": "technical_architect", "last_modified": _ts(), "patch_count": 0}))

        # known_issues.md
        _write_atomic(self.root / "known_issues.md",
                      "# Known Issues\n\n*No issues recorded yet.*\n")
        _write_atomic(self.root / "known_issues.md.meta.json",
                      json.dumps({"owner": "technical_architect", "last_modified": _ts(), "patch_count": 0}))

        # decision_log.jsonl (empty)
        _write_atomic(self.root / "decision_log.jsonl", "")

        # ADRs from tech_spec
        for adr in tech_spec.get("adrs_to_create", []):
            self._write_adr(adr)

    def _write_adr(self, adr: dict) -> None:
        adr_id = adr.get("id", "ADR-000")
        title = adr.get("title", "untitled")
        decision = adr.get("decision", "")
        reason = adr.get("reason", "")
        alternatives = adr.get("alternatives", [])

        filename = f"{adr_id}-{title}.md"
        content = (
            f"# {adr_id}: {title.replace('-', ' ').title()}\n\n"
            f"**Status:** Accepted\n"
            f"**Date:** {_ts()[:10]}\n"
            f"**Decided by:** technical_architect\n\n"
            f"## Decision\n\n{decision}\n\n"
            f"## Reason\n\n{reason}\n\n"
        )
        if alternatives:
            content += "## Alternatives Considered\n\n"
            content += "".join(f"- {a}\n" for a in alternatives)
        _write_atomic(self.adr_dir / filename, content)

    # ── version ───────────────────────────────────────────────────────────────

    def get_version(self) -> int:
        if not self._meta_path.exists():
            return 0
        return json.loads(self._meta_path.read_text(encoding="utf-8")).get("version", 0)

    def _increment_version(self, agent: str, reason: str) -> int:
        with _LOCK:
            meta = json.loads(self._meta_path.read_text(encoding="utf-8")) if self._meta_path.exists() else {}
            meta["version"] = meta.get("version", 0) + 1
            meta["last_modified"] = _ts()
            meta["last_patch_by"] = agent
            meta["last_change_reason"] = reason
            _write_atomic(self._meta_path, json.dumps(meta, indent=2))
            return meta["version"]

    # ── patch application ─────────────────────────────────────────────────────

    def apply_patch(self, patch: dict) -> dict:
        """Apply a validated memory patch. Returns {ok, version, reason}."""
        if not self.root.exists():
            return {"ok": False, "reason": "project_memory not initialized"}

        operation = patch.get("operation", "")
        agent = patch.get("agent", "worker")
        reason = patch.get("change_reason", "")
        payload = patch.get("payload", {})

        try:
            if operation == "add_api_endpoint":
                self._patch_add_endpoint(payload, agent, reason)
            elif operation == "update_api_endpoint":
                self._patch_update_endpoint(payload, agent, reason)
            elif operation == "deprecate_api_endpoint":
                self._patch_deprecate_endpoint(payload, agent, reason)
            elif operation == "log_decision":
                self._patch_log_decision(payload, agent)
            elif operation == "add_known_issue":
                self._patch_add_issue(payload, agent, reason)
            elif operation == "resolve_known_issue":
                self._patch_resolve_issue(payload, agent)
            elif operation == "update_project_summary":
                self._patch_update_summary(payload, agent, reason)
            elif operation == "create_adr":
                self._write_adr(payload)
            else:
                return {"ok": False, "reason": f"Unknown operation: {operation!r}"}

            new_version = self._increment_version(agent, reason or operation)
            return {"ok": True, "version": new_version}

        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    def _patch_add_endpoint(self, payload: dict, agent: str, reason: str) -> None:
        method = payload.get("method", "GET").upper()
        route = payload.get("route", "")
        request_schema = payload.get("request", {})
        response_schema = payload.get("response", {})
        path = self.root / "api_contracts.md"
        current = path.read_text(encoding="utf-8") if path.exists() else "# API Contracts\n\n"
        if f"`{method} {route}`" in current:
            raise ValueError(f"Endpoint {method} {route} already defined")
        entry = (
            f"\n## `{method} {route}`\n"
            f"*Added by: {agent}*\n\n"
            f"**Request:** `{json.dumps(request_schema)}`\n\n"
            f"**Response:** `{json.dumps(response_schema)}`\n"
        )
        if reason:
            entry += f"\n*Reason: {reason}*\n"
        _write_atomic(path, current + entry)
        self._update_meta("api_contracts.md", agent, reason)

    def _patch_update_endpoint(self, payload: dict, agent: str, reason: str) -> None:
        method = payload.get("method", "GET").upper()
        route = payload.get("route", "")
        path = self.root / "api_contracts.md"
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if f"`{method} {route}`" not in current:
            raise ValueError(f"Endpoint {method} {route} not found — cannot update")
        note = f"\n*Updated by {agent}: {reason}*\n"
        new = current.replace(f"`{method} {route}`", f"`{method} {route}` *(updated)*", 1)
        _write_atomic(path, new + note)
        self._update_meta("api_contracts.md", agent, reason)

    def _patch_deprecate_endpoint(self, payload: dict, agent: str, reason: str) -> None:
        method = payload.get("method", "GET").upper()
        route = payload.get("route", "")
        path = self.root / "api_contracts.md"
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if f"`{method} {route}`" not in current:
            raise ValueError(f"Endpoint {method} {route} not found — cannot deprecate")
        new = current.replace(f"`{method} {route}`", f"~~`{method} {route}`~~ *(deprecated)*", 1)
        _write_atomic(path, new)
        self._update_meta("api_contracts.md", agent, reason)

    def _patch_log_decision(self, payload: dict, agent: str) -> None:
        path = self.root / "decision_log.jsonl"
        entry = {
            "ts": _ts(),
            "agent": agent,
            "decision": payload.get("decision", ""),
            "tags": payload.get("tags", []),
            "reason": payload.get("reason", ""),
        }
        with _LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def _patch_add_issue(self, payload: dict, agent: str, reason: str) -> None:
        path = self.root / "known_issues.md"
        current = path.read_text(encoding="utf-8") if path.exists() else "# Known Issues\n\n"
        issue = payload.get("issue", "")
        workaround = payload.get("workaround", "")
        tags = payload.get("tags", [])
        tag_str = ", ".join(tags) if tags else ""
        entry = (
            f"\n## {issue}\n"
            f"**Reported by:** {agent}  \n"
            f"**Tags:** {tag_str}  \n"
            f"**Workaround:** {workaround}\n"
        )
        if "*No issues recorded yet.*" in current:
            current = current.replace("*No issues recorded yet.*\n", "")
        _write_atomic(path, current + entry)
        self._update_meta("known_issues.md", agent, reason)

    def _patch_resolve_issue(self, payload: dict, agent: str) -> None:
        path = self.root / "known_issues.md"
        issue = payload.get("issue", "")
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if issue not in current:
            raise ValueError(f"Issue {issue!r} not found")
        _write_atomic(path, current.replace(f"## {issue}", f"## ~~{issue}~~ ✓ Resolved"))
        self._update_meta("known_issues.md", agent, "resolved issue")

    def _patch_update_summary(self, payload: dict, agent: str, reason: str) -> None:
        path = self.root / "project_summary.md"
        content = payload.get("content", "")
        if not content:
            raise ValueError("update_project_summary requires non-empty content")
        _write_atomic(path, content)
        self._update_meta("project_summary.md", agent, reason)

    def _update_meta(self, filename: str, agent: str, reason: str) -> None:
        meta_path = self.root / f"{filename}.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        meta["owner"] = agent
        meta["last_modified"] = _ts()
        meta["last_change_reason"] = reason
        meta["patch_count"] = meta.get("patch_count", 0) + 1
        _write_atomic(meta_path, json.dumps(meta, indent=2))

    # ── read helpers for ContextBuilder ──────────────────────────────────────

    def read_coding_standards(self) -> str:
        path = self.root / "coding_standards.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def read_api_contracts(self) -> str:
        path = self.root / "api_contracts.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def read_architecture_head(self, lines: int = 60) -> str:
        path = self.root / "architecture.md"
        if not path.exists():
            return ""
        content_lines = path.read_text(encoding="utf-8").splitlines()
        return "\n".join(content_lines[:lines])

    def read_recent_decisions(self, n: int = 10, keywords: list[str] | None = None) -> list[dict]:
        path = self.root / "decision_log.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        recent = []
        for line in reversed(lines[-50:]):
            try:
                entry = json.loads(line)
                recent.append(entry)
                if len(recent) >= n:
                    break
            except json.JSONDecodeError:
                continue
        if keywords:
            kw_set = {k.lower() for k in keywords}
            keyword_matches = [
                e for e in recent
                if kw_set & {t.lower() for t in e.get("tags", [])}
            ]
            for e in keyword_matches:
                if e not in recent:
                    recent.append(e)
        return recent

    def read_known_issues(self, keywords: list[str] | None = None, max_issues: int = 5) -> str:
        path = self.root / "known_issues.md"
        if not path.exists():
            return ""
        content = path.read_text(encoding="utf-8")
        if not keywords:
            return content
        kw_set = {k.lower() for k in keywords}
        # Split into issue blocks (separated by ## headers)
        blocks = []
        current: list[str] = []
        for line in content.splitlines():
            if line.startswith("## ") and current:
                blocks.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            blocks.append("\n".join(current))
        # Filter blocks by keyword overlap
        matched = [b for b in blocks if sum(1 for kw in kw_set if kw in b.lower()) >= 2]
        return "\n\n".join(matched[:max_issues])

    def read_adr_index(self, keywords: list[str] | None = None) -> list[dict]:
        if not self.adr_dir.exists():
            return []
        adrs = []
        for f in sorted(self.adr_dir.glob("ADR-*.md")):
            lines = f.read_text(encoding="utf-8").splitlines()
            title = lines[0].lstrip("# ") if lines else f.stem
            adrs.append({"file": f.name, "title": title})
        if keywords:
            kw_set = {k.lower() for k in keywords}
            adrs = [a for a in adrs if any(kw in a["title"].lower() for kw in kw_set)]
        return adrs

    def exists(self) -> bool:
        return self.root.exists()


# ── RuntimeMemory ─────────────────────────────────────────────────────────────

class RuntimeMemory:
    """Manages runtime_memory/ — ephemeral execution state for crash recovery."""

    def __init__(self, project_dir: Path) -> None:
        self.root = project_dir / "runtime_memory"

    def _path(self, name: str) -> Path:
        return self.root / name

    def _read_json(self, name: str, default: Any) -> Any:
        p = self._path(name)
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default

    def _write_json(self, name: str, data: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _write_atomic(self._path(name), json.dumps(data, indent=2))

    def set_state(self, phase: str, completed: int = 0, failed: int = 0, branch: str = "") -> None:
        state = {
            "phase": phase,
            "completed_tasks": completed,
            "failed_tasks": failed,
            "current_branch": branch,
            "updated_at": _ts(),
        }
        self._write_json("current_state.json", state)

    def update_task(self, task_id: str, status: str) -> None:
        registry = self._read_json("task_registry.json", {})
        registry[task_id] = status
        self._write_json("task_registry.json", registry)

    def get_task_registry(self) -> dict:
        return self._read_json("task_registry.json", {})

    def set_active_workers(self, workers: dict) -> None:
        self._write_json("active_workers.json", workers)

    def get_current_state(self) -> dict:
        return self._read_json("current_state.json", {"phase": "unknown"})

    def clear(self) -> None:
        import shutil
        if self.root.exists():
            shutil.rmtree(self.root)

    def exists(self) -> bool:
        return self.root.exists()
