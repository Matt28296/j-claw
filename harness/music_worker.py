"""Music generation worker for J-Claw.

Placeholder implementation using the standard-library wave module.
Set can_generate() to True and swap generate_music() body when MusicGen is available.
"""
from __future__ import annotations
import logging
import re
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 44100
_CHANNELS = 1
_SAMPLE_WIDTH = 2  # bytes (16-bit)


def can_generate() -> bool:
    """Return True when a real music-generation backend is available.

    Currently always False — this is a placeholder implementation.
    Set to True and replace generate_music() body when MusicGen is installed.
    """
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

    for filename in getattr(task, "files", []):
        p = Path(filename)
        if p.suffix.lower() not in audio_exts:
            continue
        dest = output_dir / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_silent_wav(dest, duration_seconds)
        logger.info("music_worker: wrote %s (%ds silent placeholder)", dest, duration_seconds)
        written.append(dest)

    return written


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
