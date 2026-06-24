"""Deterministic secret/PII scrubbing for j-claw training-data export.

Ported from The-Brain's `brain/redact.py` (same conservative philosophy: better to over-redact than to
leak a key into a dataset a model would memorize) and extended with j-claw-specific cases: OpenRouter
keys, JWTs, OAuth/Bearer tokens, generic `KEY = value` env assignments (covers Pinata/Anthropic/Google/
etc.), emails, and absolute Windows/Unix user paths.

Pure/stdlib, self-testing:  python -m training.secret_scrub   (or run this file directly)
"""
from __future__ import annotations

import re

SCRUBBER_VERSION = "1.0.0"
REDACTED = "[REDACTED]"
EMAIL_MARK = "[EMAIL]"

# (pattern, replacement) applied in order. Order matters: specific high-entropy tokens before the
# generic KEY=value catch-all, and path/email markers last.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"), REDACTED),                  # Anthropic
    (re.compile(r"sk-or-[A-Za-z0-9_\-]{16,}"), REDACTED),                   # OpenRouter
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), REDACTED),                      # OpenAI-style
    (re.compile(r"AKIA[0-9A-Z]{16}"), REDACTED),                            # AWS access key id
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),                  # GitHub PAT/OAuth/refresh
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), REDACTED),               # Slack
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), REDACTED),                     # Google API key
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"), REDACTED),           # Telegram bot token
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), REDACTED),  # JWT
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"), REDACTED),           # bearer tokens
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
     REDACTED),                                                             # PEM private keys
    (re.compile(
        # \w* lead so prefixed env names match too, e.g. ANTHROPIC_API_KEY=, OPENAI_API_KEY=, BOT_TOKEN=
        r"(?i)\b\w*(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key|auth[_-]?token"
        r"|client[_-]?secret|pinata[_-]?\w*key)\b\s*[:=]\s*['\"]?[A-Za-z0-9._\-/+]{8,}['\"]?"),
     REDACTED),                                                             # generic KEY=value
    (re.compile(r"(?i)https?://[^\s/]*:[^\s/@]+@[^\s]+"), REDACTED),        # creds embedded in a URL
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), EMAIL_MARK),  # email
    (re.compile(r"(?i)([A-Za-z]:\\Users\\)[^\\/:*?\"<>|\r\n]+"), r"\1[USER]"),   # Windows user path
    (re.compile(r"(?i)(/(?:home|Users)/)[^/\s:*?\"<>|]+"), r"\1[USER]"),         # Unix home path
]


def scrub_text(text):
    """Mask secrets/PII in a string. Non-strings are returned unchanged (never crashes)."""
    if not isinstance(text, str):
        return text
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def scrub_obj(obj):
    """Recursively scrub strings inside dicts/lists/tuples; other types pass through untouched."""
    if isinstance(obj, str):
        return scrub_text(obj)
    if isinstance(obj, dict):
        return {(scrub_text(k) if isinstance(k, str) else k): scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(scrub_obj(v) for v in obj)
    return obj


def contains_secret(text) -> bool:
    """True if scrubbing would change the text — i.e. a secret/PII pattern is present.
    Used to DROP a training row whose output code embeds a secret (don't train on it, even scrubbed)."""
    return isinstance(text, str) and scrub_text(text) != text


if __name__ == "__main__":
    assert "sk-ant" not in scrub_text("key=sk-ant-abcdef0123456789ABCDEF here")
    assert scrub_text("ANTHROPIC_API_KEY=abcdef12345678").endswith(REDACTED)
    assert EMAIL_MARK in scrub_text("contact me at a.user@example.com please")
    assert "[USER]" in scrub_text(r"path C:\Users\Tyler\Desktop\x.py")
    assert "[USER]" in scrub_text("/home/tyler/secret/x.py")
    assert scrub_obj({"a": ["bearer abcdef0123456789xyz", 5, None]})["a"][0] == REDACTED
    assert scrub_obj(5) == 5 and scrub_obj(None) is None
    assert contains_secret("token: AIza0123456789012345678901234567890")
    assert not contains_secret("just a normal sentence about code")
    print("secret_scrub self-test passed (ok)")
