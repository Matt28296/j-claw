"""
test_wave4_metered.py — Wave 4, Task C2 regression coverage for the three
metered Anthropic call sites that bypassed the per-build cost ceiling.

Zero API spend (every client constructor / request is mocked). Run with:
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python harness/test_wave4_metered.py

Sites guarded (each gets two regression tests):
  1. final_review.run_final_review        — already recorded; was missing the check
  2. handoff._stamp_via_api               — was missing BOTH check and record_usage
  3. e2e_generator._call_worker (anthropic)— was missing BOTH check and record_usage
  4. orchestrator._OpenAICompatOrchestrator.call — skipped the base-class check

For each site:
  - "refuses when tripped": with the REAL ceiling pre-tripped, the metered call
    raises BuildCostCeilingExceeded BEFORE the API client is constructed (no spend).
  - "records spend": under a high ceiling, a successful call drives the cost
    accumulator up (record_usage is wired), so the accumulator can no longer
    under-count.
"""

from __future__ import annotations
import os
import sys
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Environment setup (mirrors test_llm_layers.py) ──────────────────────────────
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("GOOGLE_API_KEY", "test-google-key")
os.environ.setdefault("OPENROUTER_API_KEY", "test-or-key")
os.environ.setdefault("ORCHESTRATOR_PROVIDER", "gemini")
os.environ.setdefault("OLLAMA_HOST", "http://localhost:11434")

sys.path.insert(0, str(Path(__file__).parent))

import cost  # noqa: E402


class _U:
    """Minimal stand-in for an Anthropic response.usage."""
    def __init__(self, i=0, o=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


def _anthropic_response(text: str, usage):
    """Fake anthropic Messages response: .content[0].text + .usage + .stop_reason."""
    resp = MagicMock()
    block = MagicMock()
    block.text = text
    resp.content = [block]
    resp.usage = usage
    resp.stop_reason = "end_turn"
    return resp


# A spend big enough to trip a $5 ceiling: 1M sonnet output ($15) → $15 >= $5.
def _trip_ceiling():
    cost.record_usage(_U(o=1_000_000), "claude-sonnet-4-6", "pretrip")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. final_review.run_final_review
# ═══════════════════════════════════════════════════════════════════════════════

class TestFinalReviewCeiling(unittest.TestCase):
    def setUp(self):
        cost.reset_costs()

    def _make_output(self, tmp: Path):
        (tmp / "app.py").write_text("print('hi')\n", encoding="utf-8")

    def test_refuses_when_tripped(self):
        import config
        import final_review
        with patch.object(config, "MAX_BUILD_COST_USD", 5.0), \
             patch.object(config, "MAX_BUILD_TOKENS", 0), \
             patch.object(final_review, "ANTHROPIC_API_KEY", "sk-ant-test"), \
             patch("anthropic.Anthropic") as mock_client:
            _trip_ceiling()
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                out = Path(d)
                self._make_output(out)
                with self.assertRaises(cost.BuildCostCeilingExceeded):
                    final_review.run_final_review(out, {"goal": "x"})
            mock_client.assert_not_called()  # never spent

    def test_records_spend_on_success(self):
        import config
        import final_review
        with patch.object(config, "MAX_BUILD_COST_USD", 1000.0), \
             patch.object(config, "MAX_BUILD_TOKENS", 0), \
             patch.object(final_review, "ANTHROPIC_API_KEY", "sk-ant-test"), \
             patch.object(final_review, "FINAL_REVIEW_MODEL", "claude-sonnet-4-6"), \
             patch.object(final_review, "_review_via_cli", return_value=None), \
             patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = \
                _anthropic_response("VERDICT: PASS\n", _U(i=1_000_000, o=1_000_000))
            import tempfile
            before = cost.cost_summary()["total_usd"]
            with tempfile.TemporaryDirectory() as d:
                out = Path(d)
                self._make_output(out)
                final_review.run_final_review(out, {"goal": "x"})
            after = cost.cost_summary()["total_usd"]
            self.assertGreater(after, before)  # spend recorded


# ═══════════════════════════════════════════════════════════════════════════════
# 2. handoff._stamp_via_api
# ═══════════════════════════════════════════════════════════════════════════════

class TestStampCeiling(unittest.TestCase):
    def setUp(self):
        cost.reset_costs()

    def test_refuses_when_tripped(self):
        import config
        import handoff
        with patch.object(config, "MAX_BUILD_COST_USD", 5.0), \
             patch.object(config, "MAX_BUILD_TOKENS", 0), \
             patch.object(handoff, "ANTHROPIC_API_KEY", "sk-ant-test"), \
             patch.object(handoff, "_collect_stamp_context", return_value="ctx"), \
             patch.object(handoff, "_append_verdict") as mock_append, \
             patch("anthropic.Anthropic") as mock_client:
            _trip_ceiling()
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                # The ceiling must fail CLOSED here: it must NOT be masked by the
                # broad try/except inside _stamp_via_api (which would swallow it
                # and silently skip the stamp).
                with self.assertRaises(cost.BuildCostCeilingExceeded):
                    handoff._stamp_via_api(Path(d) / "HANDOFF.md", Path(d))
            mock_client.assert_not_called()   # never spent
            mock_append.assert_not_called()   # no verdict written

    def test_records_spend_on_success(self):
        import config
        import handoff
        with patch.object(config, "MAX_BUILD_COST_USD", 1000.0), \
             patch.object(config, "MAX_BUILD_TOKENS", 0), \
             patch.object(handoff, "ANTHROPIC_API_KEY", "sk-ant-test"), \
             patch.object(handoff, "ORCHESTRATOR_MODEL", "claude-sonnet-4-6"), \
             patch.object(handoff, "_collect_stamp_context", return_value="ctx"), \
             patch.object(handoff, "_append_verdict"), \
             patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = \
                _anthropic_response("OPENCLAW: APPROVED", _U(i=1_000_000, o=1_000_000))
            import tempfile
            before = cost.cost_summary()["total_usd"]
            with tempfile.TemporaryDirectory() as d:
                handoff._stamp_via_api(Path(d) / "HANDOFF.md", Path(d))
            after = cost.cost_summary()["total_usd"]
            self.assertGreater(after, before)  # spend now recorded (was a silent gap)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. e2e_generator._call_worker (anthropic branch)
# ═══════════════════════════════════════════════════════════════════════════════

class TestE2EGeneratorCeiling(unittest.TestCase):
    def setUp(self):
        cost.reset_costs()

    def test_refuses_when_tripped(self):
        import config
        import e2e_generator
        with patch.object(config, "MAX_BUILD_COST_USD", 5.0), \
             patch.object(config, "MAX_BUILD_TOKENS", 0), \
             patch.object(e2e_generator, "WORKER_PROVIDER", "anthropic"), \
             patch.object(e2e_generator, "WORKER_MODEL", "claude-sonnet-4-6"), \
             patch.object(e2e_generator, "WORKER_FALLBACKS", []), \
             patch("config.ANTHROPIC_API_KEY", "sk-ant-test"), \
             patch("anthropic.Anthropic") as mock_client:
            _trip_ceiling()
            # Fail closed: must raise (not swallow + fall through to another
            # provider returning None) and must not construct the client.
            with self.assertRaises(cost.BuildCostCeilingExceeded):
                e2e_generator._call_worker("sys", "user")
            mock_client.assert_not_called()

    def test_records_spend_on_success(self):
        import config
        import e2e_generator
        with patch.object(config, "MAX_BUILD_COST_USD", 1000.0), \
             patch.object(config, "MAX_BUILD_TOKENS", 0), \
             patch.object(e2e_generator, "WORKER_PROVIDER", "anthropic"), \
             patch.object(e2e_generator, "WORKER_MODEL", "claude-sonnet-4-6"), \
             patch.object(e2e_generator, "WORKER_FALLBACKS", []), \
             patch("config.ANTHROPIC_API_KEY", "sk-ant-test"), \
             patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = \
                _anthropic_response('{"files": []}', _U(i=1_000_000, o=1_000_000))
            before = cost.cost_summary()["total_usd"]
            out = e2e_generator._call_worker("sys", "user")
            after = cost.cost_summary()["total_usd"]
            self.assertEqual(out, '{"files": []}')
            self.assertGreater(after, before)  # spend now recorded (was a silent gap)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. orchestrator._OpenAICompatOrchestrator.call
# ═══════════════════════════════════════════════════════════════════════════════

class TestOpenAICompatOrchestratorCeiling(unittest.TestCase):
    def setUp(self):
        cost.reset_costs()

    def _make_orch(self):
        from orchestrator import _OpenAICompatOrchestrator
        orch = _OpenAICompatOrchestrator.__new__(_OpenAICompatOrchestrator)
        orch._model_chain = ["gemini-2.5-flash"]
        orch._system_prompt = "sys"
        orch._provider_name = "gemini"
        orch._quota_failfast = False
        orch._client = MagicMock()
        return orch

    def test_refuses_when_tripped(self):
        import config
        with patch.object(config, "MAX_BUILD_COST_USD", 5.0), \
             patch.object(config, "MAX_BUILD_TOKENS", 0):
            orch = self._make_orch()
            _trip_ceiling()
            with self.assertRaises(cost.BuildCostCeilingExceeded):
                orch.call({"system_state": "SPEC_ACCEPTED"}, max_retries=3)
            # The ceiling check sits BEFORE the request: no completion attempted.
            orch._client.chat.completions.create.assert_not_called()

    def test_under_ceiling_allows_call(self):
        import config
        with patch.object(config, "MAX_BUILD_COST_USD", 1000.0), \
             patch.object(config, "MAX_BUILD_TOKENS", 0):
            orch = self._make_orch()
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = json.dumps({"tasks": []})
            orch._client.chat.completions.create.return_value = resp
            result = orch.call({"system_state": "SPEC_ACCEPTED"}, max_retries=3)
            self.assertEqual(result, {"tasks": []})
            orch._client.chat.completions.create.assert_called_once()


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
