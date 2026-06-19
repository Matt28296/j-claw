#!/usr/bin/env python3
"""Terminal-outcome push notifications via Telegram.

A hands-off factory must be silent while working and loud at the end: the
operator is the escalation channel of last resort, not a polling loop. Reuses
the credentials the Telegram front-end already needs (TELEGRAM_BOT_TOKEN +
TELEGRAM_CHAT_ID in harness/.env); if either is unset every call is a silent
no-op, so CLI-only users are unaffected.

Stdlib-only (urllib) and never raises — a notification failure must never take
down a finished build.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import config  # noqa: F401  — imported for its .env loading side effect

_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_LEN = 4000  # Telegram hard limit is 4096; leave headroom


def send_telegram(text: str) -> bool:
    """POST a plain-text message to the configured chat. Returns True on success."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    payload = json.dumps(
        {"chat_id": chat_id, "text": text[:_MAX_LEN], "disable_web_page_preview": True}
    ).encode("utf-8")
    req = urllib.request.Request(
        _SEND_URL.format(token=token),
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def notify_build_outcome(
    *,
    project: str,
    passed: bool,
    heal_cycles: int,
    max_heal: int,
    handoff_path: Path | None = None,
    cost_line: str = "",
    stamp_issues: bool = False,
    deploy_url: str | None = None,
) -> bool:
    """Push the terminal verdict of a build. PASS can still carry caveats when
    the independent OpenClaw stamp disagreed with the pipeline verdict."""
    if passed and stamp_issues:
        # Honest reporting (#6): the pipeline verdict passed, but the independent
        # OpenClaw stamp found unresolved issues. Do NOT show a clean ✅ — that would
        # tell the operator the build is good when a reviewer disagreed.
        head = f"⚠️ J-Claw build PASSED-PIPELINE / STAMP FLAGGED ISSUES — {project}"
    elif passed:
        head = f"✅ J-Claw build PASSED — {project}"
    else:
        head = f"❌ J-Claw build FAILED — {project}"
    lines = [head, f"Heal cycles: {heal_cycles}/{max_heal}"]
    if stamp_issues:
        lines.append("⚠ OpenClaw stamp found unresolved issues — see HANDOFF verdict")
    if deploy_url:
        lines.append(f"Deployed: {deploy_url}")
    if cost_line:
        lines.append(cost_line)
    if handoff_path is not None:
        lines.append(f"HANDOFF: {handoff_path}")
    return send_telegram("\n".join(lines))


def notify_crash(*, project: str, error: str, output_dir: Path | None = None) -> bool:
    """Push a pipeline crash (all retries exhausted). Phase details live in the
    failure HANDOFF.md that _write_failure_handoff already produced."""
    lines = [f"💥 J-Claw pipeline CRASHED — {project}", f"Error: {error[:500]}"]
    if output_dir is not None:
        lines.append(f"HANDOFF: {output_dir / 'HANDOFF.md'}")
    return send_telegram("\n".join(lines))
