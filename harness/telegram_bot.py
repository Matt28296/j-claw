#!/usr/bin/env python3
"""
harness/telegram_bot.py — Telegram bot interface for the J-Claw pipeline.

Controls the pipeline and delivers notifications via Telegram.
Uses python-telegram-bot >= 20.0 (async / v20 API).

Builds are queued FIFO and executed strictly sequentially — one GPU, one
worker model, one build at a time. /run and /continue both enqueue; a single
queue-worker task drains the queue.

Required env vars:
  TELEGRAM_BOT_TOKEN   — BotFather token
  TELEGRAM_CHAT_ID     — (optional) restrict to one chat ID for security
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
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
# PID of the in-flight pipeline subprocess. Written on spawn, removed on exit.
# Lets a freshly-restarted bot tell whether a prior run is still alive.
_PIPELINE_PIDFILE = _HARNESS_DIR / ".pipeline.pid"

# Terminal pipeline states (mirrors state_writer.TERMINAL_STATES).
_TERMINAL_STATES = {"DONE", "NEEDS_FOLLOWUP", "FAILED", "CANCELED"}

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

@dataclass
class _Job:
    """One queued pipeline invocation (/run or /continue)."""

    kind: str                       # "run" | "continue"
    spec: str                       # intent text
    chat_id: int
    project_dir: Path | None = None  # set for kind="continue"
    cancelled: bool = False          # set by /cancel so the summary says so

    @property
    def label(self) -> str:
        if self.kind == "continue":
            return f"continue [{self.project_dir.name[:40]}]: {self.spec[:60]}"
        return self.spec[:80]

    def argv(self) -> list[str]:
        args = [sys.executable, str(_HARNESS_DIR / "main.py"), "--yes"]
        if self.kind == "continue":
            args += ["--continue", str(self.project_dir)]
        args.append(self.spec)
        return args


class _BotState:
    """Mutable singleton: FIFO job queue + the currently-running subprocess."""

    def __init__(self) -> None:
        self.pending: collections.deque[_Job] = collections.deque()
        self.current: _Job | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.start_time: float = 0.0
        self.worker: asyncio.Task | None = None  # single queue-drainer task
        self.new_job = asyncio.Event()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


_state = _BotState()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_terminal_state(state_name: str, message: str) -> None:
    """Patch mission_control.json to a terminal state from the bot process.

    The pipeline subprocess can't flush its own terminal state when it has been
    killed (cancel) or died with the bot (restart orphan). We patch the file
    directly, mirroring StateWriter._set_terminal_state(state_name, message).
    """
    try:
        state: dict = {}
        if _MISSION_CONTROL.exists():
            state = json.loads(_MISSION_CONTROL.read_text(encoding="utf-8"))
        now_ts = time.strftime("%H:%M:%S")
        now_epoch = time.time()
        state["pipeline_state"] = state_name
        state["active_agent"] = None
        state["terminal"] = {
            "state": state_name,
            "message": message,
            "recorded_at": now_ts,
        }
        events = state.get("events") or []
        events.insert(0, {"ts": now_ts, "msg": message})
        state["events"] = events[:100]
        for node in (state.get("agent_nodes") or {}).values():
            if node.get("status") == "running":
                node["status"] = state_name.lower()
                node["state"] = state_name
                node["updated_at_epoch"] = now_epoch
        state["sequence"] = int(state.get("sequence") or 0) + 1
        state["updated_at_epoch"] = now_epoch
        tmp = _MISSION_CONTROL.with_name(f".{_MISSION_CONTROL.name}.bot.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, _MISSION_CONTROL)
    except Exception as exc:
        logger.warning("Failed to write %s state: %s", state_name, exc)


def _write_canceled_state() -> None:
    """Write a CANCELED terminal state (user cancel via /cancel)."""
    _write_terminal_state("CANCELED", "Pipeline canceled by user")


def _pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is currently running.

    Dependency-free: ctypes/OpenProcess on Windows (alive iff exit code is
    STILL_ACTIVE), os.kill(pid, 0) elsewhere.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        STILL_ACTIVE = 259
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def _read_pipeline_pid() -> int | None:
    """Read the recorded pipeline PID, or None if absent/unreadable."""
    try:
        return int(_PIPELINE_PIDFILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _reconcile_exited_child(returncode: int | None) -> None:
    """Fail a run whose child exited without writing its own terminal state.

    Called after the pipeline subprocess exits while the bot is alive. A clean
    run already wrote DONE/NEEDS_FOLLOWUP and a user cancel wrote CANCELED; in
    those cases the state is already terminal and we leave it. Only a crash or
    external kill leaves it non-terminal — patch it FAILED.
    """
    try:
        if not _MISSION_CONTROL.exists():
            return
        state = json.loads(_MISSION_CONTROL.read_text(encoding="utf-8"))
        if state.get("pipeline_state") in _TERMINAL_STATES:
            return
        logger.warning(
            "Pipeline exited (code=%s) without a terminal state — marking FAILED.",
            returncode,
        )
        _write_terminal_state("FAILED", "Pipeline exited without a terminal state")
    except Exception as exc:
        logger.warning("Exited-child reconciliation failed: %s", exc)


def _reconcile_orphaned_run() -> None:
    """On bot startup, fail any run left mid-flight by a previous bot instance.

    If mission_control.json shows a non-terminal run but its pipeline process is
    gone (pidfile missing or PID dead), the previous bot was killed mid-run and
    no terminal state was ever written — the dashboard would show EXECUTING
    forever. Mark it FAILED so it converges. If the PID is still alive, a real
    run survived the restart; leave it untouched.
    """
    try:
        if not _MISSION_CONTROL.exists():
            return
        state = json.loads(_MISSION_CONTROL.read_text(encoding="utf-8"))
        pipeline_state = state.get("pipeline_state")
        if pipeline_state in _TERMINAL_STATES or pipeline_state in (None, "IDLE"):
            return
        pid = _read_pipeline_pid()
        if pid is not None and _pid_alive(pid):
            logger.warning(
                "Startup: run still in %s with live pipeline PID %d — leaving as-is.",
                pipeline_state, pid,
            )
            return
        logger.warning(
            "Startup: orphaned run in %s (pid=%s, not alive) — marking FAILED.",
            pipeline_state, pid,
        )
        _write_terminal_state("FAILED", "Pipeline orphaned by bot restart")
        _PIPELINE_PIDFILE.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Startup reconciliation failed: %s", exc)


def _guard(update: Update) -> bool:
    """Return True if the chat is allowed to use this bot."""
    if ALLOWED_CHAT_ID is None:
        return True
    return update.effective_chat is not None and update.effective_chat.id == ALLOWED_CHAT_ID


async def _send(bot, chat_id: int, text: str) -> None:
    """Send a message, splitting at _MAX_MSG chars if needed."""
    text = text.strip()
    if not text:
        return
    while text:
        chunk, text = text[:_MAX_MSG], text[_MAX_MSG:]
        await bot.send_message(chat_id=chat_id, text=chunk)


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


def _queue_lines() -> str:
    """Human-readable pending-queue block ('' when empty)."""
    if not _state.pending:
        return ""
    lines = [f"\nQueued ({len(_state.pending)}):"]
    for i, job in enumerate(_state.pending, 1):
        lines.append(f"  {i}. {job.label[:70]}")
    return "\n".join(lines)


# ── Queue worker ──────────────────────────────────────────────────────────────

async def _enqueue(job: _Job, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Append a job and make sure the single queue-worker task is alive."""
    _state.pending.append(job)
    _state.new_job.set()
    if _state.worker is None or _state.worker.done():
        _state.worker = asyncio.create_task(
            _queue_worker(context.bot), name="queue-worker"
        )
    if _state.current is None and len(_state.pending) == 1:
        await _send(context.bot, job.chat_id, f"Starting now:\n{job.label}")
    else:
        ahead = len(_state.pending) - 1 + (1 if _state.current else 0)
        await _send(
            context.bot,
            job.chat_id,
            f"Queued at position {len(_state.pending)} — {ahead} job(s) ahead:\n{job.label}",
        )


async def _queue_worker(bot) -> None:
    """Drain the queue strictly sequentially (single GPU → one build at a time)."""
    while True:
        await _state.new_job.wait()
        _state.new_job.clear()
        while _state.pending:
            job = _state.pending.popleft()
            _state.current = job
            _state.start_time = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *job.argv(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(_HARNESS_DIR),
                    env={**os.environ, "PYTHONUTF8": "1"},
                )
            except Exception as exc:
                await _send(bot, job.chat_id, f"Failed to start pipeline: {exc}")
                _state.current = None
                continue
            _state.proc = proc
            try:
                _PIPELINE_PIDFILE.write_text(str(proc.pid), encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not write pidfile: %s", exc)
            await _send(
                bot, job.chat_id,
                f"Pipeline started:\n{job.label}\n\nStreaming output below...",
            )
            try:
                await _stream_output(proc, job, bot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad job must not kill the worker
                logger.error("Job stream failed: %s", exc)
            finally:
                # If the child actually exited (returncode set) without writing its
                # own terminal state (crash / external kill), patch it FAILED so the
                # dashboard converges. Skip when returncode is None — the child is
                # still alive (e.g. worker cancelled on shutdown, or proc.wait timed
                # out); marking a live run FAILED would be wrong. /cancel already
                # wrote CANCELED and a clean run wrote DONE/NEEDS_FOLLOWUP, both of
                # which are skipped by the terminal-state check inside.
                if proc.returncode is not None:
                    _PIPELINE_PIDFILE.unlink(missing_ok=True)
                    if not (_state.current and _state.current.cancelled):
                        _reconcile_exited_child(proc.returncode)
                _state.proc = None
                _state.current = None
            if _state.pending:
                await _send(bot, job.chat_id,
                            f"{len(_state.pending)} job(s) still queued — starting next...")


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return
    text = (
        "J-Claw Pipeline Bot\n\n"
        "Commands:\n"
        "  /run <spec>                — queue a new pipeline run\n"
        "  /continue <project> <spec> — queue a feature addition to an existing project\n"
        "  /status                    — current build + queue\n"
        "  /cancel                    — kill the running build (queue continues)\n"
        "  /cancel queue              — clear queued (not running) jobs\n"
        "  /cancel all                — clear queue and kill the running build\n"
        "  /projects                  — list 5 most recent projects\n"
        "  /start                     — show this message\n\n"
        "Builds run one at a time; extra requests queue FIFO.\n\n"
        "Examples:\n"
        "  /run A Phaser 3 Snake game with high score tracking\n"
        "  /continue portfolio Add a dark-mode toggle"
    )
    await _send(context.bot, update.effective_chat.id, text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return

    chat_id = update.effective_chat.id

    if _state.current is not None:
        elapsed = int(time.time() - _state.start_time)
        mins, secs = divmod(elapsed, 60)
        mc = _read_mission_control()
        pipeline_state = mc.get("pipeline_state", "RUNNING")
        tasks = mc.get("tasks", [])
        done = sum(1 for t in tasks if t.get("status") == "done")
        total = len(tasks)
        intent = mc.get("project", {}).get("intent", _state.current.spec)[:80]

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

    text += _queue_lines()
    await _send(context.bot, chat_id, text)


async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return

    projects = _recent_projects(5)
    if not projects:
        await _send(context.bot, update.effective_chat.id, "No projects found.")
        return

    lines = ["Recent projects:\n"]
    for i, (name, date) in enumerate(projects, 1):
        lines.append(f"{i}. [{date}] {name[:55]}")
    await _send(context.bot, update.effective_chat.id, "\n".join(lines))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return

    chat_id = update.effective_chat.id
    arg = context.args[0].lower() if context.args else ""

    if arg in ("queue", "all"):
        n = len(_state.pending)
        _state.pending.clear()
        await _send(context.bot, chat_id, f"Cleared {n} queued job(s).")
        if arg == "queue":
            return

    if not _state.running:
        await _send(context.bot, chat_id,
                    "No pipeline is currently running." + _queue_lines())
        return

    if _state.current is not None:
        _state.current.cancelled = True
    try:
        _state.proc.terminate()
        await asyncio.sleep(2)
        if _state.running:
            _state.proc.kill()
    except Exception as exc:
        logger.warning("Error killing subprocess: %s", exc)

    _write_canceled_state()

    await _send(context.bot, chat_id, "Pipeline cancelled." +
                (" Next queued job will start." if _state.pending else ""))


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return

    chat_id = update.effective_chat.id

    spec = " ".join(context.args).strip() if context.args else ""
    if not spec:
        await _send(
            context.bot, chat_id,
            "Usage: /run <spec>\nExample: /run A Phaser 3 Snake game with high score tracking",
        )
        return

    await _enqueue(_Job(kind="run", spec=spec, chat_id=chat_id), context)


async def cmd_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return

    chat_id = update.effective_chat.id
    if not context.args or len(context.args) < 2:
        await _send(
            context.bot, chat_id,
            "Usage: /continue <project-name-fragment> <feature description>\n"
            "Example: /continue portfolio Add a dark-mode toggle\n"
            "Use /projects to see recent project names.",
        )
        return

    fragment = context.args[0].lower()
    intent = " ".join(context.args[1:]).strip()

    dirs = [d for d in _PROJECTS_DIR.iterdir() if d.is_dir()] if _PROJECTS_DIR.exists() else []
    matches = [d for d in dirs if fragment in d.name.lower()]

    if not matches:
        await _send(context.bot, chat_id,
                    f"No project matches '{fragment}'. Use /projects to list recent ones.")
        return
    if len(matches) > 1:
        names = "\n".join(f"  - {d.name[:60]}" for d in matches[:5])
        await _send(context.bot, chat_id,
                    f"'{fragment}' is ambiguous ({len(matches)} matches):\n{names}\n"
                    "Use a longer fragment.")
        return

    project_dir = matches[0]
    if not (project_dir / "spec.json").exists():
        await _send(context.bot, chat_id,
                    f"Project '{project_dir.name[:60]}' has no spec.json — "
                    "it cannot be continued (was it a failed/partial build?).")
        return

    await _enqueue(
        _Job(kind="continue", spec=intent, chat_id=chat_id, project_dir=project_dir),
        context,
    )


async def _stream_output(
    proc: asyncio.subprocess.Process,
    job: _Job,
    bot,
) -> None:
    """Read subprocess stdout and send batched updates to Telegram."""
    buffer: list[str] = []
    last_flush = time.monotonic()
    chat_id = job.chat_id

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
            await _send(bot, chat_id, text)
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
        intent = mc.get("project", {}).get("intent", job.spec)[:80]
        output_dir = mc.get("project", {}).get("output_dir", "unknown")

        if job.cancelled:
            verdict = "CANCELLED"
        elif rc == 0:
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
        await _send(bot, chat_id, summary)


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
    app.add_handler(CommandHandler("continue", cmd_continue))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("projects", cmd_projects))

    # A prior bot instance may have been killed mid-run, leaving mission_control
    # stuck in a non-terminal state. Converge it before we start serving.
    _reconcile_orphaned_run()

    if ALLOWED_CHAT_ID:
        logger.info("Bot started — restricted to chat ID %d", ALLOWED_CHAT_ID)
    else:
        logger.info("Bot started — WARNING: no TELEGRAM_CHAT_ID set, accepting all chats")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
