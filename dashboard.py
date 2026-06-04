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
import webbrowser
import argparse
from pathlib import Path

ROOT = Path(__file__).parent
HARNESS = ROOT / "harness"
MISSION_CONTROL = HARNESS / "mission_control.json"


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass  # suppress per-request noise

    def do_POST(self):
        if self.path == "/api/restart":
            self._handle_restart()
        else:
            self.send_error(404, "Not found")

    def _handle_restart(self):
        try:
            intent = self._read_last_intent()
            if not intent:
                self._json_response(400, {"error": "No previous build found in mission_control.json"})
                return

            slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in intent[:50]).strip("_-")
            output_dir = HARNESS / "projects" / slug

            # Kill any running pipeline (python main.py process)
            _kill_pipeline()

            # Spawn new pipeline
            proc = subprocess.Popen(
                [sys.executable, str(HARNESS / "main.py"), "--yes", intent,
                 "--output", str(output_dir)],
                cwd=str(HARNESS),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )

            self._json_response(200, {"ok": True, "intent": intent, "pid": proc.pid})
        except Exception as exc:
            self._json_response(500, {"error": str(exc)})

    def _read_last_intent(self) -> str | None:
        try:
            data = json.loads(MISSION_CONTROL.read_text(encoding="utf-8"))
            return data.get("project", {}).get("intent") or data.get("project", {}).get("goal")
        except Exception:
            return None

    def _json_response(self, status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)


def _kill_pipeline():
    """Kill any python main.py processes that are currently running."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/FI", "IMAGENAME eq python.exe", "/FI",
                 f"WINDOWTITLE eq *main.py*"],
                capture_output=True,
            )
        else:
            subprocess.run(["pkill", "-f", "main.py"], capture_output=True)
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="J-Claw Mission Control dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0 for LAN access)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    try:
        server = http.server.HTTPServer((args.host, args.port), _Handler)
    except OSError as exc:
        print(f"Cannot bind {args.host}:{args.port}: {exc}")
        print(f"Try: python dashboard.py --port {args.port + 1}")
        sys.exit(1)

    local_url = f"http://localhost:{args.port}/dashboard/index.html"
    print(f"J-Claw Mission Control: {local_url}")
    if args.host == "0.0.0.0":
        import socket
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
