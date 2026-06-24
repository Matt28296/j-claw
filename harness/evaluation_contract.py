"""Stable EVALUATION CONTRACT — the only surface training/eval code may use to reach live worker
behaviour.

eval_worker reconstructs how j-claw *actually* prompts, parses, and verifies a worker model. If it
reached into ``worker``/``verification`` privates directly, a harness refactor could silently change
what "valid output" or "verified" means and the eval would drift without anyone noticing. This module
wraps those internals behind a small, versioned API and is pinned by golden tests
(``training/test_eval_contract.py``). **Bump the relevant ``*_VERSION`` when the wrapped behaviour
changes** — the version travels into every eval artifact, so a stale comparison is detectable.

Public API:
  task_from_dataset_row(row) -> Task            (fail-closed on missing critical fields)
  build_worker_prompt(row)   -> (system, user, task, stack)
  parse_worker_output(text, task) -> (files|None, error)
  verify_task(task, project_dir)  -> (outcome, log)   outcome: pass|fail|skipped|error
  ollama_version() -> str
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import config
import verification
import worker
from project import Task

# Versions travel into eval artifacts. Bump when the wrapped harness behaviour changes shape.
CONTRACT_VERSION = 1
PROMPT_VERSION = 1     # _SYSTEM_PROMPT + _STACK_PROMPTS + _build_user_message
PARSER_VERSION = 1     # _parse_and_validate (fence-strip + tolerant decode + single-file salvage)
VERIFIER_VERSION = 1   # run_verification + SKIP_PREFIX semantics

SKIP_PREFIX = verification.SKIP_PREFIX


class ContractError(ValueError):
    """A dataset row cannot be faithfully turned into a worker task (fail closed; do not guess)."""


def task_from_dataset_row(row: dict) -> Task:
    """Rebuild a harness Task from a stored SFT row. FAIL CLOSED on missing fields that change meaning
    (type/objective/files); only cosmetic fields (priority) get a default. The verification METHOD is
    recovered from metadata.verification (the exporter stores it there)."""
    if not isinstance(row, dict):
        raise ContractError("row is not a dict")
    inp = row.get("input") or {}
    t = inp.get("task") or {}
    meta = row.get("metadata") or {}
    ttype = t.get("type")
    objective = t.get("objective")
    files = t.get("files")
    if not isinstance(ttype, str) or not ttype:
        raise ContractError("row.input.task.type missing")
    if not isinstance(objective, str) or not objective:
        raise ContractError("row.input.task.objective missing")
    if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
        raise ContractError("row.input.task.files missing or not a list[str]")
    verification_method = meta.get("verification") or t.get("verification") or ""
    return Task(
        id=t.get("id", ""),
        type=ttype,
        objective=objective,
        files=list(files),
        dependencies=list(t.get("dependencies") or []),
        priority=t.get("priority", "P2"),          # cosmetic: does not affect verification routing
        acceptance_criteria=list(t.get("acceptance_criteria") or []),
        verification=verification_method,
    )


def _spec_from_row(row: dict) -> dict:
    inp = row.get("input") or {}
    spec = dict(inp.get("spec") or {})
    arch = dict(spec.get("architecture") or {})
    if not arch.get("stack"):
        arch["stack"] = (inp.get("context") or {}).get("stack", "")
    spec["architecture"] = arch
    return spec


def build_worker_prompt(row: dict):
    """Reconstruct the EXACT (system, user) j-claw would send a worker for this row, plus the resolved
    Task and stack. context=None on purpose — the stored row carries no real memory_context, so we
    evaluate the no-memory baseline rather than injecting a fake one."""
    task = task_from_dataset_row(row)
    spec = _spec_from_row(row)
    deps = (row.get("input") or {}).get("dependency_files") or {}
    stack = config.spec_stack(spec) or "vanilla"
    stack_prompt = worker._STACK_PROMPTS.get(stack, worker._STACK_PROMPTS["vanilla"])
    system = worker._SYSTEM_PROMPT + "\n" + stack_prompt
    user = worker._build_user_message(task, spec, deps, None)
    return system, user, task, stack


def parse_worker_output(text: str, task):
    """Parse a raw model response with j-claw's REAL tolerant contract (incl. single-file salvage).
    Returns (files|None, error) where files is a list of {path, content}."""
    try:
        parsed = worker._parse_and_validate(text, task)
    except Exception as exc:  # noqa: BLE001 — production treats any parse failure as a hard miss
        return None, str(exc)[:200]
    return parsed.get("files", []), None


def verify_task(task, project_dir: Path) -> tuple[str, str]:
    """Run the REAL harness verifier. Returns (outcome, log): pass | fail | skipped | error.
    A SKIP_PREFIX log is 'skipped' (NOT a pass) — the same rule the exporter uses."""
    try:
        ok, log = verification.run_verification(task, Path(project_dir))
    except Exception as exc:  # noqa: BLE001 — a verifier crash is an eval error, not a model verdict
        return "error", f"{type(exc).__name__}: {exc}"
    log = log if isinstance(log, str) else ""
    if log.startswith(SKIP_PREFIX):
        return "skipped", log
    return ("pass" if ok else "fail"), log


def ollama_version() -> str:
    """Best-effort `ollama --version` for eval provenance (deterministic gen is not stable across
    Ollama/model/runtime versions, so we record it)."""
    try:
        proc = subprocess.run(["ollama", "--version"], capture_output=True, text=True, timeout=10)
        return (proc.stdout or proc.stderr or "").strip()[:120] or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def versions() -> dict:
    return {
        "contract": CONTRACT_VERSION,
        "prompt": PROMPT_VERSION,
        "parser": PARSER_VERSION,
        "verifier": VERIFIER_VERSION,
    }
