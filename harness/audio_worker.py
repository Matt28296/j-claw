"""Audio generation worker — uses local Coqui TTS REST API.

Requires a running Coqui TTS server at COQUI_API_URL (default http://localhost:5002).
Falls back to silent WAV placeholders when TTS is not running.
No cloud API keys needed — fully local.
"""
from __future__ import annotations
import json
import os
import struct
import urllib.request
import urllib.error
from pathlib import Path
from rich.console import Console

from config import COQUI_API_URL

console = Console()

_TTS_PATH = "/api/tts"


def can_generate() -> bool:
    """True when Coqui TTS server is reachable."""
    try:
        urllib.request.urlopen(f"{COQUI_API_URL}{_TTS_PATH}", timeout=2)
        return True
    except Exception:
        return False


def generate_audio(task, spec: dict, output_dir: Path) -> list[str]:
    """
    Generate audio files for an audio task via local Coqui TTS.
    Returns list of written file paths (may be silent WAV placeholders if TTS unavailable).
    """
    if not can_generate():
        console.print(
            f"  [yellow]Coqui TTS not reachable at {COQUI_API_URL} — writing silent WAV placeholders. "
            "Start Coqui TTS server and set COQUI_API_URL in .env to enable real audio generation.[/yellow]"
        )
        return _write_all_placeholders(task, output_dir)

    written: list[str] = []

    for file_path in task.files:
        ext = Path(file_path).suffix.lower()
        if ext not in (".wav", ".mp3", ".ogg"):
            continue

        stem = Path(file_path).stem.replace("_", " ").replace("-", " ")
        prompt = f"{task.objective}, {stem}"

        console.print(f"  [dim]Generating audio via Coqui TTS: {file_path}…[/dim]")
        dest = output_dir / file_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            payload = json.dumps({
                "text": prompt[:500],
                "speaker_id": "p267",
                "style_wav": "",
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{COQUI_API_URL}{_TTS_PATH}",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                audio_bytes = resp.read()

            dest.write_bytes(audio_bytes)
            console.print(f"  [green]✓ Generated: {file_path}[/green]")
            written.append(file_path)

        except Exception as exc:
            console.print(f"  [yellow]TTS generation failed for {file_path}: {exc} — using silent placeholder.[/yellow]")
            _write_silent_wav(dest)
            written.append(file_path)

    return written


def _write_all_placeholders(task, output_dir: Path) -> list[str]:
    written = []
    for file_path in task.files:
        if Path(file_path).suffix.lower() in (".wav", ".mp3", ".ogg"):
            dest = output_dir / file_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            _write_silent_wav(dest)
            written.append(file_path)
    return written


def _write_silent_wav(dest: Path) -> None:
    """Write a minimal valid 44-byte WAV header with 0 data bytes so audio players don't crash."""
    # WAV format: RIFF header (12 bytes) + fmt chunk (24 bytes) + data chunk (8 bytes) = 44 bytes
    # num_channels=1 (mono), sample_rate=22050, bits_per_sample=16, data_size=0
    num_channels = 1
    sample_rate = 22050
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = 0
    chunk_size = 36 + data_size  # total RIFF chunk size minus 8 bytes for "RIFF" + size field

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        chunk_size,
        b"WAVE",
        b"fmt ",
        16,            # fmt chunk size (PCM)
        1,             # audio format (PCM = 1)
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    dest.write_bytes(header)
