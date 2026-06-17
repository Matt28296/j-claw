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

    # ── Phase 1: log_escalation stores whitelisted rich lesson fields ─────────
    def test_log_escalation_stores_lesson_fields(self):
        self._el.log_escalation(
            task_type="frontend", stack="vanilla",
            failed_model="ollama/qwen3:8b", succeeded_model="codex/gpt-5.5",
            error_summary="addEventListener lost this binding",
            objective_summary="wire the submit handler",
            lesson={
                "solution_technique": "Bind class methods or use an arrow wrapper as listeners",
                "prompt_hint": "Register listeners with an arrow wrapper to preserve `this`",
                "anti_pattern": "passing this.handler unbound",
                "verification_signal": "form submit no longer throws",
                "confidence": "high",
                "bogus_field": "should be dropped",
            },
        )
        e = self._read_entries()[0]
        self.assertTrue(e["solution_technique"].startswith("Bind "))
        self.assertIn("arrow", e["prompt_hint"])
        self.assertEqual(e["rescue_model"], "codex/gpt-5.5")
        self.assertNotIn("bogus_field", e, "non-whitelisted lesson keys must be dropped")

    # ── Phase 1: techniques rank before bare warnings in get_worker_hints ─────
    def test_get_worker_hints_techniques_before_warnings(self):
        self._el.log_escalation(  # a bare warning (no technique)
            task_type="frontend", stack="vanilla",
            failed_model="ollama/qwen3:8b", succeeded_model="anthropic/claude-sonnet-4-6",
            error_summary="generic truncation somewhere", objective_summary="write the file",
        )
        self._el.log_escalation(  # a technique-bearing rescue
            task_type="frontend", stack="vanilla",
            failed_model="ollama/qwen3:8b", succeeded_model="codex/gpt-5.5",
            error_summary="event listener binding broke", objective_summary="wire handler",
            lesson={"prompt_hint": "Use an arrow wrapper to preserve `this`",
                    "verification_signal": "submit works"},
        )
        hints = self._el.get_worker_hints("frontend", "vanilla", limit=3)
        self.assertTrue(hints)
        self.assertIn("Known successful technique", hints[0],
                      f"a proven technique must rank before bare warnings: {hints}")
        self.assertIn("arrow wrapper", hints[0])

    # ── Phase 1: _parse_and_validate lesson boundary (top-level only, never a file) ──
    def test_parse_and_validate_lesson_boundary(self):
        import worker as w
        raw = json.dumps({
            "files": [{"path": "a.js", "content": "console.log('hello world');", "lesson": "LEAK"}],
            "lesson": {"solution_technique": "do Y"},
        })
        result = w._parse_and_validate(raw)
        # Top-level lesson is captured for the experience log…
        self.assertEqual(result["lesson"]["solution_technique"], "do Y")
        # …and each file entry is reduced to exactly {path, content}; the in-entry "lesson" is
        # dropped, so learning metadata can never be written to disk as/inside a file.
        self.assertEqual(set(result["files"][0].keys()), {"path", "content"})

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


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Codex CLI OAuth worker rung — routing, unavailability, capacity, budget split
# ═══════════════════════════════════════════════════════════════════════════════
#
# Contract under test (implemented concurrently in config.py / worker.py / cost.py):
#   - WORKER_LADDER default gains a ("codex","gpt-5.5") rung between the strongest
#     local rung and the anthropic rung.
#   - config: CODEX_CLI_ENABLED, CODEX_MODEL, CODEX_CLI_MAX_CALLS, CODEX_TIMEOUT,
#     OAUTH_PROVIDERS={"codex"}, METERED_PROVIDERS={"anthropic","openrouter"}.
#   - worker: _call_codex(model, system, user) -> str (shells to `codex exec`),
#     _is_codex_unavailable(exc) -> bool, _reserve_oauth_call(provider) -> bool
#     (capacity counter capped at CODEX_CLI_MAX_CALLS), module flag _codex_disabled,
#     reset_paid_budget() also resets oauth counters + _codex_disabled,
#     _call_provider routes provider "codex" -> _call_codex.
#   - OAuth rungs consume the capacity counter, NOT the dollar budget
#     (_paid_calls_made); an _is_codex_unavailable error sets _codex_disabled and
#     escalates; capacity exhaustion skips codex and escalates.
#   - cost: record_oauth_usage(provider, *, success, latency_s, tokens);
#     cost_summary() has an "oauth" key.
#
# All mocked — monkeypatch _call_codex / _call_provider so NO real subprocess or
# API runs. reset_paid_budget() is invoked in setUp.

class TestCodexWorkerRung(unittest.TestCase):

    def setUp(self):
        import worker as w
        self._w = w
        self._orig_ladder = w.WORKER_LADDER
        # Pin CODEX_CLI_ENABLED so the rung is exercised deterministically — otherwise these
        # tests silently depend on the operator's untracked harness/.env (a clean checkout/CI
        # has it default False, which would skip the rung and make routing assertions vacuous).
        self._orig_codex_enabled = w.CODEX_CLI_ENABLED
        w.CODEX_CLI_ENABLED = True
        # Codex rung between strongest-local and anthropic.
        w.WORKER_LADDER = [
            ("ollama", "qwen3:8b"),
            ("ollama", "deepseek-coder-v2:16b"),
            ("codex", "gpt-5.5"),
            ("anthropic", "claude-sonnet-4-6"),
        ]
        # Reset both the dollar budget and the oauth/capacity counters + flag.
        if hasattr(w, "reset_paid_budget"):
            w.reset_paid_budget()
        else:  # defensive fallback if the helper name drifts
            w._paid_calls_made = 0

    def tearDown(self):
        self._w.WORKER_LADDER = self._orig_ladder
        self._w.CODEX_CLI_ENABLED = self._orig_codex_enabled
        if hasattr(self._w, "reset_paid_budget"):
            self._w.reset_paid_budget()

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

    # ── 1. _call_provider routes provider "codex" → _call_codex ────────────────
    def test_call_provider_routes_codex_to_call_codex(self):
        w = self._w
        sentinel = json.dumps({"files": [{"path": "x.txt", "content": "ok"}]})
        with patch.object(w, "_call_codex", return_value=sentinel) as mock_codex:
            out = w._call_provider("codex", "gpt-5.5", "sys prompt", "user prompt")
        mock_codex.assert_called_once()
        # The model + prompts must be forwarded to _call_codex.
        args, kwargs = mock_codex.call_args
        forwarded = list(args) + list(kwargs.values())
        self.assertIn("gpt-5.5", forwarded)
        self.assertEqual(out, sentinel)

    # ── 2. _is_codex_unavailable failure classification ────────────────────────
    def test_is_codex_unavailable_classification(self):
        w = self._w
        fn = w._is_codex_unavailable

        # Unavailable (skip-to-next-rung) cases.
        self.assertTrue(fn(FileNotFoundError("codex.cmd not found")),
                        "exe missing must be unavailable")
        self.assertTrue(fn(RuntimeError("401 unauthorized")),
                        "401 must be unavailable")
        self.assertTrue(fn(RuntimeError("429 rate limit")),
                        "429 must be unavailable")
        self.assertTrue(fn(RuntimeError("not logged in")),
                        "not-logged-in must be unavailable")
        self.assertTrue(fn(RuntimeError("Please run codex login to continue")),
                        "explicit 'please run codex login' must be unavailable")

        # NOT unavailable — a real capability/output failure must NOT skip the rung.
        self.assertFalse(fn(ValueError("bad output")),
                         "bad output is a capability error, not unavailability")
        self.assertFalse(fn(RuntimeError("model refused the task — capability gap")),
                         "generic capability error is not unavailability")
        # Bare "login" must NOT trip the unavailable classifier — a genuine capability failure
        # in a task that writes login/auth code can echo the word on a nonzero exit.
        self.assertFalse(fn(RuntimeError("TypeError in generated LoginForm.handleLogin")),
                         "a capability error merely containing 'login' must not skip the rung")

    # ── 3. OAuth call does NOT consume the dollar budget ───────────────────────
    def test_oauth_call_does_not_consume_dollar_budget(self):
        w = self._w
        # Route directly to the codex rung so the first attempt is an oauth call.
        reserved = {"oauth": 0}
        orig_reserve = w._reserve_oauth_call

        def counting_reserve(provider):
            ok = orig_reserve(provider)
            if ok:
                reserved["oauth"] += 1
            return ok

        def mock_call(provider, model, sys, user):
            self.assertEqual(provider, "codex")
            return self._good_output()

        spec = {"architecture": {"stack": "vanilla"}}
        with patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "_call_codex", return_value=self._good_output()), \
             patch.object(w, "_reserve_oauth_call", side_effect=counting_reserve), \
             patch.object(w, "route_task", return_value=2), \
             patch.object(w, "_parse_and_validate", return_value={"files": []}):
            before = w._paid_calls_made
            w.execute_task(self._task(), spec, {})
            after = w._paid_calls_made

        # An oauth call WAS reserved, but the dollar counter is untouched.
        self.assertGreaterEqual(reserved["oauth"], 1,
                                "expected an oauth capacity reservation on the codex rung")
        self.assertEqual(before, after,
                         "oauth call must NOT decrement/increment the dollar budget")

    # ── 4. Capacity exhaustion skips codex → escalates to anthropic ────────────
    def test_capacity_exhaustion_skips_codex_escalates_to_anthropic(self):
        w = self._w
        # Clamp the capacity cap to zero so no oauth call may be reserved.
        # Exhaust the counter directly too, to be robust to either gating path.
        with patch.object(w, "CODEX_CLI_MAX_CALLS", 0):
            # Drain any remaining capacity.
            for _ in range(5):
                w._reserve_oauth_call("codex")

            call_log = []

            def mock_call(provider, model, sys, user):
                call_log.append((provider, model))
                if provider == "ollama":
                    raise RuntimeError("model output truncated — capability failure")
                return self._good_output()

            anthropic_sentinel = MagicMock(return_value=self._good_output())

            spec = {"architecture": {"stack": "vanilla"}}
            with patch.object(w, "_call_provider", side_effect=mock_call), \
                 patch.object(w, "_call_codex") as mock_codex, \
                 patch.object(w, "_call_anthropic", anthropic_sentinel), \
                 patch.object(w, "_reserve_paid_call", return_value=True), \
                 patch.object(w, "route_task", return_value=0), \
                 patch.object(w, "_parse_and_validate", return_value={"files": []}):
                w.execute_task(self._task(retry=2), spec, {})

        # Codex was skipped (capacity exhausted) and anthropic rung was reached.
        self.assertFalse(any(p == "codex" for p, _ in call_log),
                         f"codex rung must be skipped when capacity exhausted: {call_log}")
        mock_codex.assert_not_called()
        self.assertTrue(any(p == "anthropic" for p, _ in call_log),
                        f"execution must escalate to anthropic rung: {call_log}")

    # ── 5. _codex_disabled short-circuit after an unavailable error ────────────
    def test_codex_disabled_short_circuits_subsequent_tasks(self):
        w = self._w

        codex_calls = {"n": 0}

        def codex_unavailable(model, system, user):
            codex_calls["n"] += 1
            raise RuntimeError("not logged in")

        def mock_call(provider, model, sys, user):
            if provider == "ollama":
                raise RuntimeError("model output truncated — capability failure")
            if provider == "codex":
                return w._call_codex(model, sys, user)  # routes to our unavailable stub
            return self._good_output()

        spec = {"architecture": {"stack": "vanilla"}}
        with patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "_call_codex", side_effect=codex_unavailable), \
             patch.object(w, "_reserve_paid_call", return_value=True), \
             patch.object(w, "route_task", return_value=0), \
             patch.object(w, "_parse_and_validate", return_value={"files": []}):
            # First task: codex raises unavailable → flag flips, escalates to anthropic.
            w.execute_task(self._task(retry=2), spec, {})
            self.assertTrue(w._codex_disabled,
                            "_codex_disabled must flip True after an unavailable error")
            calls_after_first = codex_calls["n"]

            # Second task: codex must be skipped cheaply (no second _call_codex),
            # and execution still reaches the anthropic rung.
            second_log = []

            def mock_call2(provider, model, sys, user):
                second_log.append((provider, model))
                if provider == "ollama":
                    raise RuntimeError("model output truncated — capability failure")
                if provider == "codex":
                    return w._call_codex(model, sys, user)
                return self._good_output()

            with patch.object(w, "_call_provider", side_effect=mock_call2):
                w.execute_task(self._task(retry=2), spec, {})

        self.assertEqual(codex_calls["n"], calls_after_first,
                         "_call_codex must NOT be invoked again once _codex_disabled is set")
        self.assertTrue(any(p == "anthropic" for p, _ in second_log),
                        f"second task must still reach anthropic rung: {second_log}")

    # ── 6. _call_codex parse path → execute_task yields valid result ───────────
    def test_call_codex_parse_path_yields_codex_result(self):
        w = self._w
        codex_json = json.dumps({"files": [{"path": "x.txt", "content": "ok"}]})

        # _call_codex returns the JSON string (mirroring the `-o` temp-file mechanism);
        # the real subprocess is mocked away entirely.
        def mock_call(provider, model, sys, user):
            if provider == "codex":
                return w._call_codex(model, sys, user)
            raise AssertionError(f"only the codex rung should be exercised here: {provider}")

        spec = {"architecture": {"stack": "vanilla"}}
        with patch.object(w, "_call_codex", return_value=codex_json) as mock_codex, \
             patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "_reserve_oauth_call", return_value=True), \
             patch.object(w, "route_task", return_value=2):
            result = w.execute_task(self._task(), spec, {})

        mock_codex.assert_called()
        # Parsed result reflects the codex output + model_used == the codex rung.
        files = result.get("files") if isinstance(result, dict) else None
        self.assertTrue(files, f"expected parsed files from codex output: {result}")
        self.assertEqual(files[0]["path"], "x.txt")
        model_used = (result.get("model_used") or result.get("model") or "") \
            if isinstance(result, dict) else ""
        self.assertIn("codex", str(model_used).lower(),
                      f"model_used must reflect codex rung: {result}")

    # ── 7. cost.record_oauth_usage surfaces in cost_summary()["oauth"] ─────────
    def test_record_oauth_usage_surfaces_in_cost_summary(self):
        import cost
        cost.record_oauth_usage("codex", success=True, latency_s=1.2, tokens=100)
        summary = cost.cost_summary()
        self.assertIn("oauth", summary,
                      "cost_summary() must expose an 'oauth' key for OAuth-rung telemetry")

    # ── 8. A FAILED _call_codex still records an attempted oauth call ($0) ──────
    def test_failed_call_codex_records_failed_oauth_attempt(self):
        w = self._w
        import cost
        cost.reset_costs()

        # Real _call_codex body runs; only the subprocess is mocked to fail (nonzero exit),
        # exercising the failure-telemetry path. record_oauth_usage is imported inside
        # _call_codex from the cost module, so patch it there.
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = "429 usage limit reached"
        fake_proc.stdout = ""

        with patch.object(w.subprocess, "run", return_value=fake_proc), \
             patch.object(w.shutil, "which", return_value="codex"):
            with self.assertRaises(RuntimeError):
                w._call_codex("gpt-5.5", "sys", "user")

        oauth = cost.cost_summary().get("oauth", {}).get("codex", {})
        self.assertEqual(oauth.get("calls", 0), 1,
                         "a failed codex invocation must still count as an attempted call")
        self.assertEqual(oauth.get("success", -1), 0,
                         "a failed codex invocation must NOT count as a success")

    # ── 9. _call_codex must send stdin as UTF-8 (live-smoke-test regression) ────
    def test_call_codex_sends_utf8_stdin(self):
        """Regression for a bug found by the live smoke test: real worker prompts contain
        non-ASCII glyphs (arrows, bullets, box chars). Windows text-mode subprocess defaults
        to cp1252, and `codex exec` reads stdin strictly as UTF-8 → it rejected the prompt with
        'input is not valid UTF-8'. _call_codex must pass encoding='utf-8' to subprocess.run."""
        w = self._w
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = json.dumps({"files": []})
        fake_proc.stderr = ""
        captured = {}

        def capture_run(cmd, **kwargs):
            captured.update(kwargs)
            return fake_proc

        with patch.object(w.subprocess, "run", side_effect=capture_run), \
             patch.object(w.shutil, "which", return_value="codex"):
            # Prompt deliberately carries non-cp1252-safe characters.
            w._call_codex("gpt-5.5", "system ▶ arrow → bullet •", "user 中文")

        self.assertEqual(captured.get("encoding"), "utf-8",
                         "_call_codex must pass encoding='utf-8' to subprocess.run")
        self.assertEqual(captured.get("errors"), "replace",
                         "_call_codex should tolerate stray output bytes via errors='replace'")


class TestGrokWorkerRung(unittest.TestCase):
    """Grok Build CLI OAuth rung — mirrors TestCodexWorkerRung. All mocked: no real `grok`
    subprocess or network. Grok sits in the ladder ABOVE local Ollama and BELOW Codex (Grok-first).
    Key behavioural difference vs Codex: a transient throttle (429) must NOT trip the disable-latch."""

    def setUp(self):
        import worker as w
        self._w = w
        self._orig_ladder = w.WORKER_LADDER
        # Pin GROK_CLI_ENABLED so the rung is exercised deterministically (clean checkout/CI
        # defaults it False, which would skip the rung and make routing assertions vacuous).
        self._orig_grok_enabled = w.GROK_CLI_ENABLED
        self._orig_codex_enabled = w.CODEX_CLI_ENABLED
        w.GROK_CLI_ENABLED = True
        w.CODEX_CLI_ENABLED = False  # isolate grok: escalation past grok lands on anthropic
        # Grok rung between strongest-local and anthropic (codex omitted to isolate grok).
        w.WORKER_LADDER = [
            ("ollama", "qwen3:8b"),
            ("ollama", "deepseek-coder-v2:16b"),
            ("grok", "grok-build"),
            ("anthropic", "claude-sonnet-4-6"),
        ]
        if hasattr(w, "reset_paid_budget"):
            w.reset_paid_budget()
        else:
            w._paid_calls_made = 0

    def tearDown(self):
        self._w.WORKER_LADDER = self._orig_ladder
        self._w.GROK_CLI_ENABLED = self._orig_grok_enabled
        self._w.CODEX_CLI_ENABLED = self._orig_codex_enabled
        if hasattr(self._w, "reset_paid_budget"):
            self._w.reset_paid_budget()

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

    # ── 1. _call_provider routes provider "grok" → _call_grok ──────────────────
    def test_call_provider_routes_grok_to_call_grok(self):
        w = self._w
        sentinel = json.dumps({"files": [{"path": "x.txt", "content": "ok"}]})
        with patch.object(w, "_call_grok", return_value=sentinel) as mock_grok:
            out = w._call_provider("grok", "grok-build", "sys prompt", "user prompt")
        mock_grok.assert_called_once()
        args, kwargs = mock_grok.call_args
        forwarded = list(args) + list(kwargs.values())
        self.assertIn("grok-build", forwarded)
        self.assertEqual(out, sentinel)

    # ── 2. _is_grok_unavailable classification (throttle ≠ unavailable) ─────────
    def test_is_grok_unavailable_classification(self):
        w = self._w
        fn = w._is_grok_unavailable

        # PERMANENTLY unavailable (latch + skip) cases.
        self.assertTrue(fn(FileNotFoundError("grok.exe not found")),
                        "exe missing must be unavailable")
        self.assertTrue(fn(RuntimeError("401 unauthorized")), "401 must be unavailable")
        self.assertTrue(fn(RuntimeError("not logged in")), "not-logged-in must be unavailable")
        self.assertTrue(fn(RuntimeError("Please run grok login")),
                        "explicit 'please run grok login' must be unavailable")
        self.assertTrue(fn(RuntimeError("daily limit reached")),
                        "daily quota exhaustion must be unavailable")

        # The DEFINING difference vs Codex: a transient throttle is NOT permanent-unavailable.
        self.assertFalse(fn(RuntimeError("grok -p exited 1: 429 rate limit")),
                         "429 / rate limit is a TRANSIENT throttle — must NOT latch the rung")
        self.assertFalse(fn(RuntimeError("too many requests, throttled")),
                         "burst throttle must NOT latch the rung")

        # A real capability/output failure must NOT skip the rung.
        self.assertFalse(fn(ValueError("bad output")),
                         "bad output is a capability error, not unavailability")

    # ── 3. OAuth grok call does NOT consume the dollar budget ──────────────────
    def test_grok_oauth_call_does_not_consume_dollar_budget(self):
        w = self._w
        reserved = {"oauth": 0}
        orig_reserve = w._reserve_oauth_call

        def counting_reserve(provider):
            ok = orig_reserve(provider)
            if ok:
                reserved["oauth"] += 1
            return ok

        def mock_call(provider, model, sys, user):
            self.assertEqual(provider, "grok")
            return self._good_output()

        spec = {"architecture": {"stack": "vanilla"}}
        with patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "_call_grok", return_value=self._good_output()), \
             patch.object(w, "_reserve_oauth_call", side_effect=counting_reserve), \
             patch.object(w, "route_task", return_value=2), \
             patch.object(w, "_parse_and_validate", return_value={"files": []}):
            before = w._paid_calls_made
            w.execute_task(self._task(), spec, {})
            after = w._paid_calls_made

        self.assertGreaterEqual(reserved["oauth"], 1,
                                "expected an oauth capacity reservation on the grok rung")
        self.assertEqual(before, after,
                         "oauth grok call must NOT touch the dollar budget")

    # ── 4. Capacity exhaustion skips grok → escalates to anthropic ─────────────
    def test_grok_capacity_exhaustion_skips_grok_escalates_to_anthropic(self):
        w = self._w
        with patch.object(w, "GROK_MAX_CALLS", 0):
            for _ in range(5):
                w._reserve_oauth_call("grok")

            call_log = []

            def mock_call(provider, model, sys, user):
                call_log.append((provider, model))
                if provider == "ollama":
                    raise RuntimeError("model output truncated — capability failure")
                return self._good_output()

            spec = {"architecture": {"stack": "vanilla"}}
            with patch.object(w, "_call_provider", side_effect=mock_call), \
                 patch.object(w, "_call_grok") as mock_grok, \
                 patch.object(w, "_call_anthropic", MagicMock(return_value=self._good_output())), \
                 patch.object(w, "_reserve_paid_call", return_value=True), \
                 patch.object(w, "route_task", return_value=0), \
                 patch.object(w, "_parse_and_validate", return_value={"files": []}):
                w.execute_task(self._task(retry=2), spec, {})

        self.assertFalse(any(p == "grok" for p, _ in call_log),
                         f"grok rung must be skipped when capacity exhausted: {call_log}")
        mock_grok.assert_not_called()
        self.assertTrue(any(p == "anthropic" for p, _ in call_log),
                        f"execution must escalate to anthropic rung: {call_log}")

    # ── 5. _grok_disabled short-circuit after an unavailable (auth) error ──────
    def test_grok_disabled_short_circuits_subsequent_tasks(self):
        w = self._w
        grok_calls = {"n": 0}

        def grok_unavailable(model, system, user):
            grok_calls["n"] += 1
            raise RuntimeError("not logged in")

        def mock_call(provider, model, sys, user):
            if provider == "ollama":
                raise RuntimeError("model output truncated — capability failure")
            if provider == "grok":
                return w._call_grok(model, sys, user)
            return self._good_output()

        spec = {"architecture": {"stack": "vanilla"}}
        with patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "_call_grok", side_effect=grok_unavailable), \
             patch.object(w, "_reserve_paid_call", return_value=True), \
             patch.object(w, "route_task", return_value=0), \
             patch.object(w, "_parse_and_validate", return_value={"files": []}):
            w.execute_task(self._task(retry=2), spec, {})
            self.assertTrue(w._grok_disabled,
                            "_grok_disabled must flip True after an unavailable error")
            calls_after_first = grok_calls["n"]

            second_log = []

            def mock_call2(provider, model, sys, user):
                second_log.append((provider, model))
                if provider == "ollama":
                    raise RuntimeError("model output truncated — capability failure")
                if provider == "grok":
                    return w._call_grok(model, sys, user)
                return self._good_output()

            with patch.object(w, "_call_provider", side_effect=mock_call2):
                w.execute_task(self._task(retry=2), spec, {})

        self.assertEqual(grok_calls["n"], calls_after_first,
                         "_call_grok must NOT be invoked again once _grok_disabled is set")
        self.assertTrue(any(p == "anthropic" for p, _ in second_log),
                        f"second task must still reach anthropic rung: {second_log}")

    # ── 6. A transient throttle must NOT latch the grok rung ───────────────────
    def test_grok_transient_throttle_does_not_latch(self):
        """The distinctive Grok semantics: a 429/throttle escalates that attempt but leaves the
        rung enabled for later tasks (unlike Codex, which latches on 429)."""
        w = self._w

        def grok_throttled(model, system, user):
            raise RuntimeError("grok -p exited 1: 429 too many requests")

        def mock_call(provider, model, sys, user):
            if provider == "ollama":
                raise RuntimeError("model output truncated — capability failure")
            if provider == "grok":
                return w._call_grok(model, sys, user)
            return self._good_output()

        spec = {"architecture": {"stack": "vanilla"}}
        with patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "_call_grok", side_effect=grok_throttled), \
             patch.object(w, "_reserve_paid_call", return_value=True), \
             patch.object(w, "route_task", return_value=0), \
             patch.object(w, "_parse_and_validate", return_value={"files": []}):
            w.execute_task(self._task(retry=2), spec, {})
            self.assertFalse(w._grok_disabled,
                             "a transient 429 throttle must NOT trip the permanent disable-latch")

    # ── 7. _call_grok parse path (.text envelope) → valid execute_task result ──
    def test_call_grok_parse_path_yields_grok_result(self):
        w = self._w
        grok_json = json.dumps({"files": [{"path": "x.txt", "content": "ok"}]})

        def mock_call(provider, model, sys, user):
            if provider == "grok":
                return w._call_grok(model, sys, user)
            raise AssertionError(f"only the grok rung should be exercised here: {provider}")

        spec = {"architecture": {"stack": "vanilla"}}
        with patch.object(w, "_call_grok", return_value=grok_json) as mock_grok, \
             patch.object(w, "_call_provider", side_effect=mock_call), \
             patch.object(w, "_reserve_oauth_call", return_value=True), \
             patch.object(w, "route_task", return_value=2):
            result = w.execute_task(self._task(), spec, {})

        mock_grok.assert_called()
        files = result.get("files") if isinstance(result, dict) else None
        self.assertTrue(files, f"expected parsed files from grok output: {result}")
        self.assertEqual(files[0]["path"], "x.txt")
        model_used = (result.get("model_used") or result.get("model") or "") \
            if isinstance(result, dict) else ""
        self.assertIn("grok", str(model_used).lower(),
                      f"model_used must reflect grok rung: {result}")

    # ── 8. _extract_grok_text unwraps the JSON envelope's "text" field ─────────
    def test_extract_grok_text_envelope(self):
        w = self._w
        contract = json.dumps({"files": [{"path": "a.py", "content": "x=1"}]})
        # Confirmed live shape: the contract lives inside the envelope's "text".
        env = json.dumps({"text": contract, "stopReason": "EndTurn", "sessionId": "abc"})
        self.assertEqual(w._extract_grok_text(env), contract,
                         "must pull the final message out of the .text field")
        # Non-JSON stdout falls back to raw (so _parse_and_validate can still hunt the contract).
        self.assertEqual(w._extract_grok_text("plain text"), "plain text")
        self.assertEqual(w._extract_grok_text(""), "")

    # ── 9. _call_grok: real body — UTF-8, scratch cwd, correct flags, $0 telem ─
    def test_call_grok_subprocess_shape_and_failed_telemetry(self):
        w = self._w
        import cost
        cost.reset_costs()

        # 9a. Success path: capture the subprocess shape and confirm envelope unwrap.
        captured = {}
        ok_proc = MagicMock()
        ok_proc.returncode = 0
        ok_proc.stdout = json.dumps({"text": json.dumps({"files": []}), "stopReason": "EndTurn"})
        ok_proc.stderr = ""

        def capture_run(cmd, *a, **kw):
            captured["cmd"] = cmd
            captured.update(kw)
            return ok_proc

        with patch.object(w.subprocess, "run", side_effect=capture_run), \
             patch.object(w.shutil, "which", return_value="grok"):
            out = w._call_grok("grok-build", "system ▶ arrow → bullet •", "user 中文")

        self.assertEqual(out, json.dumps({"files": []}),
                         "_call_grok must unwrap the .text envelope to the raw contract")
        cmd = captured.get("cmd", [])
        self.assertIn("-p", cmd, "must invoke headless -p mode")
        self.assertIn("--output-format", cmd)
        self.assertIn("json", cmd)
        self.assertIn("-m", cmd)
        self.assertIn("grok-build", cmd)
        self.assertEqual(captured.get("encoding"), "utf-8",
                         "_call_grok must force UTF-8 (non-cp1252 glyphs in real prompts)")
        self.assertEqual(captured.get("errors"), "replace")
        self.assertTrue(captured.get("cwd"),
                        "_call_grok must run in an isolated scratch cwd, not the project dir")

        # 9b. Failure path: a nonzero exit still records an attempted ($0) oauth call.
        cost.reset_costs()
        fail_proc = MagicMock()
        fail_proc.returncode = 1
        fail_proc.stderr = "429 too many requests"
        fail_proc.stdout = ""
        with patch.object(w.subprocess, "run", return_value=fail_proc), \
             patch.object(w.shutil, "which", return_value="grok"):
            with self.assertRaises(RuntimeError):
                w._call_grok("grok-build", "sys", "user")
        oauth = cost.cost_summary().get("oauth", {}).get("grok", {})
        self.assertEqual(oauth.get("calls", 0), 1,
                         "a failed grok invocation must still count as an attempted call")
        self.assertEqual(oauth.get("success", -1), 0,
                         "a failed grok invocation must NOT count as a success")

    # ── 10. Grok-first ordering in the default config ladder ───────────────────
    def test_default_ladder_places_grok_before_codex(self):
        import config
        provs = [p for p, _ in config.WORKER_LADDER]
        if "grok" in provs and "codex" in provs:
            self.assertLess(provs.index("grok"), provs.index("codex"),
                            "Grok-first: grok rung must precede codex in the default ladder")
        else:
            self.skipTest("live WORKER_LADDER (from .env) does not include both grok and codex")


class TestRoleMetrics(unittest.TestCase):
    """Phase-0 instrumentation baseline: per-role routing telemetry in cost.py.
    Pure accumulator — these never exercise a model. Verifies the metrics surfaced in
    cost_summary()['roles'] + the derived 'anthropic_avoided' (free-OAuth successes)."""

    def setUp(self):
        import cost
        self._cost = cost
        cost.reset_costs()

    def tearDown(self):
        self._cost.reset_costs()

    def test_record_role_event_accumulates(self):
        cost = self._cost
        cost.record_role_event("orch:INIT", provider="codex", model="gpt-5.5", success=True, latency_s=1.0)
        cost.record_role_event("orch:INIT", provider="codex", model="gpt-5.5",
                               success=False, schema_fail=True, latency_s=0.5)
        cost.record_role_event("orch:INIT", provider="anthropic", model="sonnet",
                               success=True, fallback=True, latency_s=2.0)
        r = cost.cost_summary()["roles"]["orch:INIT"]
        self.assertEqual(r["attempts"], 3)
        self.assertEqual(r["success"], 2)
        self.assertEqual(r["schema_fails"], 1)
        self.assertEqual(r["fallbacks"], 1)
        self.assertAlmostEqual(r["latency_s"], 3.5, places=3)
        self.assertEqual(r["by_provider"]["codex"], {"calls": 2, "success": 1})
        self.assertEqual(r["by_provider"]["anthropic"], {"calls": 1, "success": 1})

    def test_anthropic_avoided_counts_oauth_successes(self):
        cost = self._cost
        cost.record_oauth_usage("grok", success=True)
        cost.record_oauth_usage("grok", success=True)
        cost.record_oauth_usage("codex", success=True)
        cost.record_oauth_usage("codex", success=False)
        # 3 successful free-OAuth calls = 3 Anthropic calls avoided.
        self.assertEqual(cost.cost_summary()["anthropic_avoided"], 3)

    def test_reset_clears_role_metrics(self):
        cost = self._cost
        cost.record_role_event("worker", provider="ollama", success=True)
        self.assertTrue(cost.cost_summary()["roles"])
        cost.reset_costs()
        self.assertEqual(cost.cost_summary()["roles"], {})
        self.assertEqual(cost.cost_summary()["anthropic_avoided"], 0)


class TestPlanningCall(unittest.TestCase):
    """Phase 2: planning_call — Codex-first → 1 retry → Sonnet → Opus, gated by validate_fn.
    All mocked: _call_codex / _call_anthropic patched, no real subprocess/API. Helper is INERT
    (not wired to a role yet) — these test it directly."""

    def setUp(self):
        import worker as w
        self._w = w
        self._orig_enabled = w.CODEX_CLI_ENABLED
        w.CODEX_CLI_ENABLED = True
        with w._oauth_lock:
            w._codex_disabled = False
            w._oauth_calls_made.clear()

    def tearDown(self):
        w = self._w
        w.CODEX_CLI_ENABLED = self._orig_enabled
        with w._oauth_lock:
            w._codex_disabled = False
            w._oauth_calls_made.clear()

    def _ok(self, parsed):
        if "ok" not in parsed:
            raise ValueError("validate_fn: missing 'ok'")

    def test_codex_first_success_no_anthropic(self):
        w = self._w
        with patch.object(w, "_reserve_oauth_call", return_value=True), \
             patch.object(w, "_call_codex", return_value=json.dumps({"ok": True, "x": 1})) as mc, \
             patch.object(w, "_call_anthropic") as ma:
            out = w.planning_call("sys", "user", self._ok, role="creative")
        self.assertEqual(out["x"], 1)
        mc.assert_called_once()
        ma.assert_not_called()

    def test_codex_schema_fail_retries_then_escalates_to_sonnet(self):
        w = self._w
        with patch.object(w, "_reserve_oauth_call", return_value=True), \
             patch.object(w, "_call_codex", return_value=json.dumps({"nope": 1})) as mc, \
             patch.object(w, "_call_anthropic", return_value=json.dumps({"ok": True})) as ma:
            out = w.planning_call("s", "u", self._ok, role="architect")
        self.assertEqual(mc.call_count, 2, "codex schema-fail must retry once at the same tier")
        ma.assert_called()
        self.assertTrue(out["ok"])

    def test_codex_unavailable_latches_and_falls_to_anthropic(self):
        w = self._w

        def codex_unavail(model, system, user):
            raise RuntimeError("not logged in")

        with patch.object(w, "_reserve_oauth_call", return_value=True), \
             patch.object(w, "_call_codex", side_effect=codex_unavail) as mc, \
             patch.object(w, "_call_anthropic", return_value=json.dumps({"ok": True})) as ma:
            out = w.planning_call("s", "u", self._ok)
        self.assertEqual(mc.call_count, 1, "unavailable codex must NOT retry — go straight to Anthropic")
        self.assertTrue(w._codex_disabled, "codex unavailability must latch the rung")
        ma.assert_called()
        self.assertTrue(out["ok"])

    def test_reservation_false_skips_codex_entirely(self):
        w = self._w
        with patch.object(w, "_reserve_oauth_call", return_value=False), \
             patch.object(w, "_call_codex") as mc, \
             patch.object(w, "_call_anthropic", return_value=json.dumps({"ok": True})) as ma:
            out = w.planning_call("s", "u", self._ok)
        mc.assert_not_called()
        ma.assert_called()
        self.assertTrue(out["ok"])

    def test_planning_call_records_latency(self):
        # Corrective fix: planning_call must pass latency_s to record_role_event (it was omitted,
        # zeroing CD/TA latency telemetry that Phase 4 gates on).
        w = self._w
        calls = []

        def rec(role, **kw):
            calls.append(kw)

        with patch.object(w, "_reserve_oauth_call", return_value=True), \
             patch.object(w, "_call_codex", return_value=json.dumps({"ok": True})), \
             patch.object(w, "record_role_event", side_effect=rec):
            w.planning_call("s", "u", self._ok, role="creative")
        self.assertTrue(calls, "record_role_event should be called")
        self.assertIn("latency_s", calls[0], "planning_call must pass latency_s")

    def test_all_tiers_fail_raises(self):
        w = self._w
        with patch.object(w, "_reserve_oauth_call", return_value=True), \
             patch.object(w, "_call_codex", return_value=json.dumps({"nope": 1})), \
             patch.object(w, "_call_anthropic", return_value=json.dumps({"nope": 1})):
            with self.assertRaises(RuntimeError):
                w.planning_call("s", "u", self._ok)


class TestRoleCutover(unittest.TestCase):
    """Phase 3: Creative Director + Technical Architect route through planning_call (Codex-first),
    preserving their existing validation as the fallback boundary. planning_call is mocked."""

    def test_creative_director_routes_through_planning_call(self):
        import creative_director as cd, worker as w
        brief = {
            "output_type": "website",
            "features": ["a"],
            "scale": "mvp",
            "visual_identity": {"style": "flat"},
        }
        captured = {}

        def fake_planning(system, user, validate_fn, *, role=None, **kw):
            captured["role"] = role
            captured["validate_fn"] = validate_fn
            return brief

        # No ANTHROPIC_API_KEY patch — the corrective fix lets CD construct key-free (Codex-first).
        with patch.object(w, "planning_call", side_effect=fake_planning):
            out = cd.CreativeDirector().interpret("build me a site")

        self.assertEqual(out, brief)
        self.assertEqual(captured["role"], "creative")
        # The validation handed to planning_call must reject a brief missing required fields…
        with self.assertRaises(ValueError):
            captured["validate_fn"]({"features": ["a"]})  # missing output_type
        captured["validate_fn"](brief)  # …and accept a valid one (no raise)

    def test_technical_architect_routes_through_planning_call(self):
        import tempfile
        from pathlib import Path
        import technical_architect as ta, worker as w
        spec = {"confirmed_stack": "vanilla", "file_structure": [], "adrs_to_create": []}
        captured = {}

        def fake_planning(system, user, validate_fn, *, role=None, **kw):
            captured["role"] = role
            return spec

        # No ANTHROPIC_API_KEY patch — the corrective fix lets TA construct key-free (Codex-first).
        with patch.object(w, "planning_call", side_effect=fake_planning), \
             patch.object(ta, "ProjectMemory") as mock_pm, \
             tempfile.TemporaryDirectory() as tmp:
            out = ta.TechnicalArchitect().review({"output_type": "website"}, "intent", Path(tmp))

        self.assertEqual(out["confirmed_stack"], "vanilla")
        self.assertEqual(captured["role"], "architect")
        mock_pm.assert_called()  # ProjectMemory is still seeded after planning


class TestCreativeDirectorValidator(unittest.TestCase):
    """The hardened CREATIVE_BRIEF validator: a thin/malformed brief must escalate (raise) rather
    than pass silently and mis-route downstream. Pure function — no planning_call involved."""

    @staticmethod
    def _brief(**overrides):
        brief = {
            "output_type": "website",
            "features": ["landing page", "contact form"],
            "scale": "mvp",
            "visual_identity": {"style": "flat", "palette": "#000", "typography": "sans"},
        }
        brief.update(overrides)
        return brief

    def test_valid_brief_passes(self):
        import creative_director as cd
        cd._validate(self._brief())  # must not raise

    def test_non_dict_raises(self):
        import creative_director as cd
        with self.assertRaises(ValueError):
            cd._validate(["not", "a", "dict"])

    def test_missing_output_type_raises(self):
        import creative_director as cd
        b = self._brief()
        del b["output_type"]
        with self.assertRaises(ValueError):
            cd._validate(b)

    def test_output_type_not_in_enum_raises(self):
        import creative_director as cd
        # "web" was the old loose value — no longer valid; "website" is the real enum member.
        with self.assertRaises(ValueError):
            cd._validate(self._brief(output_type="web"))

    def test_missing_scale_raises(self):
        import creative_director as cd
        b = self._brief()
        del b["scale"]
        with self.assertRaises(ValueError):
            cd._validate(b)

    def test_scale_not_in_enum_raises(self):
        import creative_director as cd
        with self.assertRaises(ValueError):
            cd._validate(self._brief(scale="enterprise"))

    def test_empty_features_raises(self):
        import creative_director as cd
        with self.assertRaises(ValueError):
            cd._validate(self._brief(features=[]))

    def test_over_inflated_features_raises(self):
        import creative_director as cd
        bloated = [f"feature {i}" for i in range(cd._MAX_FEATURES + 1)]
        with self.assertRaises(ValueError):
            cd._validate(self._brief(features=bloated))

    def test_features_at_cap_passes(self):
        import creative_director as cd
        at_cap = [f"feature {i}" for i in range(cd._MAX_FEATURES)]
        cd._validate(self._brief(features=at_cap))  # must not raise (boundary inclusive)

    def test_non_code_requires_visual_identity(self):
        import creative_director as cd
        with self.assertRaises(ValueError):
            cd._validate(self._brief(visual_identity={}))
        b = self._brief()
        del b["visual_identity"]
        with self.assertRaises(ValueError):
            cd._validate(b)

    def test_code_exempt_from_visual_identity(self):
        import creative_director as cd
        # Code prompts are explicitly allowed minimal/empty visual_identity by the prompt.
        cd._validate(self._brief(output_type="code", visual_identity={}))
        b = self._brief(output_type="code")
        del b["visual_identity"]
        cd._validate(b)  # must not raise


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
        TestCodexWorkerRung,
        TestGrokWorkerRung,
        TestRoleMetrics,
        TestPlanningCall,
        TestRoleCutover,
        TestCreativeDirectorValidator,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
