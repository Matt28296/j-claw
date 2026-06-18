"""Tests for the build email reporter (email_report.py).

Pure, $0, network-free surface only: subject-label derivation, the EMAIL_ENABLED gate, the
disabled-send no-op, and the log-summary parser. No SMTP is ever opened — send_email must short
-circuit to False before touching the network whenever the feature is disabled or creds are missing.
"""
from __future__ import annotations
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HARNESS = os.path.join(os.path.dirname(__file__), "..")
if _HARNESS not in sys.path:
    sys.path.insert(0, _HARNESS)

import email_report


_LOG_WITH_INTENT = (
    "┌──────────────────────────── J-Claw Project ────────────────────────────┐\n"
    "│ Build a League of Legends style MOBA game with online multiplayer        │\n"
    "└─────────────────────────────────────────────────────────────────────────┘\n"
    "▶ task-001  ...\n"
    "  ✓ done\n"
)


class TestBuildLabel(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, text):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_explicit_override_wins(self):
        p = self._write("a.log", _LOG_WITH_INTENT)
        self.assertEqual(email_report.build_label(p, "My Cool Build"), "My Cool Build")

    def test_override_takes_precedence_over_intent(self):
        # Even with a parseable intent, an explicit label is used verbatim.
        p = self._write("a.log", _LOG_WITH_INTENT)
        self.assertEqual(email_report.build_label(p, "X"), "X")

    def test_derives_short_intent_from_log(self):
        p = self._write("a.log", _LOG_WITH_INTENT)
        label = email_report.build_label(p, None)
        # First ~6 words of the intent, truncated with an ellipsis — and crucially NOT "MOBA build".
        self.assertTrue(label.startswith("Build a League of Legends style"))
        self.assertTrue(label.endswith("…"))

    def test_missing_log_falls_back_to_generic(self):
        self.assertEqual(email_report.build_label(self.tmp / "nope.log", None), "build")

    def test_blank_override_falls_back(self):
        p = self._write("a.log", "no project box here\n")
        self.assertEqual(email_report.build_label(p, "   "), "build")


class TestEmailEnabledGate(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {"EMAIL_ENABLED": ""}, clear=False):
            self.assertFalse(email_report.email_enabled())

    def test_truthy_values(self):
        for v in ("1", "true", "TRUE", "yes", "on"):
            with patch.dict(os.environ, {"EMAIL_ENABLED": v}, clear=False):
                self.assertTrue(email_report.email_enabled(), v)

    def test_falsy_values(self):
        for v in ("0", "false", "no", "off", "nope"):
            with patch.dict(os.environ, {"EMAIL_ENABLED": v}, clear=False):
                self.assertFalse(email_report.email_enabled(), v)


class TestSendIsNoOpWhenDisabled(unittest.TestCase):
    def test_disabled_send_returns_false_without_smtp(self):
        # Must short-circuit on the enabled gate BEFORE any smtplib use.
        with patch.dict(os.environ, {"EMAIL_ENABLED": "false"}, clear=False), \
                patch("smtplib.SMTP", side_effect=AssertionError("must not open SMTP")):
            self.assertFalse(email_report.send_email("s", "b"))

    def test_enabled_but_missing_creds_returns_false(self):
        env = {"EMAIL_ENABLED": "true", "EMAIL_SMTP_USER": "", "EMAIL_SMTP_PASSWORD": "",
               "EMAIL_TO": ""}
        with patch.dict(os.environ, env, clear=False), \
                patch("smtplib.SMTP", side_effect=AssertionError("must not open SMTP")):
            self.assertFalse(email_report.send_email("s", "b"))


class TestSummarizeLog(unittest.TestCase):
    def test_missing_log_message(self):
        out = email_report.summarize_log(Path(tempfile.gettempdir()) / "definitely_absent.log")
        self.assertIn("No build log found", out)

    def test_counters_parsed(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            log = tmp / "b.log"
            log.write_text(_LOG_WITH_INTENT + "  ✓ done\n  ✗ error: boom\n", encoding="utf-8")
            out = email_report.summarize_log(log)
            self.assertIn("=== Counters ===", out)
            self.assertIn("Tasks completed", out)
            self.assertIn("Project:", out)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
