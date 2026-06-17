#!/usr/bin/env python3
"""Claude Code Mission Control — read-only local dashboard server.

Watches the active Claude Code session JSONL transcript for THIS repo and
renders it as a live, read-only dashboard (session vitals, timeline, sub-agent
fleet, tokens/context, tasks/files/git).

Usage:
    python cc_dashboard.py                  # http://127.0.0.1:8766/cc_dashboard/index.html
    python cc_dashboard.py --port 8766 --no-browser
    python cc_dashboard.py --session <uuid> # pin a specific session instead of newest

SECURITY: defaults to --host 127.0.0.1 (localhost-only). The transcript
contains raw prompt + tool I/O; do NOT bind 0.0.0.0 / advertise a LAN URL.

A background tailer thread parses the session JSONL into a normalized
``cc_state`` dict and writes it atomically to ``cc_state.json`` every ~1s.
The HTTP server only serves static files (the HTML + cc_state.json), exactly
like dashboard.py serves mission_control.json — no custom GET handler needed.
"""
import argparse
import glob
import http.server
import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent
CC_STATE = ROOT / "cc_state.json"

# Claude Code stores per-project session transcripts here. The project dir name
# is the cwd with separators flattened to '-'.
SESSION_DIR = Path(
    os.path.expanduser(
        r"~/.claude/projects/C--Users-Tyler-Desktop-Jarvis-Claw"
    )
)

_REQUEST_TIMEOUT_S = 10
_CONTEXT_WINDOW = 200_000  # ~200K context window for the fill-% gauge
_TIMELINE_CAP = 400        # keep the timeline bounded in memory / on disk
_MAX_TEXT = 600            # truncate long strings before they reach the browser

# Per-million-token pricing (Opus-class, USD). Used only for a rough estimate;
# cache-read is billed at a fraction of the input rate.
_PRICE_INPUT = 5.0
_PRICE_OUTPUT = 25.0
_PRICE_CACHE_WRITE = 6.25
_PRICE_CACHE_READ = 0.50

_STATE_LOCK = threading.Lock()


class _ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(_REQUEST_TIMEOUT_S)
        except (OSError, AttributeError):
            pass

    def log_message(self, *args):
        pass  # suppress per-request noise


# ── Helpers ────────────────────────────────────────────────────────────────


def _truncate(value, limit=_MAX_TEXT):
    if value is None:
        return None
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    s = s.replace("\r\n", "\n")
    if len(s) > limit:
        return s[:limit] + " …"
    return s


def _epoch_from_iso(ts):
    """Parse an ISO-8601 timestamp (e.g. 2026-06-17T19:27:44.393Z) to epoch."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        clean = ts.replace("Z", "+00:00")
        from datetime import datetime

        return datetime.fromisoformat(clean).timestamp()
    except Exception:
        return None


def _short_input(name, inp):
    """One-line summary of a tool_use input for the timeline."""
    if not isinstance(inp, dict):
        return _truncate(inp, 120)
    for key in ("command", "pattern", "file_path", "path", "query",
                "description", "prompt", "url", "skill", "subagent_type"):
        if inp.get(key):
            return _truncate(str(inp[key]), 160)
    return _truncate(inp, 160)


def _find_session_file(session_id=None):
    """Newest top-level <uuid>.jsonl in SESSION_DIR.

    CRITICAL: glob the top level ONLY — never recurse into subagents/, or the
    tailer latches onto a sub-agent transcript instead of the parent session.
    """
    if session_id:
        cand = SESSION_DIR / f"{session_id}.jsonl"
        return cand if cand.exists() else None
    files = [f for f in glob.glob(str(SESSION_DIR / "*.jsonl"))]
    if not files:
        return None
    return Path(max(files, key=os.path.getmtime))


# ── Tailer ─────────────────────────────────────────────────────────────────


class Tailer:
    """Incrementally parses the active session JSONL into a normalized state."""

    def __init__(self, session_id=None):
        self.pinned_session = session_id
        self.path = None
        self.offset = 0
        self.partial = ""          # buffered trailing partial line
        self.reset_state()

    def reset_state(self):
        self.offset = 0
        self.partial = ""
        self.started_epoch = None
        self.last_event_epoch = None
        self.session_id = None
        self.cwd = None
        self.git_branch = None
        self.model = None
        self.mode = None
        self.permission_mode = None
        self.ai_title = None
        self.timeline = []         # ordered list of normalized events
        self.tool_calls = {}       # tool_use_id -> {name, input, ...}
        self.agents = {}           # agentId -> spawn record
        self.agent_order = []      # spawn order
        self.prs = {}              # prNumber -> url
        self.files = {}            # path -> last-touched epoch
        # token accounting
        self.cost_tokens = 0       # input + output + cache_creation across turns
        self.cache_read_tokens = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_tokens = 0
        self.last_ctx = 0          # last assistant turn's context-window fill
        self.assistant_turns = 0

    # -- file selection / rotation ------------------------------------------

    def _select_file(self):
        path = _find_session_file(self.pinned_session)
        if path is None:
            return
        if self.path != path:
            # New active session: start fresh.
            self.path = path
            self.reset_state()

    def _read_appended(self):
        if not self.path or not self.path.exists():
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            # Truncation / rotation — re-read from the top.
            self.reset_state()
        new_lines = []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
                self.offset = fh.tell()
        except OSError:
            return []
        data = self.partial + chunk
        parts = data.split("\n")
        self.partial = parts.pop()  # trailing partial (no newline yet)
        for line in parts:
            line = line.strip()
            if line:
                new_lines.append(line)
        return new_lines

    # -- record dispatch ----------------------------------------------------

    def poll(self):
        self._select_file()
        for line in self._read_appended():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            try:
                self._handle(rec)
            except Exception:
                # Defensive: a single malformed record must never kill the tailer.
                continue
        self._refresh_agents()

    def _handle(self, rec):
        rtype = rec.get("type")
        # Common context fields (only present on assistant/user/attachment/system).
        if rec.get("cwd"):
            self.cwd = rec.get("cwd")
        if rec.get("gitBranch"):
            self.git_branch = rec.get("gitBranch")
        if rec.get("sessionId"):
            self.session_id = rec.get("sessionId")
        ts_epoch = _epoch_from_iso(rec.get("timestamp"))
        if ts_epoch:
            if self.started_epoch is None:
                self.started_epoch = ts_epoch
            self.last_event_epoch = ts_epoch

        if rtype == "assistant":
            self._handle_assistant(rec, ts_epoch)
        elif rtype == "user":
            self._handle_user(rec, ts_epoch)
        elif rtype == "mode":
            self.mode = rec.get("mode")
        elif rtype == "permission-mode":
            self.permission_mode = rec.get("permissionMode")
        elif rtype == "ai-title":
            self.ai_title = rec.get("aiTitle")
        elif rtype == "pr-link":
            num = str(rec.get("prNumber") or "")
            if num:
                self.prs[num] = rec.get("prUrl")
        elif rtype == "file-history-snapshot":
            self._handle_file_snapshot(rec, ts_epoch)
        # mode/permission-mode/ai-title/last-prompt/attachment/queue-operation/
        # agent-name/system carry no universal key set — handled above or skipped.

    def _handle_assistant(self, rec, ts_epoch):
        msg = rec.get("message") or {}
        if msg.get("model"):
            self.model = msg.get("model")
        usage = msg.get("usage") or {}
        if usage:
            inp = int(usage.get("input_tokens") or 0)
            out = int(usage.get("output_tokens") or 0)
            cw = int(usage.get("cache_creation_input_tokens") or 0)
            cr = int(usage.get("cache_read_input_tokens") or 0)
            # Cost tokens = Σ (input + output + cache_creation). cache_read is
            # summed SEPARATELY (different billing rate; summing it as new tokens
            # double-counts the prefix every turn → meaningless totals).
            self.input_tokens += inp
            self.output_tokens += out
            self.cache_creation_tokens += cw
            self.cache_read_tokens += cr
            self.cost_tokens += inp + out + cw
            # Context-window fill = the LAST turn's input + cache_read +
            # cache_creation (NOT a running sum).
            self.last_ctx = inp + cr + cw
            self.assistant_turns += 1

        content = msg.get("content")
        text_parts = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    text_parts.append(block["text"])
                elif btype == "tool_use":
                    self._handle_tool_use(block, rec, ts_epoch)
        elif isinstance(content, str):
            text_parts.append(content)

        text = "\n".join(p for p in text_parts if p).strip()
        if text:
            self._push({
                "kind": "assistant",
                "ts": ts_epoch,
                "text": _truncate(text),
            })

    def _handle_tool_use(self, block, rec, ts_epoch):
        tool_id = block.get("id")
        name = block.get("name")
        inp = block.get("input") or {}
        self.tool_calls[tool_id] = {
            "name": name,
            "input": inp,
            "ts": ts_epoch,
        }
        if name == "Agent":
            # Sub-agent spawn — terminal status/duration live in the per-agent
            # transcript, not here. Record the launch; _refresh_agents() reads
            # status from subagents/agent-<id>.jsonl on every poll.
            self._push({
                "kind": "agent_spawn",
                "ts": ts_epoch,
                "name": name,
                "subagent_type": inp.get("subagent_type"),
                "summary": _short_input(name, inp),
            })
        else:
            self._push({
                "kind": "tool_use",
                "ts": ts_epoch,
                "tool_id": tool_id,
                "name": name,
                "summary": _short_input(name, inp),
            })

    def _handle_user(self, rec, ts_epoch):
        msg = rec.get("message") or {}
        content = msg.get("content")
        # Typed prompt.
        if isinstance(content, str) and rec.get("promptSource") == "typed":
            self._push({
                "kind": "user",
                "ts": ts_epoch,
                "text": _truncate(content),
            })
            return
        # Tool result(s).
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id")
                is_error = bool(block.get("is_error"))
                tur = rec.get("toolUseResult")
                # Sub-agent async launch — capture spawn metadata.
                if isinstance(tur, dict) and tur.get("status") == "async_launched":
                    self._register_agent(tur)
                    continue
                call = self.tool_calls.get(tool_id, {})
                out_summary = None
                interrupted = False
                if isinstance(tur, dict):
                    interrupted = bool(tur.get("interrupted"))
                    out = tur.get("stdout") or tur.get("stderr") or ""
                    if out:
                        out_summary = _truncate(out, 240)
                self._push({
                    "kind": "tool_result",
                    "ts": ts_epoch,
                    "tool_id": tool_id,
                    "name": call.get("name"),
                    "ok": not is_error and not interrupted,
                    "interrupted": interrupted,
                    "summary": out_summary,
                })

    def _register_agent(self, tur):
        agent_id = tur.get("agentId")
        if not agent_id:
            return
        if agent_id not in self.agents:
            self.agent_order.append(agent_id)
        self.agents[agent_id] = {
            "agentId": agent_id,
            # subagent_type isn't in the parent's async_launched payload; it's
            # read from subagents/agent-<id>.meta.json in _refresh_agents().
            "subagent_type": None,
            "resolvedModel": tur.get("resolvedModel"),
            "description": tur.get("description"),
            "outputFile": tur.get("outputFile"),
            "status": "launched",
            "duration_s": None,
            "result": None,
        }

    def _handle_file_snapshot(self, rec, ts_epoch):
        snap = rec.get("snapshot")
        # snapshot is a stringified dict; extract any plausible file paths.
        text = snap if isinstance(snap, str) else json.dumps(snap, default=str)
        import re

        for m in re.finditer(r"[A-Za-z]:\\\\[^'\"]+|/[^'\"]+\.[A-Za-z0-9]+", text):
            path = m.group(0)
            if len(path) < 256 and ("." in path):
                self.files[path] = ts_epoch

    def _push(self, event):
        self.timeline.append(event)
        if len(self.timeline) > _TIMELINE_CAP:
            self.timeline = self.timeline[-_TIMELINE_CAP:]

    # -- sub-agent fleet ----------------------------------------------------

    def _refresh_agents(self):
        """Read per-agent transcripts for terminal status + duration."""
        if not self.session_id:
            return
        subdir = SESSION_DIR / self.session_id / "subagents"
        if not subdir.is_dir():
            return
        for agent_id, info in self.agents.items():
            jsonl = subdir / f"agent-{agent_id}.jsonl"
            meta = subdir / f"agent-{agent_id}.meta.json"
            if meta.exists() and not info.get("subagent_type"):
                try:
                    md = json.loads(meta.read_text(encoding="utf-8"))
                    info["subagent_type"] = md.get("agentType")
                    if md.get("description"):
                        info["description"] = md.get("description")
                except Exception:
                    pass
            if not jsonl.exists():
                # Degrade gracefully: keep launched/model/description.
                continue
            try:
                self._read_agent_transcript(jsonl, info)
            except Exception:
                continue

    def _read_agent_transcript(self, jsonl, info):
        first_ts = None
        last_ts = None
        last_text = None
        last_stop = None
        running = False
        with open(jsonl, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _epoch_from_iso(rec.get("timestamp"))
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                if rec.get("type") == "assistant":
                    msg = rec.get("message") or {}
                    last_stop = msg.get("stop_reason")
                    content = msg.get("content")
                    if isinstance(content, list):
                        parts = [b.get("text") for b in content
                                 if isinstance(b, dict) and b.get("type") == "text"]
                        joined = "\n".join(p for p in parts if p).strip()
                        if joined:
                            last_text = joined
        if first_ts and last_ts:
            info["duration_s"] = max(0, int(last_ts - first_ts))
        # Heuristic: the agent file's mtime stops advancing once it finishes; a
        # final assistant turn with a stop_reason indicates completion.
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            mtime = None
        idle = (time.time() - mtime) if mtime else 0
        if last_stop and idle > 8:
            info["status"] = "done"
        elif last_stop:
            info["status"] = "running"
        else:
            info["status"] = "running"
        if last_text:
            info["result"] = _truncate(last_text, 240)

    # -- snapshot -----------------------------------------------------------

    def snapshot(self):
        now = time.time()
        idle_s = int(now - self.last_event_epoch) if self.last_event_epoch else None
        elapsed_s = int((self.last_event_epoch or now) - self.started_epoch) \
            if self.started_epoch else 0
        est_cost = (
            self.input_tokens / 1e6 * _PRICE_INPUT
            + self.output_tokens / 1e6 * _PRICE_OUTPUT
            + self.cache_creation_tokens / 1e6 * _PRICE_CACHE_WRITE
            + self.cache_read_tokens / 1e6 * _PRICE_CACHE_READ
        )
        agents = [self.agents[a] for a in self.agent_order]
        return {
            "updated_at_epoch": now,
            "session": {
                "id": self.session_id,
                "file": self.path.name if self.path else None,
                "cwd": self.cwd,
                "git_branch": self.git_branch,
                "model": self.model,
                "mode": self.mode,
                "permission_mode": self.permission_mode,
                "title": self.ai_title,
                "elapsed_s": elapsed_s,
                "idle_s": idle_s,
                "last_event_epoch": self.last_event_epoch,
            },
            "timeline": self.timeline[-_TIMELINE_CAP:],
            "agents": agents,
            "tokens": {
                "cost_tokens": self.cost_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_creation_tokens": self.cache_creation_tokens,
                "context_fill": self.last_ctx,
                "context_window": _CONTEXT_WINDOW,
                "context_pct": round(self.last_ctx / _CONTEXT_WINDOW * 100, 1),
                "assistant_turns": self.assistant_turns,
                "est_cost_usd": round(est_cost, 2),
            },
            "prs": [{"number": k, "url": v} for k, v in self.prs.items()],
            "files": sorted(self.files.keys(), key=lambda p: -(self.files[p] or 0))[:60],
        }


# ── Atomic write ────────────────────────────────────────────────────────────


def _write_state_atomic(state):
    tmp = CC_STATE.with_name(CC_STATE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, CC_STATE)


def _tailer_loop(session_id, stop_event):
    tailer = Tailer(session_id)
    while not stop_event.is_set():
        try:
            tailer.poll()
            with _STATE_LOCK:
                _write_state_atomic(tailer.snapshot())
        except Exception as exc:  # never let the loop die
            try:
                _write_state_atomic({
                    "updated_at_epoch": time.time(),
                    "error": str(exc),
                })
            except Exception:
                pass
        stop_event.wait(1.0)


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code Mission Control (read-only)"
    )
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address (default: 127.0.0.1 — localhost only; the "
             "transcript contains raw prompts/tool I/O, do NOT expose to LAN)",
    )
    parser.add_argument("--session", default=None,
                        help="Pin a specific session id (default: newest)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open browser")
    args = parser.parse_args()

    if not SESSION_DIR.is_dir():
        print(f"WARNING: session dir not found: {SESSION_DIR}")

    stop_event = threading.Event()
    t = threading.Thread(
        target=_tailer_loop, args=(args.session, stop_event), daemon=True
    )
    t.start()

    try:
        server = _ThreadingHTTPServer((args.host, args.port), _Handler)
        server.timeout = _REQUEST_TIMEOUT_S
    except OSError as exc:
        print(f"Cannot bind {args.host}:{args.port}: {exc}")
        print(f"Try: python cc_dashboard.py --port {args.port + 1}")
        sys.exit(1)

    local_url = f"http://{args.host}:{args.port}/cc_dashboard/index.html"
    print(f"Claude Code Mission Control: {local_url}")
    print("Localhost only (transcript holds raw prompts/tool I/O — not LAN-advertised).")
    print("Press Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(local_url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
