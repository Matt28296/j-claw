from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harness"))

import dashboard  # noqa: E402
import state_writer as state_writer_mod  # noqa: E402


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class StateWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "mission_control.json"
        self.old_state_file = state_writer_mod._STATE_FILE
        state_writer_mod._STATE_FILE = self.state_path

    def tearDown(self) -> None:
        state_writer_mod._STATE_FILE = self.old_state_file
        self.tmp.cleanup()

    def writer(self) -> state_writer_mod.StateWriter:
        writer = state_writer_mod.StateWriter()
        writer.on_project_start("build a thing", str(Path(self.tmp.name) / "out"))
        return writer

    def assert_terminal(self, state: str, expected: str) -> None:
        self.assertEqual(state["pipeline_state"], expected)
        self.assertIsNone(state["active_agent"])
        self.assertGreater(state["updated_at_epoch"], 0)
        self.assertGreaterEqual(state["sequence"], 2)
        self.assertEqual(state["terminal"]["state"], expected)

    def test_success_and_needs_followup_are_terminal(self) -> None:
        writer = self.writer()
        writer.on_agent_call("orchestrator", "model", "PROJECT_REVIEW")
        writer.on_project_done("pass", "all good")
        self.assert_terminal(read_json(self.state_path), "DONE")

        writer = self.writer()
        writer.on_project_done("needs_followup", "issues remain")
        self.assert_terminal(read_json(self.state_path), "NEEDS_FOLLOWUP")

    def test_failure_cancel_and_no_continuation_are_terminal(self) -> None:
        writer = self.writer()
        writer.on_project_failed("boom", "execution")
        self.assert_terminal(read_json(self.state_path), "FAILED")

        writer = self.writer()
        writer.on_project_canceled("stopped")
        self.assert_terminal(read_json(self.state_path), "CANCELED")

        writer = self.writer()
        writer.on_no_continuation_tasks("no tasks")
        state = read_json(self.state_path)
        self.assert_terminal(state, "FAILED")
        self.assertIn("Continuation failed", state["terminal"]["message"])

    def test_deploy_cost_review_and_dynamic_checks_are_recorded(self) -> None:
        writer = self.writer()
        writer.on_final_review_result(False, "review failed", heal_cycle=1)
        writer.on_dynamic_checks(False, ["missing output"])
        writer.on_deploy("https://example.test", "deployed")
        writer.on_cost({"total_usd": 1.25, "paid_calls": 2, "tokens": {"input": 4}})
        writer.on_project_done("needs_followup", "done")
        state = read_json(self.state_path)
        self.assertEqual(state["final_review"]["heal_cycle"], 1)
        self.assertFalse(state["dynamic_checks"]["passed"])
        self.assertEqual(state["project"]["deploy_url"], "https://example.test")
        self.assertEqual(state["cost"]["paid_calls"], 2)

    def test_atomic_write_does_not_leave_temp_file(self) -> None:
        writer = self.writer()
        for idx in range(5):
            writer.on_cost({"total_usd": idx})
        leftovers = list(Path(self.tmp.name).glob(".mission_control.json.*.tmp"))
        self.assertEqual(leftovers, [])


class DashboardServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "mission_control.json"
        self.old_mission_control = dashboard.MISSION_CONTROL
        self.old_spawn_main = dashboard._spawn_main
        self.old_kill_pipeline = dashboard._kill_pipeline
        dashboard.MISSION_CONTROL = self.state_path
        dashboard._spawn_main = lambda args: SimpleNamespace(pid=12345)
        dashboard._kill_pipeline = lambda: [4321]
        self.write_state()
        self.server = dashboard._ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        dashboard.MISSION_CONTROL = self.old_mission_control
        dashboard._spawn_main = self.old_spawn_main
        dashboard._kill_pipeline = self.old_kill_pipeline
        self.tmp.cleanup()

    def write_state(self, **overrides) -> None:
        state = {
            "pipeline_state": "EXECUTING",
            "project": {
                "intent": "build dashboard",
                "output_dir": str(Path(self.tmp.name) / "out"),
            },
            "tasks": [
                {"id": "task-1", "status": "failed", "objective": "fix", "error_log": "bad"}
            ],
            "active_agent": {"agent": "worker", "started_epoch": time.time()},
            "events": [],
            "sequence": 1,
            "updated_at_epoch": time.time(),
        }
        state.update(overrides)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def request(self, path: str, method: str = "GET", body: dict | None = None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=3) as response:
            payload = response.read()
            ctype = response.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return response.status, json.loads(payload.decode("utf-8"))
            return response.status, payload

    def test_static_state_and_control_status_endpoints(self) -> None:
        status, _ = self.request("/dashboard/index.html")
        self.assertEqual(status, 200)
        status, state = self.request("/mission_control.json")
        self.assertEqual(status, 200)
        self.assertEqual(state["pipeline_state"], "EXECUTING")
        status, data = self.request("/api/control-status")
        self.assertEqual(status, 200)
        self.assertTrue(data["control_allowed"])

    def test_control_post_endpoints(self) -> None:
        for endpoint in [
            "/api/restart",
            "/api/continue",
            "/api/retry_failed_task",
        ]:
            status, data = self.request(endpoint, method="POST", body={})
            self.assertEqual(status, 200, endpoint)
            self.assertTrue(data["ok"], endpoint)

        status, data = self.request("/api/cancel", method="POST", body={})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        state = read_json(self.state_path)
        self.assertEqual(state["pipeline_state"], "CANCELED")
        self.assertIsNone(state["active_agent"])

    def test_bad_control_requests_return_400(self) -> None:
        self.write_state(project={"intent": "x"}, tasks=[])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/continue", method="POST", body={})
        self.assertEqual(ctx.exception.code, 400)

    def test_held_open_client_does_not_block_second_request(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self.server.server_address[1]), timeout=2)
        try:
            status, data = self.request("/api/control-status")
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()
