#!/usr/bin/env python3
"""J-Claw Mission Control — local dashboard server.

Usage:
    python dashboard.py            # opens http://localhost:8765/dashboard/index.html
    python dashboard.py --port 8766
"""
import http.server
import webbrowser
import threading
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass  # suppress per-request noise


def main() -> None:
    parser = argparse.ArgumentParser(description="J-Claw Mission Control dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    try:
        server = http.server.HTTPServer(("127.0.0.1", args.port), _Handler)
    except OSError as exc:
        print(f"Cannot bind port {args.port}: {exc}")
        print(f"Try: python dashboard.py --port {args.port + 1}")
        sys.exit(1)

    url = f"http://localhost:{args.port}/dashboard/index.html"
    print(f"J-Claw Mission Control: {url}")
    print("Press Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
