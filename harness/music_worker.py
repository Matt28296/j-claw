"""Music generation worker for J-Claw.

Detects a real MusicGen backend (the `audiocraft` package if importable, or an
external MusicGen HTTP endpoint via the MUSICGEN_API_URL env var) and reports
availability accordingly. When no backend is present it gracefully falls back to
a silent-WAV placeholder so the pipeline still produces a real file.
"""
from __future__ import annotations
import importlib.util
import logging
import os
import re
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 44100
_CHANNELS = 1
_SAMPLE_WIDTH = 2  # bytes (16-bit)

# Optional external MusicGen HTTP endpoint (e.g. a local Gradio/FastAPI server).
_MUSICGEN_API_URL = os.getenv("MUSICGEN_API_URL", "").strip()


def can_generate() -> bool:
    """Return True when a real music-generation backend is available.

    A backend counts as available when either:
      * the `audiocraft` package (Meta MusicGen) is importable, OR
      * a MUSICGEN_API_URL env var points at an external MusicGen HTTP endpoint.

    The backend does NOT need to be installed for the worker to function — when
    neither is present this returns False and generate_music() falls back to a
    silent-WAV placeholder.
    """
    if _MUSICGEN_API_URL:
        return True
    # importlib.util.find_spec does not import the (heavy) package — it only
    # checks that it is installed/importable.
    try:
        return importlib.util.find_spec("audiocraft") is not None
    except (ImportError, ValueError):
        return False


def generate_music(task, spec: dict, output_dir: Path) -> list[Path]:
    """Write silent WAV placeholder files for each audio output in task.files.

    Parses a duration (seconds or minutes) from task.objective; defaults to 30 s.
    Only processes files whose suffix is .wav, .mp3, or .ogg — non-audio filenames
    in task.files are silently skipped.

    Returns a list of Paths to the written files, one line logged per file.
    """
    duration_seconds = _parse_duration(getattr(task, "objective", ""))
    written: list[Path] = []
    audio_exts = {".wav", ".mp3", ".ogg"}

    prompt = getattr(task, "objective", "") or "instrumental background music"

    for filename in getattr(task, "files", []):
        p = Path(filename)
        if p.suffix.lower() not in audio_exts:
            continue
        dest = output_dir / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)

        generated = False
        if _MUSICGEN_API_URL:
            generated = _try_http_musicgen(dest, prompt, duration_seconds)

        if not generated:
            _write_silent_wav(dest, duration_seconds)
            logger.info("music_worker: wrote %s (%ds silent placeholder)", dest, duration_seconds)
        written.append(dest)

    return written


def _try_http_musicgen(dest: Path, prompt: str, duration_seconds: int) -> bool:
    """Request audio bytes from an external MusicGen HTTP endpoint.

    Returns True on success (bytes written to dest), False on any failure so the
    caller can fall back to the silent-WAV placeholder. Never raises.
    """
    import json
    import urllib.request

    payload = json.dumps({
        "prompt": prompt[:500],
        "duration": duration_seconds,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            _MUSICGEN_API_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            audio_bytes = resp.read()
        if not audio_bytes:
            return False
        dest.write_bytes(audio_bytes)
        logger.info("music_worker: wrote %s via MusicGen endpoint (%ds)", dest, duration_seconds)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("music_worker: MusicGen endpoint failed (%s) — using placeholder", exc)
        return False


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_duration(text: str) -> int:
    """Extract a duration in seconds from a free-form string.

    Recognises patterns like "30 second", "2 minute", "90-second", "1.5 minutes".
    Returns 30 if nothing is found.
    """
    text_lower = text.lower()

    # Minutes first so "2 minute 30 second" doesn't prematurely match seconds
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-\s]?minute", text_lower)
    if m:
        return max(1, int(round(float(m.group(1)) * 60)))

    m = re.search(r"(\d+(?:\.\d+)?)\s*[-\s]?second", text_lower)
    if m:
        return max(1, int(round(float(m.group(1)))))

    return 30


def _write_silent_wav(path: Path, duration_seconds: int) -> None:
    """Write a mono 16-bit 44100 Hz silent WAV file of the given duration."""
    n_frames = _SAMPLE_RATE * duration_seconds
    silent_data = bytes(n_frames * _CHANNELS * _SAMPLE_WIDTH)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(_CHANNELS)
        wf.setsampwidth(_SAMPLE_WIDTH)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(silent_data)
