"""Action-risk classification (roadmap item #6) — OBSERVE-ONLY for this milestone.

J-Claw runs side-effecting actions (package installs, deploys, git, destructive FS, LLM CLIs)
unattended and remote-triggered with no gating. This module classifies each such action by
**blast radius** and, in observe mode, logs a ``risk_classified`` event into the session log
(session_log.py / StateWriter.on_action) **WITHOUT blocking** — so real builds produce the evidence
needed to set sensible thresholds before enforcement (the permission modes, roadmap #1) is added.

Per the approved roadmap + Codex debate: log decisions before you block, then tighten policy from
evidence. The classifier is pure and deterministic (unit-testable, no I/O); ``observe()`` is the only
behavior wired today and it never raises and never blocks.
"""
from __future__ import annotations

# Ordered low → high so a future enforcement layer can compare against a threshold.
RISK_LEVELS = ("low", "medium", "high", "critical")

# Operating modes for the future enforcement layer (roadmap #1). Only "observe" is acted on today.
PERMISSION_MODES = ("observe", "read_only", "ask_before_write", "auto_safe", "dangerous_skip")


def risk_rank(risk: str) -> int:
    """Numeric rank of a risk level (unknown → most severe, so it can't slip under a threshold)."""
    try:
        return RISK_LEVELS.index(risk)
    except ValueError:
        return len(RISK_LEVELS)


def classify_action(kind: str, detail: str = "", **ctx) -> tuple[str, str]:
    """Classify a side-effecting action by blast radius → (risk, reason). Deterministic, no I/O.

    ``kind`` mirrors the execution-surface categories from the harness audit:
    deploy | deploy_hook | install | git | fs_delete | llm_cli | build | test | render | shell.
    """
    k = (kind or "").lower()
    d = (detail or "").lower()
    if k in ("deploy", "deploy_hook"):
        # Publishes to a public target, or runs an arbitrary operator-configured shell command.
        return "critical", "publishes to a public deploy target / runs an arbitrary deploy command"
    if k == "install":
        return "high", "package install can execute arbitrary install/post-install scripts"
    if k == "git":
        if "push" in d:
            return "high", "git push mutates a remote"
        return "low", "local git (init/add/commit) — no remote push"
    if k == "fs_delete":
        return "medium", "destructive filesystem delete"
    if k == "llm_cli":
        return "medium", "external LLM CLI — network egress + subscription/credential use"
    if k == "shell":
        # Executes an LLM-authored script (render.sh / render_scene.py) via bash/python —
        # arbitrary local code execution, the same blast radius as a package install.
        return "high", "executes an LLM-authored script (bash/python) — arbitrary local code execution"
    if k in ("build", "test", "render"):
        return "low", f"local {k} command"
    return "medium", f"unclassified action ({kind})"


def observe(kind: str, detail: str = "", **ctx) -> str:
    """Classify an action and LOG it (``risk_classified``) without blocking — observe-only mode.

    Returns the assessed risk for the caller's information. Safe to call from anywhere; never raises
    and never alters control flow (the enforcement layer / permission modes come in roadmap #1)."""
    risk, reason = classify_action(kind, detail, **ctx)
    try:
        from state_writer import writer as _sw
        _sw.on_action(kind, risk, reason=reason, detail=detail)
    except Exception:  # noqa: BLE001 — observation must never break the pipeline
        pass
    return risk
