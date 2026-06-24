"""Phase 1B serve/train lifecycle agent for the 3060 Ti sidecar node (runs ON the 3060 Ti).

Pure stdlib — no harness imports, no third-party deps required.

Subcommands:
  running        — probe Ollama, write RUNNING state + fresh serving lease
  offline        — DRAINING → bounded drain wait → OFFLINE
  force-offline  — write OFFLINE immediately (no drain)
  train          — hardened: DRAINING → drain → stop Ollama → TRAINING
                   → TRAINING_COMMAND → EXPORTING → RETURNING → probe → RUNNING
                   (TRAINING_FAILED on any step failure; explicit `running` clears it)
  heartbeat      — renew serving_allowed_until + updated_at while RUNNING (~every 8 s via loop/task)
  status         — print state file and Ollama health

Configuration (env vars or .env in this script's directory):
  TRAINER_NODE          node_id to write into state (default: nvidia_3060ti)
  NODE_STATE_DIR        directory for <node_id>.json (default: <script_dir>/node_state)
  OLLAMA_HOST           local Ollama base URL (default: http://localhost:11434)
  NODE_SERVING_LEASE_S  seconds each heartbeat extends the serving lease (default: 300)
  NODE_DRAIN_TIMEOUT_S  max seconds to wait for in-flight drain before stopping Ollama (default: 30)
  NODE_MAX_INFLIGHT     max_inflight cap advertised to the 9070-XT router (default: 1)
  NODE_MODEL_HINT       model_hint written into state, informational (default: qwen3:8b)
  TRAINING_COMMAND      shell command to run during the train step
                        (default: python train_worker.py --config sample_config.json)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
STATE_SCHEMA_VERSION = 1


# ── env loading ───────────────────────────────────────────────────────────────

def _load_env_file(path: Path) -> None:
    """Simple .env loader (KEY=VALUE, # comments, no shell expansion)."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k and k not in os.environ:
                v = v.strip().strip('"').strip("'")
                os.environ[k] = v
    except OSError:
        pass


def _cfg(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


def _cfg_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# ── state file ────────────────────────────────────────────────────────────────

def _state_path(node_id: str, state_dir: Path) -> Path:
    return state_dir / f"{node_id}.json"


def _read_state(node_id: str, state_dir: Path) -> dict:
    p = _state_path(node_id, state_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(node_id: str, state_dir: Path, **fields) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    current = _read_state(node_id, state_dir)
    now = time.time()
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "node_id": node_id,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "mode": current.get("mode", "OFFLINE"),
        "serving_allowed": False,
        "serving_allowed_until": 0.0,
        "max_inflight": _cfg_int("NODE_MAX_INFLIGHT", 1),
        "model_hint": _cfg("NODE_MODEL_HINT", "qwen3:8b"),
        "updated_at": now,
        "mode_epoch": current.get("mode_epoch", now),
    }
    state.update(fields)
    if state.get("mode") != current.get("mode"):
        state["mode_epoch"] = now
    p = _state_path(node_id, state_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


# ── ollama probes ─────────────────────────────────────────────────────────────

def _probe_tags(host: str, timeout: float = 3.0) -> bool:
    try:
        req = urllib.request.Request(host.rstrip("/") + "/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def _probe_generate(host: str, model: str, retries: int = 12, pause: float = 10.0) -> tuple[bool, str]:
    """Probe Ollama with a trivial generate call (triggers model load). Retries for model-load time."""
    body = json.dumps({
        "model": model, "prompt": "0", "stream": False,
        "options": {"num_predict": 1, "temperature": 0},
    }).encode("utf-8")
    req = urllib.request.Request(
        host.rstrip("/") + "/api/generate", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                if data.get("done") or data.get("response") is not None:
                    return True, ""
        except Exception as exc:
            if attempt < retries - 1:
                print(f"  probe attempt {attempt + 1}/{retries}: {exc!s:.80} — retrying in {pause:.0f}s")
                time.sleep(pause)
                continue
            return False, str(exc)
    return False, "probe exhausted retries"


# ── ollama service management ─────────────────────────────────────────────────

def _stop_ollama() -> None:
    print("  stopping Ollama service ...")
    if platform.system() == "Windows":
        r = subprocess.run(["sc", "stop", "Ollama"], capture_output=True, timeout=20)
        if r.returncode != 0:
            subprocess.run(["taskkill", "/IM", "ollama.exe", "/F"], capture_output=True, timeout=10)
    else:
        subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True, timeout=10)
    time.sleep(2)


def _start_ollama() -> None:
    print("  starting Ollama service ...")
    if platform.system() == "Windows":
        r = subprocess.run(["sc", "start", "Ollama"], capture_output=True, timeout=20)
        if r.returncode != 0:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            )
    else:
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)


# ── subcommand implementations ────────────────────────────────────────────────

def cmd_running(node_id: str, state_dir: Path, ollama_host: str, lease_s: float) -> int:
    print(f"[running] probing Ollama at {ollama_host} ...")
    if not _probe_tags(ollama_host):
        print("ERROR: Ollama /api/tags not responding — cannot set RUNNING. Start Ollama first.")
        return 1
    _write_state(node_id, state_dir,
                 mode="RUNNING",
                 serving_allowed=True,
                 serving_allowed_until=time.time() + lease_s)
    print(f"[running] RUNNING — serving lease {lease_s:.0f}s. Run `heartbeat` every ~8 s to renew.")
    return 0


def cmd_offline(node_id: str, state_dir: Path, drain_timeout_s: float) -> int:
    print(f"[offline] DRAINING — writing serving_allowed=False (drain up to {drain_timeout_s:.0f}s)")
    _write_state(node_id, state_dir, mode="DRAINING", serving_allowed=False, serving_allowed_until=0.0)
    _drain_wait(node_id, state_dir, drain_timeout_s)
    _write_state(node_id, state_dir, mode="OFFLINE", serving_allowed=False, serving_allowed_until=0.0)
    print("[offline] OFFLINE.")
    return 0


def cmd_force_offline(node_id: str, state_dir: Path) -> int:
    _write_state(node_id, state_dir, mode="OFFLINE", serving_allowed=False, serving_allowed_until=0.0)
    print("[force-offline] OFFLINE (immediate).")
    return 0


def cmd_heartbeat(node_id: str, state_dir: Path, lease_s: float) -> int:
    st = _read_state(node_id, state_dir)
    mode = st.get("mode", "OFFLINE")
    if mode != "RUNNING":
        print(f"[heartbeat] mode={mode} — skipping lease renewal (only renews RUNNING).")
        return 0
    _write_state(node_id, state_dir,
                 mode="RUNNING",
                 serving_allowed=True,
                 serving_allowed_until=time.time() + lease_s)
    return 0


def cmd_status(node_id: str, state_dir: Path, ollama_host: str) -> int:
    st = _read_state(node_id, state_dir)
    if st:
        age = time.time() - float(st.get("updated_at") or 0)
        print(f"State file: {_state_path(node_id, state_dir)}")
        print(json.dumps(st, indent=2))
        print(f"  (state age: {age:.1f}s)")
    else:
        print(f"State file missing or unreadable: {_state_path(node_id, state_dir)}")
    healthy = _probe_tags(ollama_host)
    print(f"Ollama {ollama_host}: {'healthy' if healthy else 'NOT RESPONDING'}")
    return 0


def cmd_train(node_id: str, state_dir: Path, ollama_host: str,
              drain_timeout_s: float, lease_s: float, training_cmd: str) -> int:
    st = _read_state(node_id, state_dir)
    current_mode = st.get("mode", "OFFLINE")
    if current_mode == "TRAINING":
        print("ERROR: already in TRAINING mode. Wait for it to finish or run force-offline first.")
        return 1

    model_hint = _cfg("NODE_MODEL_HINT", "qwen3:8b")
    print(f"[train] DRAINING (drain timeout={drain_timeout_s:.0f}s) ...")
    _write_state(node_id, state_dir, mode="DRAINING", serving_allowed=False, serving_allowed_until=0.0)
    _drain_wait(node_id, state_dir, drain_timeout_s)

    print("[train] stopping Ollama ...")
    _stop_ollama()

    print("[train] TRAINING ...")
    _write_state(node_id, state_dir, mode="TRAINING", serving_allowed=False, serving_allowed_until=0.0)
    t0 = time.monotonic()
    try:
        rc = subprocess.run(training_cmd, shell=True).returncode
    except Exception as exc:
        rc = -1
        print(f"[train] TRAINING_COMMAND raised: {exc}")
    elapsed = time.monotonic() - t0
    print(f"[train] TRAINING_COMMAND exited rc={rc} after {elapsed:.0f}s")

    if rc != 0:
        _write_state(node_id, state_dir,
                     mode="TRAINING_FAILED",
                     serving_allowed=False, serving_allowed_until=0.0)
        print("[train] TRAINING_FAILED — node stays out of routing. Run `running` to clear manually.")
        return 1

    print("[train] EXPORTING (adapter sync in progress) ...")
    _write_state(node_id, state_dir, mode="EXPORTING", serving_allowed=False, serving_allowed_until=0.0)
    time.sleep(2)

    print("[train] RETURNING — starting Ollama ...")
    _write_state(node_id, state_dir, mode="RETURNING", serving_allowed=False, serving_allowed_until=0.0)
    _start_ollama()

    print(f"[train] probing Ollama generation (model={model_hint}) ...")
    ok, err = _probe_generate(ollama_host, model_hint)
    if not ok:
        _write_state(node_id, state_dir, mode="TRAINING_FAILED", serving_allowed=False, serving_allowed_until=0.0)
        print(f"[train] TRAINING_FAILED — Ollama generation probe failed: {err}")
        print("        Fix Ollama, then run `node_agent.py running` to re-enter routing.")
        return 1

    _write_state(node_id, state_dir,
                 mode="RUNNING",
                 serving_allowed=True,
                 serving_allowed_until=time.time() + lease_s,
                 model_hint=model_hint)
    print("[train] RUNNING — adapter trained and Ollama serving. Run `heartbeat` every ~8 s.")
    return 0


def _drain_wait(node_id: str, state_dir: Path, timeout_s: float) -> None:
    """Wait up to timeout_s for in-flight requests to drain, refreshing updated_at every 2 s
    so the 9070-XT router can see the DRAINING state (not stale=OFFLINE) during the wait."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        remaining = deadline - time.time()
        print(f"  drain wait {remaining:.0f}s remaining ...")
        time.sleep(min(2.0, remaining))
        _write_state(node_id, state_dir, mode="DRAINING", serving_allowed=False, serving_allowed_until=0.0)
    print("  drain wait complete.")


# ── main ──────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="3060 Ti sidecar node lifecycle agent (Phase 1B).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("running", help="probe Ollama, write RUNNING + lease")
    sub.add_parser("offline", help="DRAINING → drain → OFFLINE")
    sub.add_parser("force-offline", help="write OFFLINE immediately")
    sub.add_parser("train", help="hardened DRAINING → TRAINING → RUNNING sequence")
    sub.add_parser("heartbeat", help="renew serving lease (run every ~8 s)")
    sub.add_parser("status", help="print state file and Ollama health")
    return ap


def main(argv: list[str] | None = None) -> int:
    _load_env_file(_SCRIPT_DIR / ".env")

    node_id = _cfg("TRAINER_NODE", "nvidia_3060ti")
    state_dir = Path(_cfg("NODE_STATE_DIR", str(_SCRIPT_DIR / "node_state")))
    ollama_host = _cfg("OLLAMA_HOST", "http://localhost:11434")
    lease_s = _cfg_float("NODE_SERVING_LEASE_S", 300.0)
    drain_s = _cfg_float("NODE_DRAIN_TIMEOUT_S", 30.0)
    training_cmd = _cfg(
        "TRAINING_COMMAND",
        "python train_worker.py --config sample_config.json",
    )

    args = _build_parser().parse_args(argv)

    if args.command == "running":
        return cmd_running(node_id, state_dir, ollama_host, lease_s)
    if args.command == "offline":
        return cmd_offline(node_id, state_dir, drain_s)
    if args.command == "force-offline":
        return cmd_force_offline(node_id, state_dir)
    if args.command == "heartbeat":
        return cmd_heartbeat(node_id, state_dir, lease_s)
    if args.command == "status":
        return cmd_status(node_id, state_dir, ollama_host)
    if args.command == "train":
        return cmd_train(node_id, state_dir, ollama_host, drain_s, lease_s, training_cmd)

    print(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
