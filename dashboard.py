#!/usr/bin/env python3
"""J-Claw Mission Control — local dashboard server.

Usage:
    python dashboard.py            # http://0.0.0.0:8765/dashboard/index.html
    python dashboard.py --port 8766 --host 127.0.0.1
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
import argparse
import socket
from urllib.parse import urlparse
from pathlib import Path

ROOT = Path(__file__).parent
HARNESS = ROOT / "harness"
MISSION_CONTROL = ROOT / "mission_control.json"
CONTROL_TOKEN = os.getenv("DASHBOARD_CONTROL_TOKEN", "")

_LOCAL_CLIENTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_MAX_CONTROL_BODY_BYTES = 16 * 1024
_REQUEST_TIMEOUT_S = 10
_STATE_WRITE_LOCK = threading.Lock()


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

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/restart":
            self._handle_restart()
        elif path == "/api/cancel":
            self._handle_cancel()
        elif path == "/api/continue":
            self._handle_continue()
        elif path == "/api/retry_failed_task":
            self._handle_retry_failed_task()
        else:
            self.send_error(404, "Not found")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/control-status":
            self._json_response(200, {
                "ok": True,
                "control_allowed": self._control_allowed(),
                "token_required": bool(CONTROL_TOKEN),
                "local_request": self._is_local_request(),
                "client": self._client_ip(),
                "endpoints": [
                    "/api/restart",
                    "/api/cancel",
                    "/api/continue",
                    "/api/retry_failed_task",
                ],
            })
        else:
            super().do_GET()

    def _handle_restart(self):
        if not self._require_control():
            return
        try:
            state = self._read_state()
            intent = self._last_intent(state)
            if not intent:
                self._json_response(400, {"error": "No previous build found in mission_control.json"})
                return

            slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in intent[:50]).strip("_-")
            output_dir = HARNESS / "projects" / slug

            # Kill any running pipeline (python main.py process)
            killed_pids = _kill_pipeline()

            # Spawn new pipeline
            proc = _spawn_main(["--yes", intent, "--output", str(output_dir)])
            _append_audit_event(
                "restart",
                f"Restart requested for: {intent[:120]}",
                pid=proc.pid,
                client=self._client_ip(),
                killed_pids=killed_pids,
            )

            self._json_response(200, {
                "ok": True,
                "intent": intent,
                "pid": proc.pid,
                "killed_pids": killed_pids,
            })
        except Exception as exc:
            self._json_response(500, {"error": str(exc)})

    def _handle_cancel(self):
        if not self._require_control():
            return
        killed_pids = _kill_pipeline()
        _append_audit_event(
            "cancel",
            "Cancel requested from Mission Control",
            client=self._client_ip(),
            killed_pids=killed_pids,
        )
        _mark_control_terminal(
            "CANCELED",
            "Pipeline canceled from Mission Control",
            client=self._client_ip(),
            killed_pids=killed_pids,
        )
        self._json_response(200, {
            "ok": True,
            "killed": bool(killed_pids),
            "killed_pids": killed_pids,
        })

    def _handle_continue(self):
        if not self._require_control():
            return
        try:
            body = self._read_json_body()
            state = self._read_state()
            output_dir = state.get("project", {}).get("output_dir")
            if not output_dir:
                self._json_response(400, {"error": "No output_dir found in mission_control.json"})
                return
            intent = (body.get("intent") or "").strip()
            if not intent:
                intent = "Continue the current project by addressing remaining review, verification, and completeness issues."
            proc = _spawn_main(["--yes", intent, "--continue", str(output_dir)])
            _append_audit_event(
                "continue",
                f"Continuation requested: {intent[:120]}",
                pid=proc.pid,
                client=self._client_ip(),
            )
            self._json_response(200, {"ok": True, "intent": intent, "pid": proc.pid})
        except ValueError as exc:
            self._json_response(400, {"error": str(exc)})
        except Exception as exc:
            self._json_response(500, {"error": str(exc)})

    def _handle_retry_failed_task(self):
        if not self._require_control():
            return
        try:
            body = self._read_json_body()
            task_id = (body.get("task_id") or "").strip()
            state = self._read_state()
            output_dir = state.get("project", {}).get("output_dir")
            tasks = state.get("tasks", [])
            failed = [t for t in tasks if t.get("status") == "failed"]
            task = next((t for t in failed if t.get("id") == task_id), None) if task_id else (failed[0] if failed else None)
            if not output_dir:
                self._json_response(400, {"error": "No output_dir found in mission_control.json"})
                return
            if not task:
                self._json_response(400, {"error": "No failed task found to refine"})
                return
            error_log = str(task.get("error_log") or "")
            intent = (
                f"Fix failed task {task.get('id')}: {task.get('objective', '')}. "
                f"Use the recorded error as the primary issue: {error_log[:500]}"
            )
            proc = _spawn_main(["--yes", intent, "--continue", str(output_dir)])
            _append_audit_event(
                "retry_failed_task",
                f"Failed-task refinement requested for {task.get('id')}",
                pid=proc.pid,
                client=self._client_ip(),
            )
            self._json_response(200, {"ok": True, "task_id": task.get("id"), "pid": proc.pid})
        except ValueError as exc:
            self._json_response(400, {"error": str(exc)})
        except Exception as exc:
            self._json_response(500, {"error": str(exc)})

    def _read_state(self) -> dict:
        try:
            return json.loads(MISSION_CONTROL.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _last_intent(self, data: dict) -> str | None:
        return data.get("project", {}).get("intent") or data.get("project", {}).get("goal")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > _MAX_CONTROL_BODY_BYTES:
            raise ValueError("Control request body is too large")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Control request body must be valid JSON") from exc

    def _is_local_request(self) -> bool:
        return self._client_ip() in _LOCAL_CLIENTS

    def _client_ip(self) -> str:
        return self.client_address[0] or ""

    def _control_allowed(self) -> bool:
        if self._is_local_request():
            return True
        if not CONTROL_TOKEN:
            return False
        token = self.headers.get("X-Mission-Control-Token", "")
        auth = self.headers.get("Authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
        return token == CONTROL_TOKEN or bearer == CONTROL_TOKEN

    def _require_control(self) -> bool:
        if self._control_allowed():
            return True
        self._json_response(403, {
            "error": "Dashboard controls are local-only unless DASHBOARD_CONTROL_TOKEN is configured and supplied.",
            "control_allowed": False,
            "token_required": bool(CONTROL_TOKEN),
        })
        return False

    def _json_response(self, status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _spawn_main(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(HARNESS / "main.py"), *args],
        cwd=str(HARNESS),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )


def _kill_pipeline() -> list[int]:
    """Kill running harness main.py processes and return the PIDs that were targeted."""
    pids = _find_pipeline_pids()
    killed: list[int] = []
    for pid in pids:
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                )
            else:
                result = subprocess.run(
                    ["kill", "-TERM", str(pid)],
                    capture_output=True,
                    text=True,
                )
            if result.returncode == 0:
                killed.append(pid)
        except Exception:
            continue
    return killed


def _find_pipeline_pids() -> list[int]:
    try:
        if sys.platform == "win32":
            harness_hint = str(HARNESS).replace("'", "''")
            main_hint = str(HARNESS / "main.py").replace("'", "''")
            script = (
                "$harness = '" + harness_hint + "'; "
                "$main = '" + main_hint + "'; "
                "Get-CimInstance Win32_Process | "
                "Where-Object { "
                "$_.ProcessId -ne $PID -and $_.CommandLine -and "
                "($_.CommandLine -like '*main.py*') -and "
                "($_.CommandLine -like ('*' + $harness + '*') -or $_.CommandLine -like ('*' + $main + '*')) "
                "} | Select-Object -ExpandProperty ProcessId"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return []
            return _parse_pids(result.stdout)

        pattern = str(HARNESS / "main.py")
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        if result.returncode != 0:
            return []
        return [pid for pid in _parse_pids(result.stdout) if pid != os.getpid()]
    except Exception:
        return []


def _parse_pids(raw: str) -> list[int]:
    pids: list[int] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def _append_audit_event(
    action: str,
    message: str,
    pid: int | None = None,
    client: str | None = None,
    killed_pids: list[int] | None = None,
) -> None:
    try:
        with _STATE_WRITE_LOCK:
            state = json.loads(MISSION_CONTROL.read_text(encoding="utf-8")) if MISSION_CONTROL.exists() else {}
            events = state.setdefault("events", [])
            entry = {"ts": time.strftime("%H:%M:%S"), "msg": f"[control:{action}] {message}"}
            if pid is not None:
                entry["pid"] = pid
            if client:
                entry["client"] = client
            if killed_pids:
                entry["killed_pids"] = killed_pids
            events.insert(0, entry)
            state["events"] = events[:100]
            state["updated_at_epoch"] = time.time()
            state["sequence"] = int(state.get("sequence", 0)) + 1
            _write_state_atomic(state)
    except Exception:
        pass


def _mark_control_terminal(
    state_name: str,
    message: str,
    client: str | None = None,
    killed_pids: list[int] | None = None,
) -> None:
    try:
        with _STATE_WRITE_LOCK:
            state = json.loads(MISSION_CONTROL.read_text(encoding="utf-8")) if MISSION_CONTROL.exists() else {}
            state["pipeline_state"] = state_name
            state["active_agent"] = None
            events = state.setdefault("events", [])
            entry = {"ts": time.strftime("%H:%M:%S"), "msg": f"[control:{state_name.lower()}] {message}"}
            if client:
                entry["client"] = client
            if killed_pids:
                entry["killed_pids"] = killed_pids
            events.insert(0, entry)
            state["events"] = events[:100]
            state["updated_at_epoch"] = time.time()
            state["sequence"] = int(state.get("sequence", 0)) + 1
            _write_state_atomic(state)
    except Exception:
        pass


def _write_state_atomic(state: dict) -> None:
    tmp = MISSION_CONTROL.with_name(MISSION_CONTROL.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(MISSION_CONTROL)


def main() -> None:
    parser = argparse.ArgumentParser(description="J-Claw Mission Control dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0 for LAN access)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    try:
        server = _ThreadingHTTPServer((args.host, args.port), _Handler)
        server.timeout = _REQUEST_TIMEOUT_S
    except OSError as exc:
        print(f"Cannot bind {args.host}:{args.port}: {exc}")
        print(f"Try: python dashboard.py --port {args.port + 1}")
        sys.exit(1)

    local_url = f"http://localhost:{args.port}/dashboard/index.html"
    print(f"J-Claw Mission Control: {local_url}")
    if args.host == "0.0.0.0":
        try:
            lan_ip = socket.gethostbyname(socket.gethostname())
            print(f"Mobile access:          http://{lan_ip}:{args.port}/dashboard/index.html")
        except Exception:
            pass
    print("Press Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(local_url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
