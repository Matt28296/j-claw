#!/usr/bin/env python3
"""Email status reports for long-running J-Claw builds.

A companion to notify.py (Telegram): where notify.py pushes a single terminal
verdict, this module sends *periodic* progress summaries to an operator's inbox
and a final detailed report on completion — useful when a FORMAT 5 build runs for
hours and the operator is away from the terminal.

SMTP config comes from harness/.env (loaded by config). If EMAIL_ENABLED is not
true or the SMTP credentials are missing, every send is a silent no-op so CLI-only
users are unaffected. Stdlib-only (smtplib/ssl) and never raises — a report
failure must never take down a build or its monitor.

CLI:
    python email_report.py --once                 # send one summary now
    python email_report.py --final                # send the completion report
    python email_report.py --loop --interval 1800 # summary every 30 min until the
                                                   # build process exits, then a final
Config (harness/.env):
    EMAIL_ENABLED=true
    EMAIL_SMTP_HOST=smtp.gmail.com
    EMAIL_SMTP_PORT=587
    EMAIL_SMTP_USER=claudematthew321@gmail.com
    EMAIL_SMTP_PASSWORD=<16-char Gmail app password, no spaces>
    EMAIL_FROM=claudematthew321@gmail.com          # defaults to EMAIL_SMTP_USER
    EMAIL_TO=matthew.t.a@hotmail.com
"""
from __future__ import annotations

import argparse
import os
import re
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

import config  # noqa: F401  — imported for its .env loading side effect

HARNESS_DIR = Path(__file__).resolve().parent
DEFAULT_LOG = HARNESS_DIR / "_moba_test.log"


def _cfg(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def email_enabled() -> bool:
    return _cfg("EMAIL_ENABLED").lower() in ("1", "true", "yes", "on")


def send_email(subject: str, body: str, *, to: str | None = None) -> bool:
    """Send a plain-text email via STARTTLS SMTP. Returns True on success.

    No-op (returns False) when disabled or any credential is missing. Never raises.
    """
    if not email_enabled():
        return False
    host = _cfg("EMAIL_SMTP_HOST", "smtp.gmail.com")
    port = int(_cfg("EMAIL_SMTP_PORT", "587") or "587")
    user = _cfg("EMAIL_SMTP_USER")
    password = _cfg("EMAIL_SMTP_PASSWORD")
    sender = _cfg("EMAIL_FROM") or user
    recipient = (to or _cfg("EMAIL_TO")).strip()
    if not (host and user and password and sender and recipient):
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(user, password)
                s.send_message(msg)
        return True
    except Exception as exc:  # never let a report failure escape
        sys.stderr.write(f"[email_report] send failed: {exc}\n")
        return False


# --------------------------------------------------------------------------- #
# Summary construction — derived purely by parsing the build log (+ optional   #
# mission_control.json). No LLM, no network, $0.                               #
# --------------------------------------------------------------------------- #

def _tail(text: str, n: int) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def summarize_log(log_path: Path) -> str:
    """Build a brief-but-detailed plain-text summary from the build log."""
    if not log_path.exists():
        return f"No build log found at {log_path} yet."
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Could not read build log {log_path}: {exc}"

    def count(pat: str) -> int:
        return len(re.findall(pat, text, re.IGNORECASE))

    # Project intent (first boxed line block) — best-effort.
    intent_match = re.search(r"J-Claw Project.*?\n(.*?)\n[└┘]", text, re.DOTALL)
    intent = ""
    if intent_match:
        intent = " ".join(p.strip("│ ") for p in intent_match.group(1).splitlines()).strip()

    # Sub-projects seen (FORMAT 5).
    subprojects = re.findall(r"Sub-project:\s*([A-Za-z0-9_\- ]+)", text)
    # Stacks chosen.
    stacks = re.findall(r"Technical Architect:\s*stack=(\S+)\s+files=(\d+)", text)

    tasks_done = count(r"✓ done")
    escalations = count(r"\(escalated\)")
    errors = count(r"✗ error")
    truncations = count(r"may be truncated")
    auto_passes = count(r"auto-passing")
    stalled = count(r"Scheduler stalled")
    heal_attempts = count(r"EXECUTION_ERROR refinement")
    final_pass = count(r"VERDICT:\s*PASS")
    final_issues = count(r"VERDICT:\s*ISSUES|ISSUES FOUND")
    deploy = count(r"Running deployment hook")
    handoffs = count(r"Handoff report written")

    # Which rungs have engaged.
    rungs = sorted(set(re.findall(r"rung \d+:\s*([^\s(]+)", text)))

    lines: list[str] = []
    if intent:
        lines.append(f"Project: {intent}")
    lines.append("")
    lines.append("=== Progress ===")
    if subprojects:
        lines.append(f"Sub-projects launched (FORMAT 5): {len(subprojects)}")
        for sp in subprojects:
            lines.append(f"  - {sp.strip()}")
    if stacks:
        lines.append("Stacks chosen:")
        for st, fc in stacks:
            lines.append(f"  - {st} ({fc} files)")
    lines.append("")
    lines.append("=== Counters ===")
    lines.append(f"Tasks completed (✓ done) : {tasks_done}")
    lines.append(f"Heal/refinement attempts : {heal_attempts}")
    lines.append(f"Rung escalations         : {escalations}")
    lines.append(f"Worker rungs engaged     : {', '.join(rungs) if rungs else 'local only'}")
    lines.append(f"Hard errors (✗ error)    : {errors}")
    lines.append(f"Tasks stalled (no retry) : {stalled}")
    lines.append(f"Truncated worker outputs : {truncations}")
    lines.append(f"unit_test auto-passes    : {auto_passes}")
    lines.append(f"Final reviews PASS       : {final_pass}")
    lines.append(f"Final reviews w/ issues  : {final_issues}")
    lines.append(f"Deploy hooks fired       : {deploy}")
    lines.append(f"HANDOFF reports written  : {handoffs}")
    lines.append("")
    lines.append("=== Latest activity (log tail) ===")
    lines.append(_tail(text, 15))
    return "\n".join(lines)


def _proc_alive(pid: int) -> bool:
    """Best-effort liveness check, cross-platform."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import subprocess
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return True  # if unsure, assume alive so we don't stop reporting early


def _build_finished(log_path: Path) -> bool:
    """Heuristic: the whole run is done when the log shows a terminal marker and
    has been quiet (no growth) — caller pairs this with a PID check."""
    if not log_path.exists():
        return False
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return bool(re.search(r"All sub-projects complete|Pipeline (complete|finished)|"
                          r"build (PASSED|FAILED)|DONE\b", text, re.IGNORECASE))


def build_label(log_path: Path, override: str | None = None) -> str:
    """Short, generic build label for email subjects.

    Precedence: explicit --label override → the project intent parsed from the log (first ~6 words)
    → "build". Replaces the old hardcoded "MOBA build" so the reporter works for any project."""
    if override and override.strip():
        return override.strip()
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    except Exception:
        text = ""
    m = re.search(r"J-Claw Project.*?\n(.*?)\n[└┘]", text, re.DOTALL)
    if m:
        intent = " ".join(p.strip("│ ") for p in m.group(1).splitlines()).strip()
        words = intent.split()
        if words:
            short = " ".join(words[:6])
            return short + ("…" if len(words) > 6 else "")
    return "build"


def main() -> int:
    ap = argparse.ArgumentParser(description="J-Claw build email reporter")
    ap.add_argument("--log", default=str(DEFAULT_LOG), help="Path to the build log")
    ap.add_argument("--once", action="store_true", help="Send one summary and exit")
    ap.add_argument("--final", action="store_true", help="Send the completion report and exit")
    ap.add_argument("--loop", action="store_true", help="Send a summary every --interval seconds")
    ap.add_argument("--interval", type=int, default=1800, help="Seconds between summaries (loop mode)")
    ap.add_argument("--pid", type=int, default=0, help="Build process PID; loop stops + sends final when it exits")
    ap.add_argument("--to", default=None, help="Override recipient")
    ap.add_argument("--label", default=None,
                    help="Short build label for email subjects (default: the project intent parsed "
                         "from the log, else 'build')")
    args = ap.parse_args()

    # Windows consoles default to cp1252; the log tail contains ✓/✗/box glyphs.
    # Email bodies are UTF-8 via EmailMessage, but the stdout fallback needs this.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    log_path = Path(args.log)

    if not email_enabled():
        sys.stderr.write(
            "[email_report] EMAIL_ENABLED is not true (or SMTP creds missing). "
            "Set EMAIL_* in harness/.env to activate. Printing summary instead:\n\n"
        )
        print(summarize_log(log_path))
        return 0

    label = build_label(log_path, args.label)

    if args.once or args.final:
        kind = "FINAL" if args.final else "UPDATE"
        subject = f"[J-Claw {kind}] {label} — {time.strftime('%Y-%m-%d %H:%M')}"
        ok = send_email(subject, summarize_log(log_path), to=args.to)
        print("sent" if ok else "send failed / disabled")
        return 0 if ok else 1

    if args.loop:
        from dotenv import load_dotenv  # re-read .env each tick so a freshly-pasted
        sent = 0                         # app password activates a running loop.
        while True:
            load_dotenv(override=True)
            ready = bool(_cfg("EMAIL_SMTP_PASSWORD") and _cfg("EMAIL_SMTP_USER"))
            done = (args.pid and not _proc_alive(args.pid)) or _build_finished(log_path)
            if ready:
                subject = f"[J-Claw UPDATE #{sent + 1}] {label} — {time.strftime('%Y-%m-%d %H:%M')}"
                if send_email(subject, summarize_log(log_path), to=args.to):
                    sent += 1
                if done:
                    fsubject = f"[J-Claw FINAL] {label} complete — {time.strftime('%Y-%m-%d %H:%M')}"
                    send_email(fsubject, "BUILD FINISHED.\n\n" + summarize_log(log_path), to=args.to)
                    break
                time.sleep(max(60, args.interval))
            else:
                # Credentials not in place yet — poll lightly until they appear, then
                # send the first update immediately rather than after a full interval.
                if done:
                    break
                time.sleep(60)
        return 0

    # No mode flag → print summary to stdout.
    print(summarize_log(log_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
