"""Memory Validator — validates operation-based memory patches before application.

Uses deterministic rules only — no LLM, target <10ms per patch.
"""
from __future__ import annotations
import json
import re
from pathlib import Path


class ValidationResult:
    __slots__ = ("outcome", "reason")

    def __init__(self, outcome: str, reason: str = "") -> None:
        self.outcome = outcome  # "PASS" | "WARN" | "REJECT"
        self.reason = reason

    @property
    def ok(self) -> bool:
        return self.outcome in ("PASS", "WARN")

    def __repr__(self) -> str:
        return f"ValidationResult({self.outcome!r}, {self.reason!r})"


class MemoryValidator:
    """Validates a memory_patch.json dict against the current project_memory state.

    Checks:
    - Optimistic concurrency: patch.base_version == current memory version
    - Per-operation structural rules
    - Duplicate / conflict detection
    """

    def validate(self, patch: dict, memory_dir: Path) -> ValidationResult:
        operation = patch.get("operation", "")
        base_version = patch.get("base_version")

        # Required fields
        if not operation:
            return ValidationResult("REJECT", "Patch missing 'operation' field")
        if "payload" not in patch:
            return ValidationResult("REJECT", "Patch missing 'payload' field")

        # Version check (optimistic concurrency)
        if base_version is not None:
            current_version = self._get_version(memory_dir)
            if base_version < current_version:
                return ValidationResult(
                    "REJECT",
                    f"Stale patch: base_version={base_version} < current_version={current_version}"
                )

        # Per-operation rules
        payload = patch.get("payload", {})
        if operation == "add_api_endpoint":
            return self._validate_add_endpoint(payload, memory_dir)
        elif operation == "update_api_endpoint":
            return self._validate_update_endpoint(payload, memory_dir)
        elif operation == "deprecate_api_endpoint":
            return self._validate_deprecate_endpoint(payload, memory_dir)
        elif operation == "log_decision":
            return self._validate_log_decision(payload, memory_dir)
        elif operation == "add_known_issue":
            return self._validate_add_issue(payload)
        elif operation == "resolve_known_issue":
            return self._validate_resolve_issue(payload, memory_dir)
        elif operation == "update_project_summary":
            return self._validate_update_summary(payload)
        elif operation == "create_adr":
            return self._validate_create_adr(payload, memory_dir)
        else:
            return ValidationResult("REJECT", f"Unknown operation: {operation!r}")

    # ── per-operation validators ──────────────────────────────────────────────

    def _validate_add_endpoint(self, payload: dict, memory_dir: Path) -> ValidationResult:
        method = payload.get("method", "")
        route = payload.get("route", "")

        if not method or not route:
            return ValidationResult("REJECT", "add_api_endpoint requires 'method' and 'route'")
        if "request" not in payload or "response" not in payload:
            return ValidationResult("REJECT", "add_api_endpoint requires 'request' and 'response' schema dicts")

        # Check for exact duplicate
        contracts = self._read_file(memory_dir / "api_contracts.md")
        if f"`{method.upper()} {route}`" in contracts:
            return ValidationResult("REJECT", f"Endpoint {method.upper()} {route} already defined")

        # Check for near-duplicate (plural/singular variant)
        alt_route = route.rstrip("s") if route.endswith("s") else route + "s"
        if f"`{method.upper()} {alt_route}`" in contracts:
            return ValidationResult(
                "WARN",
                f"Similar endpoint {method.upper()} {alt_route} already exists — plural/singular inconsistency"
            )

        # Route format check
        if not route.startswith("/"):
            return ValidationResult("WARN", f"Route {route!r} should start with '/'")

        return ValidationResult("PASS")

    def _validate_update_endpoint(self, payload: dict, memory_dir: Path) -> ValidationResult:
        method = payload.get("method", "")
        route = payload.get("route", "")
        if not method or not route:
            return ValidationResult("REJECT", "update_api_endpoint requires 'method' and 'route'")
        contracts = self._read_file(memory_dir / "api_contracts.md")
        if f"`{method.upper()} {route}`" not in contracts:
            return ValidationResult("REJECT", f"Endpoint {method.upper()} {route} not found — cannot update")
        return ValidationResult("PASS")

    def _validate_deprecate_endpoint(self, payload: dict, memory_dir: Path) -> ValidationResult:
        method = payload.get("method", "")
        route = payload.get("route", "")
        if not method or not route:
            return ValidationResult("REJECT", "deprecate_api_endpoint requires 'method' and 'route'")
        contracts = self._read_file(memory_dir / "api_contracts.md")
        if f"`{method.upper()} {route}`" not in contracts:
            return ValidationResult("REJECT", f"Endpoint {method.upper()} {route} not found — cannot deprecate")
        return ValidationResult("PASS")

    def _validate_log_decision(self, payload: dict, memory_dir: Path) -> ValidationResult:
        if not payload.get("decision"):
            return ValidationResult("REJECT", "log_decision requires non-empty 'decision'")
        if not payload.get("tags"):
            return ValidationResult("WARN", "log_decision should include 'tags' for filtering")

        # Duplicate detection (check last 50 entries)
        log_path = memory_dir / "decision_log.jsonl"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            decision_text = payload["decision"].strip().lower()
            for line in lines[-50:]:
                try:
                    entry = json.loads(line)
                    if entry.get("decision", "").strip().lower() == decision_text:
                        return ValidationResult("REJECT", "Duplicate decision already logged")
                except json.JSONDecodeError:
                    continue

        return ValidationResult("PASS")

    def _validate_add_issue(self, payload: dict) -> ValidationResult:
        if not payload.get("issue"):
            return ValidationResult("REJECT", "add_known_issue requires non-empty 'issue'")
        if not payload.get("workaround"):
            return ValidationResult("REJECT", "add_known_issue requires non-empty 'workaround'")
        return ValidationResult("PASS")

    def _validate_resolve_issue(self, payload: dict, memory_dir: Path) -> ValidationResult:
        issue = payload.get("issue", "")
        if not issue:
            return ValidationResult("REJECT", "resolve_known_issue requires 'issue' name")
        content = self._read_file(memory_dir / "known_issues.md")
        if issue not in content:
            return ValidationResult("REJECT", f"Issue {issue!r} not found in known_issues.md")
        return ValidationResult("PASS")

    def _validate_update_summary(self, payload: dict) -> ValidationResult:
        if not payload.get("content"):
            return ValidationResult("REJECT", "update_project_summary requires non-empty 'content'")
        return ValidationResult("PASS")

    def _validate_create_adr(self, payload: dict, memory_dir: Path) -> ValidationResult:
        adr_id = payload.get("id", "")
        title = payload.get("title", "")
        if not adr_id or not title:
            return ValidationResult("REJECT", "create_adr requires 'id' and 'title'")

        # ID format check: ADR-NNN
        if not re.match(r"^ADR-\d{3,}$", adr_id):
            return ValidationResult("REJECT", f"ADR ID must match ADR-NNN format, got {adr_id!r}")

        # Title format: kebab-case
        if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", title):
            return ValidationResult("WARN", f"ADR title should be kebab-case, got {title!r}")

        # Uniqueness
        adr_dir = memory_dir / "architecture_decisions"
        if adr_dir.exists():
            existing = [f.name for f in adr_dir.glob(f"{adr_id}-*.md")]
            if existing:
                return ValidationResult("REJECT", f"ADR {adr_id} already exists: {existing[0]}")

        return ValidationResult("PASS")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_version(self, memory_dir: Path) -> int:
        meta_path = memory_dir / "_meta.json"
        if not meta_path.exists():
            return 0
        try:
            return json.loads(meta_path.read_text(encoding="utf-8")).get("version", 0)
        except (json.JSONDecodeError, OSError):
            return 0

    def _read_file(self, path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""
