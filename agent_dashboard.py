#!/usr/bin/env python3
"""Agent Mission Control — a standalone local dashboard for the Claude Code agent swarm.

Reads agent state directly from disk (the Codex job store, per-session task buffers, and the
OS process table) and serves a Matrix-themed browser UI. The only data path is

    disk -> this server -> browser

It is NEVER  disk -> LLM, so the dashboard adds zero context to any Claude chat. The model builds
this server once; thereafter the server tails files itself and the browser renders them.

This is a sibling to the pipeline's dashboard.py (Jarvis-Claw build Mission Control). It reuses that
server's proven security model (localhost-only + optional token, _json_response) and the Windows
process-kill approach, but its data source and actions are entirely different: it watches the agents
we spawn inside a coding session and can cancel them.

Usage:
    python agent_dashboard.py                          # auto-detect everything, 127.0.0.1:8770
    python agent_dashboard.py --session-id <uuid>      # pin the Claude session to watch
    python agent_dashboard.py --port 8771 --no-browser
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── Paths / configuration ──────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
STATIC_ROOT = ROOT / "agent_dashboard"          # the Matrix UI lives here (built in Phase A)
CONTROL_TOKEN = os.getenv("AGENT_DASHBOARD_TOKEN", "")
COMPANION = Path(os.path.expanduser(
    "~/.claude/plugins/cache/openai-codex/codex/1.0.4/scripts/codex-companion.mjs"
))

_LOCAL_CLIENTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_MAX_BODY = 16 * 1024
_REQUEST_TIMEOUT_S = 10
_TAIL_LINES = 120

# Per-kind staleness thresholds (seconds). Codex jobs legitimately go quiet for minutes during a
# model turn; a quiet local task buffer means the task is done/dead. (Codex review: not one global.)
_STALE_CODEX_S = 240
_STALE_TASK_S = 25

# git/gh are slow and can hang on auth; never call them on the /api/agents hot path.
_GIT_TTL_S = 60
_GH_TIMEOUT_S = 8
_GIT_TIMEOUT_S = 5


def _slugify_repo(path: Path) -> str:
    """C:\\Users\\Tyler\\Desktop\\Jarvis-Claw -> C--Users-Tyler-Desktop-Jarvis-Claw (Claude's dir slug)."""
    return re.sub(r"[:\\/]", "-", str(path))


class Paths:
    """All resolved data-source locations, surfaced verbatim in /api/control-status (Codex RANK 3:
    discovery must be explicit/inspectable, never a silent guess)."""

    def __init__(self, repo: Path, session_id: str | None):
        self.repo = repo.resolve()
        self.slug = _slugify_repo(self.repo)
        self.session_id = session_id or self._autodetect_session()
        self.codex_state_dir = self._resolve_codex_state_dir()
        temp = Path(os.path.expandvars("%TEMP%")) / "claude" / self.slug
        self.tasks_dir = (temp / self.session_id / "tasks") if self.session_id else None
        proj = Path(os.path.expanduser("~/.claude/projects")) / self.slug
        self.transcript = (proj / f"{self.session_id}.jsonl") if self.session_id else None
        self.session_autodetected = session_id is None

    def _autodetect_session(self) -> str | None:
        """Advisory only: newest session .jsonl for this project. Pin with --session-id for control."""
        proj = Path(os.path.expanduser("~/.claude/projects")) / self.slug
        cands = sorted(glob.glob(str(proj / "*.jsonl")), key=_safe_mtime, reverse=True)
        return Path(cands[0]).stem if cands else None

    def _resolve_codex_state_dir(self) -> Path | None:
        root = Path(os.path.expanduser("~/.claude/plugins/data/codex-openai-codex/state"))
        if not root.is_dir():
            return None
        # Prefer the workspace dir whose stored jobs point back at this repo; fall back to newest.
        best, best_m = None, -1.0
        for d in root.iterdir():
            if not (d / "jobs").is_dir():
                continue
            m = _safe_mtime(d / "jobs")
            for jf in glob.glob(str(d / "jobs" / "*.json")):
                obj = _read_json(jf)
                if str(obj.get("workspaceRoot", "")).replace("\\", "/").rstrip("/") == str(self.repo).replace("\\", "/").rstrip("/"):
                    return d
            if m > best_m:
                best, best_m = d, m
        return best

    def as_dict(self) -> dict:
        return {
            "repo": str(self.repo),
            "session_id": self.session_id,
            "session_autodetected": self.session_autodetected,
            "codex_state_dir": str(self.codex_state_dir) if self.codex_state_dir else None,
            "tasks_dir": str(self.tasks_dir) if self.tasks_dir else None,
            "transcript": str(self.transcript) if self.transcript else None,
        }


# ── small fs helpers (all tolerant — never crash a poll on a bad file) ───────────────────────────
def _safe_mtime(p) -> float:
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def _read_json(path) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _tail_lines(path, n: int = _TAIL_LINES) -> list[str]:
    """Last n lines, partial-safe: drop a trailing line with no newline (mid-write). Never raises."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
    except Exception:
        return []
    if not data:
        return []
    # split() yields a trailing element that is either "" (file ended in \n) or a partial line
    # mid-write — in both cases it is not a complete line, so always drop it.
    parts = data.split("\n")[:-1]
    return parts[-n:]


def _pid_alive(pid) -> bool:
    """Liveness without a third-party dep. ctypes OpenProcess on Windows; os.kill(0) elsewhere."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            # STILL_ACTIVE == 259; a dead-but-not-reaped handle reports 259 rarely — good enough here.
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _iso_to_epoch(s) -> float:
    if not s or not isinstance(s, str):
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# ── agent model: scan disk -> registry + agent list ──────────────────────────────────────────────
class Registry:
    """Server-owned map of canonical id -> source (Codex RANK 2). Clients pass only ids; never paths,
    so transcript/cancel cannot be steered into path traversal."""

    def __init__(self):
        self._by_id: dict[str, dict] = {}

    def reset(self):
        self._by_id = {}

    def put(self, entry: dict):
        self._by_id[entry["id"]] = entry

    def get(self, agent_id: str) -> dict | None:
        return self._by_id.get(agent_id)


def _codex_status(job: dict, now: float) -> dict:
    """Trust the store's status only with an mtime/pid sanity check — the companion's status can stay
    'running' forever after the process dies (the known orphan bug)."""
    raw = str(job.get("status") or job.get("phase") or "unknown").lower()
    log = job.get("logFile")
    last = max(_safe_mtime(log) if log else 0.0, _iso_to_epoch(job.get("startedAt")))
    staleness = int(now - last) if last else None
    pid = job.get("pid")
    pid_live = _pid_alive(pid) if pid else False
    actions = []
    if raw in ("completed", "done", "cancelled", "canceled", "failed", "error"):
        status, reason, conf = ("done" if raw in ("completed", "done") else raw), f"store:{raw}", "high"
    elif pid and not pid_live:
        status, reason, conf = "orphan", "store says running but pid is dead", "high"
    elif staleness is not None and staleness > _STALE_CODEX_S:
        status, reason, conf = "orphan", f"no log activity for {staleness}s", "medium"
        actions = ["cancel"]
    else:
        status, reason, conf = "running", "log active", "high" if pid_live else "medium"
        actions = ["cancel"]
    return {
        "status": status, "status_reason": reason, "source_state": raw,
        "staleness_seconds": staleness, "confidence": conf, "actions": actions,
        "pid": pid, "pid_alive": pid_live, "last_active": last,
    }


def build_agents(paths: Paths, registry: Registry) -> list[dict]:
    registry.reset()
    now = time.time()
    agents: list[dict] = []

    # 1) Codex jobs — the primary, fully-structured source (state.json index + per-job json).
    if paths.codex_state_dir:
        for jf in glob.glob(str(paths.codex_state_dir / "jobs" / "*.json")):
            job = _read_json(jf)
            jid = job.get("id")
            if not jid:
                continue
            st = _codex_status(job, now)
            same_session = (not paths.session_id) or (job.get("sessionId") == paths.session_id)
            zombie = (not same_session) and st["pid_alive"]
            if not same_session and not zombie:
                continue  # other-session, already finished — belongs to History, not the live view
            agent_id = f"codex:{jid}"
            registry.put({
                "id": agent_id, "kind": "codex", "job_id": jid,
                "log_file": job.get("logFile"), "pid": job.get("pid"),
            })
            agents.append({
                "id": agent_id, "kind": "codex",
                "title": job.get("title") or "Codex job",
                "summary": (job.get("summary") or "")[:200],
                "label": job.get("kindLabel") or job.get("kind"),
                "session_id": job.get("sessionId"),
                "started_at": job.get("startedAt") or job.get("createdAt"),
                "completed_at": job.get("completedAt"),
                "tokens": _codex_tokens(job),
                "zombie": zombie,
                **st,
            })

    # 2) Session task buffers — best-effort liveness only (transient, heterogeneous; no cancel).
    if paths.tasks_dir and paths.tasks_dir.is_dir():
        for of in glob.glob(str(paths.tasks_dir / "*.output")):
            stem = Path(of).stem
            size = 0
            try:
                size = os.path.getsize(of)
            except OSError:
                pass
            last = _safe_mtime(of)
            staleness = int(now - last) if last else None
            if size == 0 and (staleness is None or staleness > _STALE_TASK_S):
                continue  # emptied + idle == finished/cleared; skip noise
            fresh = staleness is not None and staleness <= _STALE_TASK_S
            agent_id = f"task:{stem}"
            registry.put({"id": agent_id, "kind": "task", "log_file": of, "pid": None})
            agents.append({
                "id": agent_id, "kind": "task", "title": f"session task {stem}",
                "summary": "", "label": "bash/subagent",
                "status": "running" if fresh else "stale",
                "status_reason": "buffer growing" if fresh else f"idle {staleness}s",
                "source_state": "buffer", "staleness_seconds": staleness,
                "confidence": "low", "actions": [], "pid": None, "pid_alive": False,
                "last_active": last, "started_at": None, "completed_at": None,
                "tokens": None, "zombie": False,
            })

    agents.sort(key=lambda a: (a.get("status") != "running", -(a.get("last_active") or 0)))
    return agents


def _codex_tokens(job: dict) -> dict | None:
    res = job.get("result")
    if not isinstance(res, dict):
        return None
    usage = res.get("usage") if isinstance(res.get("usage"), dict) else res
    out = {}
    for k in ("input_tokens", "output_tokens", "total_tokens", "tokens"):
        if isinstance(usage.get(k), (int, float)):
            out[k] = usage[k]
    return out or None


def _session_token_totals(agents: list[dict]) -> dict:
    """Roll up per-agent token counts into a per-model session total.

    Each agent may carry a ``tokens`` dict with ``input_tokens``, ``output_tokens``,
    ``total_tokens``, and/or ``tokens``.  The model key is taken from the agent's
    ``label`` field (e.g. ``"codex"``, ``"rescue"``) falling back to the ``kind``.
    Agents without any token data are silently skipped (tolerant per the plan).

    Returns a dict keyed by model/label, e.g.::

        {
            "codex": {"input_tokens": 1200, "output_tokens": 800, "total_tokens": 2000},
            "rescue": {"input_tokens": 400, "output_tokens": 300, "total_tokens": 700},
        }

    An empty dict is returned when no agents carry usage data.
    """
    totals: dict[str, dict] = {}
    for agent in agents:
        tok = agent.get("tokens")
        if not tok or not isinstance(tok, dict):
            continue
        model_key = str(agent.get("label") or agent.get("kind") or "unknown")
        bucket = totals.setdefault(model_key, {})
        for k in ("input_tokens", "output_tokens", "total_tokens", "tokens"):
            v = tok.get(k)
            if isinstance(v, (int, float)):
                bucket[k] = bucket.get(k, 0) + v
    return totals


# ── git/gh panel: cached, timed-out, off the hot path ────────────────────────────────────────────
_git_cache = {"ts": 0.0, "data": None}
_git_lock = threading.Lock()


def _run(cmd, cwd, timeout):
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as exc:
        return -1, "", str(exc)


def git_panel(repo: Path) -> dict:
    with _git_lock:
        if _git_cache["data"] and (time.time() - _git_cache["ts"]) < _GIT_TTL_S:
            return _git_cache["data"]
        data = {"branch": None, "dirty": None, "ahead": None, "prs": None, "gh_ok": True}
        rc, out, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo, _GIT_TIMEOUT_S)
        if rc == 0:
            data["branch"] = out
        rc, out, _ = _run(["git", "status", "--porcelain"], repo, _GIT_TIMEOUT_S)
        if rc == 0:
            data["dirty"] = len([l for l in out.splitlines() if l.strip()])
        rc, out, _ = _run(["git", "rev-list", "--count", "@{u}..HEAD"], repo, _GIT_TIMEOUT_S)
        if rc == 0 and out.isdigit():
            data["ahead"] = int(out)
        rc, out, err = _run(["gh", "pr", "list", "--json", "number,title,headRefName", "--limit", "10"],
                            repo, _GH_TIMEOUT_S)
        if rc == 0:
            try:
                data["prs"] = json.loads(out or "[]")
            except Exception:
                data["prs"] = []
        else:
            data["gh_ok"] = False  # auth/network failure — surface, don't hang
        _git_cache.update(ts=time.time(), data=data)
        return data


# ── HTTP handler ─────────────────────────────────────────────────────────────────────────────────
PATHS: Paths
REGISTRY = Registry()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(STATIC_ROOT), **k)

    def log_message(self, *a):
        pass

    # --- security (mirrors dashboard.py: localhost always; token only needed off-box) ---
    def _client_ip(self) -> str:
        return self.client_address[0] or ""

    def _is_local(self) -> bool:
        return self._client_ip() in _LOCAL_CLIENTS

    def _control_allowed(self) -> bool:
        if self._is_local():
            return True
        if not CONTROL_TOKEN:
            return False
        tok = self.headers.get("X-Agent-Dashboard-Token", "")
        auth = self.headers.get("Authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
        return tok == CONTROL_TOKEN or bearer == CONTROL_TOKEN

    def _require_control(self) -> bool:
        if self._control_allowed():
            return True
        self._json(403, {"error": "controls are local-only unless AGENT_DASHBOARD_TOKEN is set",
                         "code": "forbidden"})
        return False

    def _json(self, status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n <= 0:
            return {}
        if n > _MAX_BODY:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/agents":
            agents = build_agents(PATHS, REGISTRY)
            self._json(200, {"agents": agents,
                             "totals": _session_token_totals(agents),
                             "scope": PATHS.as_dict(), "generated_at": time.time()})
        elif path == "/api/git":
            self._json(200, git_panel(PATHS.repo))
        elif path == "/api/transcript":
            self._transcript()
        elif path == "/api/control-status":
            self._json(200, {"ok": True, "control_allowed": self._control_allowed(),
                             "token_required": bool(CONTROL_TOKEN), "client": self._client_ip(),
                             "paths": PATHS.as_dict(),
                             "endpoints": ["/api/agents", "/api/git", "/api/transcript", "/api/cancel"]})
        elif path in ("/", ""):
            self.path = "/index.html"
            super().do_GET()
        else:
            super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/cancel":
            self._cancel()
        else:
            self._json(404, {"error": "not found", "code": "not_found"})

    # --- /api/transcript?id=  (registry-resolved; never a client path) ---
    def _transcript(self):
        agent_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
        entry = REGISTRY.get(agent_id)
        if not entry:
            self._json(404, {"error": "unknown agent id", "code": "unknown_id"})
            return
        src = entry.get("log_file")
        lines = _tail_lines(src) if src else []
        self._json(200, {"id": agent_id, "kind": entry.get("kind"), "source": "log",
                         "lines": lines})

    # --- /api/cancel {id}  (structured result; Codex RANK 4) ---
    def _cancel(self):
        if not self._require_control():
            return
        try:
            agent_id = (self._body().get("id") or "").strip()
        except ValueError as exc:
            self._json(400, {"error": str(exc), "code": "bad_body"})
            return
        entry = REGISTRY.get(agent_id)
        if not entry:
            self._json(404, {"error": "unknown agent id", "code": "unknown_id"})
            return
        if entry["kind"] == "codex":
            self._json(200, _cancel_codex(entry["job_id"]))
        elif entry["kind"] == "task" and entry.get("pid") and _pid_alive(entry["pid"]):
            ok = _kill_pid(entry["pid"])
            self._json(200, {"canceled": ok, "method": "pid_kill", "verified": not _pid_alive(entry["pid"]),
                             "reason": "" if ok else "kill failed"})
        else:
            self._json(409, {"canceled": False, "method": None, "verified": False,
                             "reason": "no actionable cancel for this agent kind", "code": "not_actionable"})


def _cancel_codex(job_id: str) -> dict:
    if not COMPANION.exists():
        return {"canceled": False, "method": "companion", "verified": False,
                "reason": "codex companion not found", "code": "no_companion"}
    env = dict(os.environ, MSYS_NO_PATHCONV="1")  # else the job-id arg gets path-mangled under Git Bash
    try:
        r = subprocess.run(["node", str(COMPANION), "cancel", job_id, "--cwd", str(PATHS.repo), "--json"],
                           capture_output=True, text=True, timeout=30, env=env)
        out = {}
        try:
            out = json.loads(r.stdout or "{}")
        except Exception:
            pass
        ok = r.returncode == 0 and str(out.get("status", "")).lower().startswith("cancel")
        return {"canceled": ok, "method": "companion", "verified": ok,
                "job_id": job_id, "status": out.get("status"),
                "reason": "" if ok else (r.stderr or "cancel failed")[:300]}
    except Exception as exc:
        return {"canceled": False, "method": "companion", "verified": False, "reason": str(exc)[:300]}


def _kill_pid(pid) -> bool:
    try:
        if sys.platform == "win32":
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
            return r.returncode == 0
        os.kill(int(pid), 15)
        return True
    except Exception:
        return False


def main():
    global PATHS
    ap = argparse.ArgumentParser(description="Agent Mission Control — Claude Code agent dashboard")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1 — DO NOT expose)")
    ap.add_argument("--session-id", default=os.getenv("AGENT_DASHBOARD_SESSION_ID"),
                    help="Claude session UUID to watch (recommended; auto-detect is advisory)")
    ap.add_argument("--repo", default=str(ROOT), help="repo root (default: this file's dir)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    PATHS = Paths(Path(args.repo), args.session_id)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {args.host} exposes control endpoints off-box. Set AGENT_DASHBOARD_TOKEN.")
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.daemon_threads = True
    except OSError as exc:
        print(f"Cannot bind {args.host}:{args.port}: {exc}\nTry: python agent_dashboard.py --port {args.port + 1}")
        sys.exit(1)

    url = f"http://{args.host}:{args.port}/index.html"
    print(f"Agent Mission Control: {url}")
    print(f"  repo            : {PATHS.repo}")
    print(f"  session         : {PATHS.session_id} ({'auto' if PATHS.session_autodetected else 'pinned'})")
    print(f"  codex state dir : {PATHS.codex_state_dir}")
    print(f"  tasks dir       : {PATHS.tasks_dir}")
    print("Press Ctrl+C to stop.\n")
    if not args.no_browser and STATIC_ROOT.exists():
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
