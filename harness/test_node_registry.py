"""Phase 1A tests — local-LLM node registry routing + the no-paid-escalation invariant.

Deterministic and network-free: health checks are monkeypatched and Ollama calls are faked, so these
never touch a real Ollama server or any cloud provider. Run from the harness dir:
    python test_node_registry.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import node_registry as nr

PRIMARY = "amd_9070xt"
SIDECAR = "nvidia_3060ti"
PRIMARY_URL = "http://localhost:11434"
SIDECAR_URL = "http://3060ti-box:11434"


class FakeTask:
    def __init__(self, ttype):
        self.type = ttype


def _write_state(d: Path, node_id: str, **kw) -> None:
    obj = {"node_id": node_id, "updated_at": time.time()}
    obj.update(kw)
    (d / f"{node_id}.json").write_text(json.dumps(obj), encoding="utf-8")


def _running_sidecar(**over):
    base = {"mode": "RUNNING", "serving_allowed": True,
            "serving_allowed_until": time.time() + 100, "max_inflight": 1}
    base.update(over)
    return base


def _set_config(tmp: Path) -> dict:
    saved = {k: getattr(config, k) for k in (
        "NODE_STATE_DIR", "LOCAL_LLM_NODES", "PRIMARY_LLM_NODE", "TRAINER_NODE", "OLLAMA_HOST",
        "SIDECAR_ALLOWED_TASK_TYPES", "NODE_STATE_TTL_S", "NODE_MAX_INFLIGHT_DEFAULT")}
    config.NODE_STATE_DIR = tmp
    config.PRIMARY_LLM_NODE = PRIMARY
    config.TRAINER_NODE = SIDECAR
    config.OLLAMA_HOST = PRIMARY_URL
    config.LOCAL_LLM_NODES = f"{PRIMARY}={PRIMARY_URL},{SIDECAR}={SIDECAR_URL}"
    config.SIDECAR_ALLOWED_TASK_TYPES = {"documentation"}
    config.NODE_STATE_TTL_S = 10.0
    config.NODE_MAX_INFLIGHT_DEFAULT = 1
    return saved


class NodeRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = _set_config(self.tmp)
        nr._INFLIGHT.clear()
        nr._HEALTH.clear()
        self._real_healthy = nr._healthy
        nr._healthy = lambda nid, url: True  # assume reachable unless a test says otherwise

    def tearDown(self):
        nr._healthy = self._real_healthy
        for k, v in self._saved.items():
            setattr(config, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_primary_defaults_running(self):
        snap = nr.node_snapshot()
        self.assertEqual(snap[PRIMARY]["mode"], "RUNNING")
        nid, url = nr.choose_ollama_node(FakeTask("documentation"))
        # no sidecar state file -> sidecar OFFLINE -> primary chosen
        self.assertEqual(nid, PRIMARY)
        self.assertEqual(url, PRIMARY_URL)

    def test_missing_sidecar_is_offline(self):
        self.assertEqual(nr.node_snapshot()[SIDECAR]["mode"], "OFFLINE")

    def test_malformed_sidecar_is_offline(self):
        (self.tmp / f"{SIDECAR}.json").write_text("{not json", encoding="utf-8")
        nid, _ = nr.choose_ollama_node(FakeTask("documentation"))
        self.assertEqual(nid, PRIMARY)

    def test_stale_sidecar_is_offline(self):
        _write_state(self.tmp, SIDECAR, **_running_sidecar())
        # backdate beyond TTL
        p = self.tmp / f"{SIDECAR}.json"
        obj = json.loads(p.read_text())
        obj["updated_at"] = time.time() - 1000
        p.write_text(json.dumps(obj), encoding="utf-8")
        nid, _ = nr.choose_ollama_node(FakeTask("documentation"))
        self.assertEqual(nid, PRIMARY)

    def test_eligible_sidecar_is_chosen(self):
        _write_state(self.tmp, SIDECAR, **_running_sidecar())
        nid, url = nr.choose_ollama_node(FakeTask("documentation"))
        self.assertEqual(nid, SIDECAR)
        self.assertEqual(url, SIDECAR_URL)

    def test_disallowed_task_type_uses_primary(self):
        _write_state(self.tmp, SIDECAR, **_running_sidecar())
        nid, _ = nr.choose_ollama_node(FakeTask("backend"))
        self.assertEqual(nid, PRIMARY)

    def test_expired_lease_uses_primary(self):
        _write_state(self.tmp, SIDECAR, **_running_sidecar(serving_allowed_until=time.time() - 1))
        nid, _ = nr.choose_ollama_node(FakeTask("documentation"))
        self.assertEqual(nid, PRIMARY)

    def test_unhealthy_sidecar_uses_primary(self):
        _write_state(self.tmp, SIDECAR, **_running_sidecar())
        nr._healthy = lambda nid, url: nid == PRIMARY  # sidecar unreachable
        nid, _ = nr.choose_ollama_node(FakeTask("documentation"))
        self.assertEqual(nid, PRIMARY)

    def test_capacity_then_release(self):
        _write_state(self.tmp, SIDECAR, **_running_sidecar(max_inflight=1))
        n1, _ = nr.choose_ollama_node(FakeTask("documentation"))
        self.assertEqual(n1, SIDECAR)                       # slot 1 -> sidecar
        n2, _ = nr.choose_ollama_node(FakeTask("documentation"))
        self.assertEqual(n2, PRIMARY)                       # sidecar full -> primary
        nr.release_ollama_node(SIDECAR)
        n3, _ = nr.choose_ollama_node(FakeTask("documentation"))
        self.assertEqual(n3, SIDECAR)                       # slot freed -> sidecar again

    def test_release_is_idempotent(self):
        nr.release_ollama_node(SIDECAR)
        nr.release_ollama_node(SIDECAR)
        self.assertEqual(nr._INFLIGHT.get(SIDECAR, 0), 0)   # never goes negative

    def test_primary_is_never_capacity_gated(self):
        # primary has no cap even at default max_inflight=1: many concurrent reservations all succeed
        for _ in range(5):
            nid, _ = nr.choose_ollama_node(None)
            self.assertEqual(nid, PRIMARY)
        self.assertEqual(nr._INFLIGHT[PRIMARY], 5)


class FakeResp:
    def __init__(self, text):
        self.message = types.SimpleNamespace(content=text)
        self.prompt_eval_count = 1
        self.eval_count = 1


def _fake_ollama(fail_substrings):
    def factory(host=None, timeout=None):
        class _Client:
            def chat(self_inner, **kw):
                if any(s in (host or "") for s in fail_substrings):
                    raise ConnectionError("connection refused")
                return FakeResp('{"ok": true}')
        return _Client()
    return types.SimpleNamespace(Client=factory)


class WorkerInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import worker  # noqa: F401
            cls.worker = worker
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"worker import unavailable: {exc}")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = _set_config(self.tmp)
        nr._INFLIGHT.clear()
        nr._HEALTH.clear()
        self._real_healthy = nr._healthy
        nr._healthy = lambda nid, url: True
        self._real_ollama = self.worker.ollama
        # Track any cloud-provider call; the invariant is that none happen on local infra failure.
        self.cloud_calls = []
        self._cloud_saved = {}
        for name in ("_call_anthropic", "_call_openrouter", "_call_codex", "_call_grok", "_call_claude_cli"):
            self._cloud_saved[name] = getattr(self.worker, name)
            setattr(self.worker, name, (lambda n: (lambda *a, **k: self.cloud_calls.append(n)))(name))

    def tearDown(self):
        self.worker.ollama = self._real_ollama
        for name, fn in self._cloud_saved.items():
            setattr(self.worker, name, fn)
        nr._healthy = self._real_healthy
        for k, v in self._saved.items():
            setattr(config, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_local_down_raises_and_never_calls_cloud(self):
        config.LOCAL_LLM_NODES = f"{PRIMARY}={PRIMARY_URL}"  # primary only
        self.worker.ollama = _fake_ollama([":"])             # every host fails
        with self.assertRaises(Exception) as ctx:
            self.worker._call_provider("ollama", "qwen3:8b", "sys", "user", task=FakeTask("documentation"))
        self.assertTrue(self.worker._is_ollama_unavailable(ctx.exception))  # classified as infra
        self.assertEqual(self.cloud_calls, [])                              # ZERO cloud calls

    def test_sidecar_failure_falls_back_to_primary(self):
        _write_state(self.tmp, SIDECAR, **_running_sidecar())
        self.worker.ollama = _fake_ollama(["3060ti-box"])    # sidecar fails, primary (localhost) ok
        out = self.worker._call_ollama("qwen3:8b", "sys", "user", task=FakeTask("documentation"))
        self.assertEqual(out, '{"ok": true}')                # recovered on primary
        self.assertEqual(self.cloud_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
