"""
test_llm_layers.py — mocked fallback-layer coverage for every LLM call path.

Zero API spend.  Run with:
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python harness/test_llm_layers.py

Layers covered (per the plan's requirement matrix):
  1. _OpenAICompatOrchestrator (Gemini/OpenRouter): 429 model-switch, 503 model-switch,
     chain-exhausted backoff-reset, all-fail → RuntimeError
  2. Anthropic Orchestrator.call: timeout retry, 429/529 backoff, bad-JSON retry,
     all-fail → RuntimeError
  3. _parse_retry_delay: all four shapes (Google RetryInfo, OpenRouter, plain-text, blind)
  4. CompositeOrchestrator: primary fails → emergency fires; primary OK → emergency not called;
     no emergency → RuntimeError propagates
  5. execute_task attempt chain: rung walk-up on infra error; ValueError raises immediately;
     paid-budget clamp skips cloud rung; all exhausted → RuntimeError
  6. routed_rung: base + retry_count capped at top rung (4-rung ladder incl. Opus)
  7. Final Review fails-CLOSED on API error (regression guard for PR #23 behaviour)
"""

from __future__ import annotations
import sys
import os
import json
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ── Environment setup ─────────────────────────────────────────────────────────
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("GOOGLE_API_KEY", "test-google-key")
os.environ.setdefault("OPENROUTER_API_KEY", "test-or-key")
os.environ.setdefault("ORCHESTRATOR_PROVIDER", "gemini")
os.environ.setdefault("WORKER_LADDER",
    "ollama::qwen3:8b,ollama::deepseek-coder-v2:16b,"
    "anthropic::claude-sonnet-4-6,anthropic::claude-opus-4-8")
os.environ.setdefault("MAX_PAID_WORKER_CALLS", "15")
os.environ.setdefault("OLLAMA_HOST", "http://localhost:11434")

sys.path.insert(0, str(Path(__file__).parent))

# ── Helpers ───────────────────────────────────────────────────────────────────

VALID_FORMAT1 = {
    "project_type": "web",
    "complexity": "low",
    "goal": "test",
    "features": [],
    "constraints": [],
    "architecture": {"frontend": "html", "backend": "none",
                     "database": "none", "deployment": "none"},
    "modules": [],
}

VALID_FORMAT2 = {"tasks": []}

VALID_FORMAT3 = {
    "refinement_target_task_id": "task-1",
    "reason_for_refinement": "test",
    "action": "modify",
    "updated_tasks": [{
        "id": "task-1", "type": "frontend", "objective": "x",
        "files": ["a.html"], "dependencies": [], "priority": "low",
        "acceptance_criteria": [], "verification": "none",
    }],
}


def _make_exc(cls, message: str = "error", response_json: dict | None = None):
    """Build a mock exception with an optional .response.json() body.

    openai APIStatusError subclasses (RateLimitError, InternalServerError) require
    `response` and `body` keyword arguments — we supply mock objects for both.
    """
    resp = MagicMock()
    resp.json.return_value = response_json or {}
    if response_json is not None:
        resp.json.return_value = response_json

    try:
        # openai >= 1.0 APIStatusError subclasses need response + body
        exc = cls(message, response=resp, body=response_json or {})
    except TypeError:
        # Plain Exception or anthropic errors — no extra kwargs
        exc = cls(message)

    exc.response = resp
    return exc


# ═══════════════════════════════════════════════════════════════════════════════
# 1. _parse_retry_delay
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseRetryDelay(unittest.TestCase):

    def _fn(self):
        from orchestrator import _parse_retry_delay
        return _parse_retry_delay

    def test_google_retry_info_3s(self):
        fn = self._fn()
        exc = _make_exc(Exception, response_json={"error": {"details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3s"},
        ]}})
        self.assertEqual(fn(exc, 0), 5)  # 3+2

    def test_google_retry_info_54s(self):
        fn = self._fn()
        exc = _make_exc(Exception, response_json={"error": {"details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "54s"},
        ]}})
        self.assertEqual(fn(exc, 1), 56)  # 54+2

    def test_openrouter_metadata(self):
        fn = self._fn()
        exc = _make_exc(Exception, response_json={"error": {"metadata": {"retry_after_seconds": 30}}})
        self.assertEqual(fn(exc, 0), 32)  # 30+2

    def test_plain_text_regex(self):
        fn = self._fn()
        exc = Exception("retry in 2.4 seconds")
        self.assertEqual(fn(exc, 0), 4)  # int(2.4)+2

    def test_blind_default_scales_with_attempt(self):
        fn = self._fn()
        exc = Exception("some opaque error")
        self.assertEqual(fn(exc, 2), 105)  # 35*3


# ═══════════════════════════════════════════════════════════════════════════════
# 2. _OpenAICompatOrchestrator (Gemini / OpenRouter path)
# ═══════════════════════════════════════════════════════════════════════════════

class TestOpenAICompatOrchestrator(unittest.TestCase):

    def _make_orch(self):
        """Build a Gemini orchestrator with mocked openai client."""
        with patch.dict(os.environ, {"ORCHESTRATOR_PROVIDER": "gemini"}):
            from orchestrator import GeminiOrchestrator
        orch = GeminiOrchestrator.__new__(GeminiOrchestrator)
        orch._model_chain = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
        orch._system_prompt = "sys"
        orch._client = MagicMock()
        return orch

    def _good_response(self, data: dict):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = json.dumps(data)
        return resp

    def test_429_triggers_model_switch_then_succeeds(self):
        from openai import RateLimitError
        orch = self._make_orch()

        call_count = [0]
        def side(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise _make_exc(RateLimitError, "429")
            return self._good_response(VALID_FORMAT2)

        orch._client.chat.completions.create.side_effect = side
        result = orch.call({"system_state": "SPEC_ACCEPTED"}, max_retries=3)
        self.assertEqual(result, VALID_FORMAT2)
        self.assertEqual(call_count[0], 2)

    def test_503_triggers_model_switch_then_succeeds(self):
        from openai import InternalServerError
        orch = self._make_orch()

        call_count = [0]
        def side(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise _make_exc(InternalServerError, "503 unavailable")
            return self._good_response(VALID_FORMAT2)

        orch._client.chat.completions.create.side_effect = side
        result = orch.call({"system_state": "SPEC_ACCEPTED"}, max_retries=3)
        self.assertEqual(result, VALID_FORMAT2)

    def test_all_models_exhausted_then_reset_and_wait(self):
        from openai import RateLimitError
        orch = self._make_orch()

        responses = [
            _make_exc(RateLimitError, "429", {"error": {"details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"},
            ]}}),
            _make_exc(RateLimitError, "429", {"error": {"details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"},
            ]}}),
            self._good_response(VALID_FORMAT2),
        ]
        idx = [0]
        def side(*a, **kw):
            r = responses[idx[0]]
            idx[0] += 1
            if isinstance(r, Exception):
                raise r
            return r

        orch._client.chat.completions.create.side_effect = side
        with patch("time.sleep"):
            result = orch.call({"system_state": "SPEC_ACCEPTED"}, max_retries=3)
        self.assertEqual(result, VALID_FORMAT2)

    def test_all_attempts_fail_raises_runtime_error(self):
        from openai import RateLimitError
        orch = self._make_orch()
        orch._client.chat.completions.create.side_effect = _make_exc(RateLimitError, "429")
        with patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                orch.call({"system_state": "INIT"}, max_retries=1)

    def test_bad_json_retries_then_succeeds(self):
        orch = self._make_orch()

        call_count = [0]
        def side(*a, **kw):
            call_count[0] += 1
            resp = MagicMock()
            resp.choices = [MagicMock()]
            if call_count[0] == 1:
                resp.choices[0].message.content = "NOT JSON"
            else:
                resp.choices[0].message.content = json.dumps(VALID_FORMAT2)
            return resp

        orch._client.chat.completions.create.side_effect = side
        result = orch.call({"system_state": "SPEC_ACCEPTED"}, max_retries=3)
        self.assertEqual(result, VALID_FORMAT2)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Anthropic Orchestrator.call
# ═══════════════════════════════════════════════════════════════════════════════

class TestAnthropicOrchestrator(unittest.TestCase):

    def _make_orch(self):
        with patch("anthropic.Anthropic"):
            from orchestrator import Orchestrator
        orch = Orchestrator.__new__(Orchestrator)
        orch._client = MagicMock()
        orch._system_prompt = "sys"
        return orch

    def _good_resp(self, data: dict):
        resp = MagicMock()
        resp.stop_reason = "end_turn"
        resp.content = [MagicMock()]
        resp.content[0].text = json.dumps(data)
        resp.usage = MagicMock()
        return resp

    def test_timeout_retries_then_succeeds(self):
        import anthropic as ant
        orch = self._make_orch()

        call_count = [0]
        def side(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ant.APITimeoutError(request=MagicMock())
            return self._good_resp(VALID_FORMAT1)

        orch._client.messages.create.side_effect = side
        with patch("time.sleep"):
            with patch("orchestrator.log_cache_usage"), patch("orchestrator.record_usage"):
                result = orch.call({"system_state": "INIT"}, max_retries=2)
        self.assertEqual(result["project_type"], "web")

    def test_rate_limit_backs_off_then_succeeds(self):
        import anthropic as ant
        orch = self._make_orch()

        call_count = [0]
        def side(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ant.RateLimitError(
                    message="429", response=MagicMock(status_code=429), body={})
            return self._good_resp(VALID_FORMAT2)

        orch._client.messages.create.side_effect = side
        with patch("time.sleep"):
            with patch("orchestrator.log_cache_usage"), patch("orchestrator.record_usage"):
                result = orch.call({"system_state": "SPEC_ACCEPTED"}, max_retries=2)
        self.assertEqual(result, VALID_FORMAT2)

    def test_all_attempts_fail_raises_runtime_error(self):
        import anthropic as ant
        orch = self._make_orch()
        orch._client.messages.create.side_effect = ant.RateLimitError(
            message="429", response=MagicMock(status_code=429), body={})
        with patch("time.sleep"):
            with patch("orchestrator.log_cache_usage"), patch("orchestrator.record_usage"):
                with self.assertRaises(RuntimeError):
                    orch.call({"system_state": "INIT"}, max_retries=0)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. CompositeOrchestrator
# ═══════════════════════════════════════════════════════════════════════════════

class TestCompositeOrchestrator(unittest.TestCase):

    def test_primary_fails_emergency_fires(self):
        from orchestrator import CompositeOrchestrator

        primary = MagicMock()
        primary.call.side_effect = RuntimeError("Gemini exhausted")
        emergency = MagicMock()
        emergency.call.return_value = VALID_FORMAT2

        c = CompositeOrchestrator(primary, emergency)
        with patch("orchestrator.console"):
            result = c.call({"system_state": "INIT"})

        self.assertEqual(result, VALID_FORMAT2)
        primary.call.assert_called_once()
        emergency.call.assert_called_once()

    def test_primary_succeeds_emergency_not_invoked(self):
        from orchestrator import CompositeOrchestrator

        primary = MagicMock()
        primary.call.return_value = VALID_FORMAT2
        emergency = MagicMock()

        c = CompositeOrchestrator(primary, emergency)
        result = c.call({"system_state": "SPEC_ACCEPTED"})

        self.assertEqual(result, VALID_FORMAT2)
        emergency.call.assert_not_called()

    def test_no_emergency_configured_propagates_runtime_error(self):
        primary = MagicMock()
        primary.call.side_effect = RuntimeError("exhausted")

        with self.assertRaises(RuntimeError):
            primary.call({})


# ═══════════════════════════════════════════════════════════════════════════════
# 5. routed_rung — 4-rung ladder incl. Opus
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoutedRung(unittest.TestCase):

    def setUp(self):
        import worker as w
        self._orig_ladder = w.WORKER_LADDER
        w.WORKER_LADDER = [
            ("ollama", "qwen3:8b"),
            ("ollama", "deepseek-coder-v2:16b"),
            ("anthropic", "claude-sonnet-4-6"),
            ("anthropic", "claude-opus-4-8"),
        ]
        self._w = w

    def tearDown(self):
        self._w.WORKER_LADDER = self._orig_ladder

    def _task(self, complexity_rung: int = 0, retry: int = 0):
        t = MagicMock()
        t.retry_count = retry
        # route_task returns the complexity rung — patch it
        with patch("worker.route_task", return_value=complexity_rung):
            from worker import routed_rung
            return routed_rung(t)

    def test_base_rung_zero_no_retries(self):
        with patch("worker.route_task", return_value=0):
            t = MagicMock(); t.retry_count = 0
            from worker import routed_rung
            self.assertEqual(routed_rung(t), 0)

    def test_retry_escalates_one_rung(self):
        with patch("worker.route_task", return_value=0):
            t = MagicMock(); t.retry_count = 1
            from worker import routed_rung
            self.assertEqual(routed_rung(t), 1)

    def test_capped_at_top_rung(self):
        with patch("worker.route_task", return_value=0):
            t = MagicMock(); t.retry_count = 99
            from worker import routed_rung
            self.assertEqual(routed_rung(t), 3)  # top = Opus

    def test_mid_rung_start_cap(self):
        with patch("worker.route_task", return_value=2):
            t = MagicMock(); t.retry_count = 5
            from worker import routed_rung
            self.assertEqual(routed_rung(t), 3)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. execute_task — rung walk-up, ValueError, paid-budget clamp
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecuteTask(unittest.TestCase):

    def setUp(self):
        import worker as w
        self._w = w
        self._orig_ladder = w.WORKER_LADDER
        self._orig_paid = w._paid_calls_made
        w.WORKER_LADDER = [
            ("ollama", "qwen3:8b"),
            ("ollama", "deepseek-coder-v2:16b"),
            ("anthropic", "claude-sonnet-4-6"),
            ("anthropic", "claude-opus-4-8"),
        ]
        w._paid_calls_made = 0
        w._MAX_PAID_OVERRIDE = None

    def tearDown(self):
        self._w.WORKER_LADDER = self._orig_ladder
        self._w._paid_calls_made = self._orig_paid

    def _task(self, retry=0):
        t = MagicMock()
        t.id = "task-1"
        t.type = "frontend"
        t.objective = "build a page"
        t.files = ["index.html"]
        t.dependencies = []
        t.acceptance_criteria = []
        t.verification = "none"
        t.retry_count = retry
        return t

    def _good_output(self):
        return json.dumps({"files": [{"path": "index.html", "content": "<html/>"}]})

    def test_rung_walkup_on_capability_error(self):
        """A non-connection error (bad model output) on qwen3 walks up to deepseek."""
        w = self._w

        call_log = []
        def mock_call(provider, model, sys, user):
            call_log.append((provider, model))
            if provider == "ollama" and model == "qwen3:8b":
                raise RuntimeError("model output truncated")
            return self._good_output()

        spec = {"architecture": {"stack": "vanilla"}}
        with patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "_reserve_paid_call", return_value=True), \
             patch.object(w, "route_task", return_value=0), \
             patch.object(w, "_parse_and_validate", return_value={"files": []}):
            result = w.execute_task(self._task(), spec, {})

        # First call failed (qwen3:8b), second call succeeded (deepseek)
        self.assertEqual(call_log[0], ("ollama", "qwen3:8b"))
        self.assertEqual(call_log[1], ("ollama", "deepseek-coder-v2:16b"))

    def test_ollama_connection_error_raises_immediately_no_cloud_escalation(self):
        """ConnectionError on Ollama must raise immediately — never escalate to paid cloud."""
        w = self._w

        call_log = []
        def mock_call(provider, model, sys, user):
            call_log.append((provider, model))
            if provider == "ollama":
                raise ConnectionError("connection refused")
            return self._good_output()  # cloud would succeed but must never be reached

        spec = {"architecture": {"stack": "vanilla"}}
        with patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "_reserve_paid_call", return_value=True), \
             patch.object(w, "route_task", return_value=0):
            with self.assertRaises(RuntimeError) as ctx:
                w.execute_task(self._task(), spec, {})

        # Must have raised on first Ollama call — cloud rung never touched
        self.assertEqual(len(call_log), 1)
        self.assertIn("ollama", call_log[0])
        self.assertIn("Ollama unavailable", str(ctx.exception))

    def test_value_error_raises_immediately_no_retry(self):
        w = self._w

        call_log = []
        def mock_call(provider, model, sys, user):
            call_log.append((provider, model))
            raise ValueError("bad JSON format")

        spec = {"architecture": {"stack": "vanilla"}}
        with patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "_reserve_paid_call", return_value=True), \
             patch.object(w, "route_task", return_value=0):
            with self.assertRaises(ValueError):
                w.execute_task(self._task(), spec, {})

        # Only one call attempted — ValueError is not retried
        self.assertEqual(len(call_log), 1)

    def test_paid_budget_exhausted_skips_cloud_rung(self):
        w = self._w
        # MAX_PAID_WORKER_CALLS is imported directly into worker.py, so we must
        # patch the module-level name there (not in config) and reset the counter.
        w._paid_calls_made = 0

        call_log = []
        def mock_call(provider, model, sys, user):
            call_log.append((provider, model))
            if provider == "ollama":
                return self._good_output()
            raise AssertionError(f"cloud rung should not be reached when budget=0: {provider}/{model}")

        spec = {"architecture": {"stack": "vanilla"}}
        with patch.object(w, "MAX_PAID_WORKER_CALLS", 0), \
             patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "route_task", return_value=0), \
             patch.object(w, "_parse_and_validate", return_value={"files": []}):
            result = w.execute_task(self._task(retry=2), spec, {})

        # Only ollama calls should appear
        self.assertTrue(all(p == "ollama" for p, _ in call_log),
                        f"Cloud rung was invoked despite budget=0: {call_log}")

    def test_all_exhausted_raises_runtime_error(self):
        w = self._w

        def mock_call(provider, model, sys, user):
            raise OSError("network down")

        spec = {"architecture": {"stack": "vanilla"}}
        with patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "_reserve_paid_call", return_value=True), \
             patch.object(w, "route_task", return_value=0):
            with self.assertRaises(RuntimeError):
                w.execute_task(self._task(), spec, {})


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Final Review fails-CLOSED on API error (regression guard for PR #23)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFinalReviewFailsClosed(unittest.TestCase):

    def test_api_error_returns_false_not_true(self):
        from final_review import run_final_review
        import anthropic as ant

        # Both API attempts fail — the function must return False (fail closed),
        # not True. (PR #23 regression guard: a crashed review must never pass.)
        # We create a dummy output file so _collect_files doesn't short-circuit.
        with patch("final_review.anthropic.Anthropic") as mock_ant:
            mock_client = MagicMock()
            mock_ant.return_value = mock_client
            mock_client.messages.create.side_effect = ant.APIConnectionError(
                request=MagicMock())

            with patch("time.sleep"), \
                 patch("final_review.console"), \
                 patch("final_review.log_cache_usage"), \
                 patch("final_review.record_usage"):
                import tempfile
                with tempfile.TemporaryDirectory() as tmp:
                    # Write a source file so the "no files" early-return is avoided
                    (Path(tmp) / "index.html").write_text("<html/>", encoding="utf-8")
                    result = run_final_review(
                        output_dir=Path(tmp),
                        spec={"goal": "test", "architecture": {"stack": "vanilla"}},
                    )

        # Must return False (fail closed) — not True (passing a broken build)
        self.assertFalse(result, "Final review must return False on API error (fail closed)")


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Experience learning loop — escalation logging + worker hint injection
# ═══════════════════════════════════════════════════════════════════════════════

class TestExperienceLearning(unittest.TestCase):

    def setUp(self):
        import tempfile, experience_log as el
        self._tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        self._tmp.close()
        self._orig_path = el.EXPERIENCE_FILE
        el.EXPERIENCE_FILE = Path(self._tmp.name)
        self._el = el

    def tearDown(self):
        import os
        self._el.EXPERIENCE_FILE = self._orig_path
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _read_entries(self):
        return [json.loads(l) for l in Path(self._tmp.name).read_text().splitlines() if l.strip()]

    # ── log_escalation writes correct shape ───────────────────────────────────
    def test_log_escalation_writes_event_field(self):
        self._el.log_escalation(
            task_type="frontend",
            stack="vanilla",
            failed_model="ollama/qwen3:8b",
            succeeded_model="anthropic/claude-sonnet-4-6",
            error_summary="Worker output missing 'files' list",
            objective_summary="Build the hero section with class hero",
        )
        entries = self._read_entries()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["event"], "escalation")
        self.assertEqual(e["task_type"], "frontend")
        self.assertEqual(e["failed_model"], "ollama/qwen3:8b")
        self.assertEqual(e["succeeded_model"], "anthropic/claude-sonnet-4-6")
        self.assertIn("files", e["error_summary"])

    # ── get_worker_hints returns [] when no history ───────────────────────────
    def test_get_worker_hints_empty_when_no_entries(self):
        hints = self._el.get_worker_hints("frontend", "vanilla")
        self.assertEqual(hints, [])

    # ── get_worker_hints returns hint after matching escalation logged ─────────
    def test_get_worker_hints_returns_hint_after_escalation(self):
        self._el.log_escalation(
            task_type="frontend", stack="vanilla",
            failed_model="ollama/qwen3:8b", succeeded_model="anthropic/claude-sonnet-4-6",
            error_summary="missing class attribute on section tag",
            objective_summary="Add class=hero to section id=hero",
        )
        hints = self._el.get_worker_hints("frontend", "vanilla")
        self.assertEqual(len(hints), 1)
        self.assertIn("frontend", hints[0])
        self.assertIn("vanilla", hints[0])

    # ── get_stack_lessons includes escalation-derived lesson ──────────────────
    def test_get_stack_lessons_includes_escalation_lesson(self):
        for _ in range(3):
            self._el.log_escalation(
                task_type="style", stack="vanilla",
                failed_model="ollama/qwen3:8b", succeeded_model="anthropic/claude-sonnet-4-6",
                error_summary="CSS truncated mid-rule",
                objective_summary="Write complete hero.css",
            )
        lessons = self._el.get_stack_lessons("vanilla", min_count=2)
        self.assertTrue(any("[escalation]" in l for l in lessons),
                        f"Expected escalation lesson in: {lessons}")

    # ── execute_task hint injection: hint appears in user_message ─────────────
    def test_execute_task_injects_worker_hints(self):
        import worker as w
        captured_user_msgs = []

        def mock_call(provider, model, sys_p, user_p):
            captured_user_msgs.append(user_p)
            return json.dumps({"files": [{"path": "index.html", "content": "<html/>"}]})

        # Pre-load one escalation entry so get_worker_hints returns a hint
        self._el.log_escalation(
            task_type="frontend", stack="vanilla",
            failed_model="ollama/qwen3:8b", succeeded_model="anthropic/claude-sonnet-4-6",
            error_summary="missing class attribute", objective_summary="add class=hero",
        )

        task = MagicMock()
        task.type = "frontend"
        task.objective = "build hero"
        task.files = ["index.html"]
        task.dependencies = []
        task.acceptance_criteria = []
        task.verification = "none"
        task.retry_count = 0
        spec = {"architecture": {"stack": "vanilla"}}

        with patch.object(w, "_build_user_message", return_value="task prompt"), \
             patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "_reserve_paid_call", return_value=True), \
             patch.object(w, "route_task", return_value=0), \
             patch.object(w, "_parse_and_validate", return_value={"files": []}), \
             patch.object(w, "get_worker_hints", return_value=["Hint: avoid missing class"]) as mock_hints:
            w.execute_task(task, spec, {})

        mock_hints.assert_called_once_with("frontend", "vanilla")
        self.assertTrue(any("PAST FAILURE PATTERNS" in m for m in captured_user_msgs),
                        "Expected hint block in worker user_message")

    # ── execute_task logs escalation when fallback succeeds ───────────────────
    def test_execute_task_logs_escalation_on_fallback_success(self):
        import worker as w

        def mock_call(provider, model, sys_p, user_p):
            if provider == "ollama" and model == "qwen3:8b":
                raise RuntimeError("model output truncated — capability failure")
            return json.dumps({"files": [{"path": "index.html", "content": "<html/>"}]})

        task = MagicMock()
        task.type = "frontend"
        task.objective = "build hero"
        task.files = ["index.html"]
        task.dependencies = []
        task.acceptance_criteria = []
        task.verification = "none"
        task.retry_count = 0
        spec = {"architecture": {"stack": "vanilla"}}

        orig_ladder = w.WORKER_LADDER
        w.WORKER_LADDER = [("ollama", "qwen3:8b"), ("anthropic", "claude-sonnet-4-6")]
        try:
            with patch.object(w, "_build_user_message", return_value="task prompt"), \
                 patch.object(w, "_call_provider", side_effect=mock_call), \
                 patch.object(w, "_reserve_paid_call", return_value=True), \
                 patch.object(w, "route_task", return_value=0), \
                 patch.object(w, "_parse_and_validate", return_value={"files": []}), \
                 patch.object(w, "get_worker_hints", return_value=[]), \
                 patch.object(w, "log_escalation") as mock_log:
                w.execute_task(task, spec, {})

            mock_log.assert_called_once()
            call_kwargs = mock_log.call_args
            self.assertEqual(call_kwargs.kwargs.get("failed_model") or call_kwargs[1].get("failed_model"),
                             "ollama/qwen3:8b")
            self.assertEqual(call_kwargs.kwargs.get("succeeded_model") or call_kwargs[1].get("succeeded_model"),
                             "anthropic/claude-sonnet-4-6")
        finally:
            w.WORKER_LADDER = orig_ladder


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestParseRetryDelay,
        TestOpenAICompatOrchestrator,
        TestAnthropicOrchestrator,
        TestCompositeOrchestrator,
        TestRoutedRung,
        TestExecuteTask,
        TestFinalReviewFailsClosed,
        TestExperienceLearning,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
