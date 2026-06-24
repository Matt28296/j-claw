"""Local-LLM node registry (Phase 1A) — routes Ollama worker calls across a two-machine pool.

The 9070 XT box is the PRIMARY always-on Ollama worker; the 3060 Ti box is an OPTIONAL sidecar that
serves only while it advertises a valid RUNNING lease (and is removed from routing while training).

Design notes / invariants:
  * SINGLE routing process. j-claw runs one scheduler/ThreadPoolExecutor, so the per-process inflight
    counters here ARE the authoritative capacity signal — no shared/SQLite lock needed. Do not run two
    routers against one sidecar (documented in config.py).
  * The PRIMARY node always uses config.OLLAMA_HOST, so single-machine setups behave exactly as before.
    LOCAL_LLM_NODES supplies the non-primary (sidecar) endpoint(s).
  * The primary is NEVER capacity-gated — it is the fail-closed fallback and must always accept work
    (this preserves the pre-existing throughput where MAX_PARALLEL_WORKERS Ollama calls ran un-capped).
    max_inflight gates ONLY the sidecar.
  * This module's universe is LOCAL Ollama nodes only — it never returns a cloud provider. The caller
    (worker._call_ollama) handles the no-paid-escalation invariant when every local node fails.

A node state file (NODE_STATE_DIR/<node_id>.json, written by node_agent.py in Phase 1B) carries:
  schema_version, node_id, hostname, pid, mode, serving_allowed, serving_allowed_until,
  max_inflight, model_hint, updated_at, mode_epoch
Missing primary -> RUNNING (it is the always-on box); missing non-primary / malformed / stale -> OFFLINE.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import config

SERVABLE_MODE = "RUNNING"
_HEALTH_TTL_S = 5.0  # cache /api/tags results briefly so routing never blocks on the network per-call

_LOCK = threading.Lock()
_INFLIGHT: dict[str, int] = {}
_HEALTH: dict[str, tuple[bool, float]] = {}  # node_id -> (ok, checked_at)


def _now() -> float:
    return time.time()


def _parse_nodes() -> dict[str, str]:
    """node_id -> base_url. The primary is forced to config.OLLAMA_HOST for back-compat."""
    nodes: dict[str, str] = {}
    for part in (config.LOCAL_LLM_NODES or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        nid, url = part.split("=", 1)
        nid, url = nid.strip(), url.strip()
        if nid and url:
            nodes[nid] = url
    nodes[config.PRIMARY_LLM_NODE] = config.OLLAMA_HOST  # primary is always OLLAMA_HOST
    return nodes


def primary_id() -> str:
    return config.PRIMARY_LLM_NODE


def _state_path(node_id: str) -> Path:
    return config.NODE_STATE_DIR / f"{node_id}.json"


def _read_state(node_id: str) -> dict:
    """The node's state dict with defaults applied. Missing primary -> RUNNING; missing non-primary,
    malformed, or stale (older than NODE_STATE_TTL_S) -> OFFLINE."""
    is_primary = node_id == config.PRIMARY_LLM_NODE
    p = _state_path(node_id)
    if not p.exists():
        if is_primary:
            return {"node_id": node_id, "mode": "RUNNING", "serving_allowed": True,
                    "serving_allowed_until": _now() + 3600.0, "updated_at": _now(),
                    "max_inflight": config.NODE_MAX_INFLIGHT_DEFAULT, "default": True}
        return {"node_id": node_id, "mode": "OFFLINE", "serving_allowed": False, "default": True}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state file is not a JSON object")
    except Exception:  # noqa: BLE001 — any unreadable/malformed state is treated as OFFLINE
        return {"node_id": node_id, "mode": "OFFLINE", "serving_allowed": False, "malformed": True}
    updated = 0.0
    try:
        updated = float(data.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated = 0.0
    if _now() - updated > config.NODE_STATE_TTL_S:
        data = dict(data)
        data["mode"] = "OFFLINE"
        data["serving_allowed"] = False
        data["stale"] = True
    return data


def _healthy(node_id: str, url: str) -> bool:
    """Cached GET /api/tags probe (TTL _HEALTH_TTL_S) so the hot path never blocks on the network."""
    now = _now()
    cached = _HEALTH.get(node_id)
    if cached and now - cached[1] < _HEALTH_TTL_S:
        return cached[0]
    ok = False
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=config.NODE_HEALTH_TIMEOUT_S) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 300
    except Exception:  # noqa: BLE001 — unreachable endpoint = unhealthy
        ok = False
    _HEALTH[node_id] = (ok, now)
    return ok


def _sidecar_eligible(node_id: str, url: str, task) -> bool:
    """True only if the sidecar is RUNNING, advertising a valid serving lease, the task type is
    allowed, it has spare capacity, and it is healthy."""
    st = _read_state(node_id)
    if st.get("mode") != SERVABLE_MODE or not st.get("serving_allowed"):
        return False
    until = st.get("serving_allowed_until")
    try:
        if until is not None and float(until) < _now():
            return False
    except (TypeError, ValueError):
        return False
    ttype = getattr(task, "type", None) if task is not None else None
    if ttype not in config.SIDECAR_ALLOWED_TASK_TYPES:
        return False
    cap = _max_inflight(st)
    if _INFLIGHT.get(node_id, 0) >= cap:
        return False
    return _healthy(node_id, url)


def _max_inflight(state: dict) -> int:
    try:
        return max(1, int(state.get("max_inflight") or config.NODE_MAX_INFLIGHT_DEFAULT))
    except (TypeError, ValueError):
        return config.NODE_MAX_INFLIGHT_DEFAULT


def choose_ollama_node(task=None, force_primary: bool = False) -> tuple[str, str]:
    """Reserve and return (node_id, base_url) for an Ollama call. Routes to an eligible sidecar when
    possible (and only for allowed task types); otherwise fails closed to the primary. The caller MUST
    release the node with release_ollama_node() (or use reserved_node()). Never returns a cloud node."""
    nodes = _parse_nodes()
    pid = config.PRIMARY_LLM_NODE
    purl = nodes.get(pid, config.OLLAMA_HOST)
    if not force_primary:
        for nid, url in nodes.items():
            if nid == pid:
                continue
            if _sidecar_eligible(nid, url, task):
                with _LOCK:  # re-check capacity under the lock to avoid a check-then-act race
                    cap = _max_inflight(_read_state(nid))
                    if _INFLIGHT.get(nid, 0) < cap:
                        _INFLIGHT[nid] = _INFLIGHT.get(nid, 0) + 1
                        return nid, url
    with _LOCK:  # primary is never capacity-gated (fail-closed fallback)
        _INFLIGHT[pid] = _INFLIGHT.get(pid, 0) + 1
    return pid, purl


# Alias kept for readability at call sites that "acquire" rather than "choose".
acquire_node = choose_ollama_node


def release_ollama_node(node_id: str) -> None:
    """Release one inflight slot for node_id. Idempotent-safe: never drops below zero."""
    with _LOCK:
        if _INFLIGHT.get(node_id, 0) > 0:
            _INFLIGHT[node_id] -= 1


@contextmanager
def reserved_node(task=None, force_primary: bool = False):
    """Context manager: reserve a node, always release on exit (success, error, or cancellation)."""
    nid, url = choose_ollama_node(task, force_primary=force_primary)
    try:
        yield nid, url
    finally:
        release_ollama_node(nid)


def node_snapshot() -> dict:
    """Telemetry for mission_control.json / the dashboard. node_id -> {mode, endpoint, lease, inflight…}."""
    out: dict[str, dict] = {}
    for nid, url in _parse_nodes().items():
        st = _read_state(nid)
        is_primary = nid == config.PRIMARY_LLM_NODE
        # Only probe health for nodes that claim to be serving (avoid hammering OFFLINE sidecars).
        healthy = _healthy(nid, url) if (is_primary or st.get("mode") == SERVABLE_MODE) else False
        out[nid] = {
            "node_id": nid,
            "endpoint": url,
            "mode": st.get("mode", "UNKNOWN"),
            "is_primary": is_primary,
            "serving_allowed": bool(st.get("serving_allowed")),
            "serving_allowed_until": st.get("serving_allowed_until"),
            "healthy": healthy,
            "inflight": _INFLIGHT.get(nid, 0),
            "max_inflight": _max_inflight(st),
            "model_hint": st.get("model_hint", ""),
            "updated_at": st.get("updated_at"),
            "stale": bool(st.get("stale")),
        }
    return out
