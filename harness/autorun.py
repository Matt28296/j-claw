#!/usr/bin/env python3
"""
J-Claw auto-restart loop.
Runs the pipeline continuously, restarting on stall or failure.
Usage:  python autorun.py "Your project intent"
        python autorun.py  (uses DEFAULT_INTENT below)
Stop:   Ctrl+C
"""
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_INTENT = "A web app for selling construction equipment"
RESTART_DELAY_S = 8   # seconds to wait between runs
MAX_RUNS = 20          # safety cap — set to 0 for unlimited


def main() -> None:
    intent = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_INTENT
    harness_dir = Path(__file__).parent

    print(f"\n{'='*60}")
    print(f"J-Claw auto-restart loop")
    print(f"Intent: {intent}")
    print(f"Ctrl+C to stop")
    print('='*60)

    run_count = 0
    while MAX_RUNS == 0 or run_count < MAX_RUNS:
        run_count += 1
        print(f"\n[Run #{run_count}] Starting pipeline…\n")

        try:
            result = subprocess.run(
                [sys.executable, "main.py", intent, "--yes"],
                cwd=harness_dir,
            )
            exit_code = result.returncode
        except KeyboardInterrupt:
            print("\n\nStopped by user.")
            sys.exit(0)

        print(f"\n[Run #{run_count}] Pipeline exited (code {exit_code}).")

        if MAX_RUNS > 0 and run_count >= MAX_RUNS:
            print(f"Reached max runs ({MAX_RUNS}). Stopping.")
            break

        print(f"Restarting in {RESTART_DELAY_S}s… (Ctrl+C to stop)\n")
        try:
            time.sleep(RESTART_DELAY_S)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            sys.exit(0)


if __name__ == "__main__":
    main()
