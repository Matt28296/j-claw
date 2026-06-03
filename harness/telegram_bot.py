#!/usr/bin/env python3
"""
harness/telegram_bot.py — Telegram bot interface for the J-Claw pipeline.

Controls the pipeline and delivers notifications via Telegram.
Uses python-telegram-bot >= 20.0 (async / v20 API).

Required env vars:
  TELEGRAM_BOT_TOKEN   — BotFather token
  TELEGRAM_CHAT_ID     — (optional) restrict to one chat ID for security
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_ID: int | None = (
    int(os.getenv("TELEGRAM_CHAT_ID"))
    if os.getenv("TELEGRAM_CHAT_ID")
    else None
)

# Paths relative to this file
_HARNESS_DIR = Path(__file__).parent
_PROJECTS_DIR = Path(os.getenv("PROJECTS_DIR", str(_HARNESS_DIR / "projects")))
_MISSION_CONTROL = _HARNESS_DIR.parent / "mission_control.json"

# Telegram message size limit
_MAX_MSG = 4096

# Batching: flush after this many stdout lines or this many seconds
_BATCH_LINES = 8
_BATCH_SECONDS = 30.0

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── State ─────────────────────────────────────────────────────────────────────

class _BotState:
    """Mutable singleton holding the currently-running subprocess (if any)."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.spec: str = ""
        self.start_time: float = 0.0
        self.task: asyncio.Task | None = None   # background streaming task

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


_state = _BotState()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _guard(update: Update) -> bool:
    """Return True if the chat is allowed to use this bot."""
    if ALLOWED_CHAT_ID is None:
        return True
    return update.effective_chat is not None and update.effective_chat.id == ALLOWED_CHAT_ID


async def _send(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message, splitting at _MAX_MSG chars if needed."""
    text = text.strip()
    if not text:
        return
    while text:
        chunk, text = text[:_MAX_MSG], text[_MAX_MSG:]
        await context.bot.send_message(chat_id=chat_id, text=chunk)


def _escape_md(text: str) -> str:
    """Minimal MarkdownV2 escaping — only used for fixed-format blocks."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _read_mission_control() -> dict:
    try:
        return json.loads(_MISSION_CONTROL.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _newest_project_dir() -> Path | None:
    """Return the most-recently-modified project subfolder, or None."""
    if not _PROJECTS_DIR.exists():
        return None
    dirs = [d for d in _PROJECTS_DIR.iterdir() if d.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime)


def _read_tasks_summary(project_dir: Path) -> tuple[int, int]:
    """Return (done, total) tasks from tasks_done.json, or (0,0)."""
    tasks_file = project_dir / "tasks_done.json"
    if not tasks_file.exists():
        return 0, 0
    try:
        tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
        done = sum(1 for t in tasks if t.get("status") == "done")
        return done, len(tasks)
    except Exception:
        return 0, 0


def _recent_projects(n: int = 5) -> list[tuple[str, str]]:
    """Return [(name, date_str), ...] for the N most recent project folders."""
    if not _PROJECTS_DIR.exists():
        return []
    dirs = sorted(
        [d for d in _PROJECTS_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    result = []
    for d in dirs[:n]:
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime))
        result.append((d.name, mtime))
    return result


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return
    text = (
        "J-Claw Pipeline Bot\n\n"
        "Commands:\n"
        "  /run <spec>   — start a new pipeline run\n"
        "  /status       — show current pipeline status\n"
        "  /cancel       — kill the running pipeline\n"
        "  /projects     — list 5 most recent projects\n"
        "  /start        — show this message\n\n"
        "Example:\n"
        "  /run A Phaser 3 Snake game with high score tracking"
    )
    await _send(update.effective_chat.id, text, context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return

    chat_id = update.effective_chat.id

    if _state.running:
        elapsed = int(time.time() - _state.start_time)
        mins, secs = divmod(elapsed, 60)
        mc = _read_mission_control()
        pipeline_state = mc.get("pipeline_state", "RUNNING")
        tasks = mc.get("tasks", [])
        done = sum(1 for t in tasks if t.get("status") == "done")
        total = len(tasks)
        intent = mc.get("project", {}).get("intent", _state.spec)[:80]

        text = (
            f"Pipeline RUNNING\n"
            f"Spec: {intent}\n"
            f"State: {pipeline_state}\n"
            f"Tasks: {done}/{total} done\n"
            f"Elapsed: {mins}m {secs:02d}s"
        )
    else:
        mc = _read_mission_control()
        pipeline_state = mc.get("pipeline_state", "IDLE")
        project_dir = _newest_project_dir()

        if project_dir:
            done, total = _read_tasks_summary(project_dir)
            text = (
                f"Pipeline IDLE (last state: {pipeline_state})\n"
                f"Last project: {project_dir.name[:60]}\n"
                f"Tasks completed: {done}/{total}"
            )
        else:
            text = f"Pipeline IDLE (state: {pipeline_state})\nNo projects found."

    await _send(chat_id, text, context)


async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return

    projects = _recent_projects(5)
    if not projects:
        await _send(update.effective_chat.id, "No projects found.", context)
        return

    lines = ["Recent projects:\n"]
    for i, (name, date) in enumerate(projects, 1):
        lines.append(f"{i}. [{date}] {name[:55]}")
    await _send(update.effective_chat.id, "\n".join(lines), context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return

    if not _state.running:
        await _send(update.effective_chat.id, "No pipeline is currently running.", context)
        return

    try:
        _state.proc.terminate()
        await asyncio.sleep(2)
        if _state.running:
            _state.proc.kill()
    except Exception as exc:
        logger.warning("Error killing subprocess: %s", exc)

    # Cancel the streaming background task
    if _state.task and not _state.task.done():
        _state.task.cancel()

    _state.proc = None
    _state.task = None
    await _send(update.effective_chat.id, "Pipeline cancelled.", context)


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return

    chat_id = update.effective_chat.id

    if _state.running:
        await _send(
            chat_id,
            "A pipeline is already running. Use /cancel to stop it first.",
            context,
        )
        return

    # Extract spec from command arguments
    spec = " ".join(context.args).strip() if context.args else ""
    if not spec:
        await _send(
            chat_id,
            "Usage: /run <spec>\nExample: /run A Phaser 3 Snake game with high score tracking",
            context,
        )
        return

    await _send(chat_id, f"Starting pipeline:\n{spec}\n\nStreaming output below...", context)

    # Launch the subprocess
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_HARNESS_DIR / "main.py"),
            "--yes",
            spec,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(_HARNESS_DIR),
        )
    except Exception as exc:
        await _send(chat_id, f"Failed to start pipeline: {exc}", context)
        return

    _state.proc = proc
    _state.spec = spec
    _state.start_time = time.time()

    # Launch background streaming task
    _state.task = asyncio.create_task(
        _stream_output(proc, spec, chat_id, context),
        name="pipeline-stream",
    )


async def _stream_output(
    proc: asyncio.subprocess.Process,
    spec: str,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Read subprocess stdout and send batched updates to Telegram."""
    buffer: list[str] = []
    last_flush = time.monotonic()

    async def flush() -> None:
        nonlocal last_flush
        if not buffer:
            return
        text = "\n".join(buffer)
        # Strip ANSI escape codes for cleaner Telegram display
        import re
        text = re.sub(r"\x1b\[[0-9;]*[mGKHF]", "", text)
        text = text.strip()
        if text:
            await _send(chat_id, text, context)
        buffer.clear()
        last_flush = time.monotonic()

    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            buffer.append(line)

            now = time.monotonic()
            if len(buffer) >= _BATCH_LINES or (now - last_flush) >= _BATCH_SECONDS:
                await flush()

        # Flush any remaining lines
        await flush()

    except asyncio.CancelledError:
        await flush()
        raise
    except Exception as exc:
        logger.error("Stream error: %s", exc)
        await flush()
    finally:
        # Wait for process to exit and collect return code
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            pass

        rc = proc.returncode
        elapsed = int(time.time() - _state.start_time)
        mins, secs = divmod(elapsed, 60)

        # Read final state from mission_control.json for the summary
        mc = _read_mission_control()
        pipeline_state = mc.get("pipeline_state", "UNKNOWN")
        tasks = mc.get("tasks", [])
        done = sum(1 for t in tasks if t.get("status") == "done")
        total = len(tasks)
        intent = mc.get("project", {}).get("intent", spec)[:80]
        output_dir = mc.get("project", {}).get("output_dir", "unknown")

        if rc == 0:
            verdict = "PASSED"
        elif rc is None:
            verdict = "CANCELLED"
        else:
            verdict = f"FAILED (exit {rc})"

        summary = (
            f"Pipeline complete\n"
            f"Project: {intent}\n"
            f"Tasks: {done}/{total} done\n"
            f"Verdict: {verdict}\n"
            f"State: {pipeline_state}\n"
            f"Output: {output_dir}\n"
            f"Elapsed: {mins}m {secs:02d}s"
        )
        await _send(chat_id, summary, context)

        # Clear running state
        _state.proc = None
        _state.task = None


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        print(
            "ERROR: TELEGRAM_BOT_TOKEN is not set.\n"
            "Add it to harness/.env or set it as an environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("projects", cmd_projects))

    if ALLOWED_CHAT_ID:
        logger.info("Bot started — restricted to chat ID %d", ALLOWED_CHAT_ID)
    else:
        logger.info("Bot started — WARNING: no TELEGRAM_CHAT_ID set, accepting all chats")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
