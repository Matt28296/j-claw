"""Audio generation worker — uses Piper TTS for local narration.

Piper is a pre-compiled binary (no Python package, no GPU required).
It runs at ~0.25x real-time on CPU: a 30-second narration clip generates
in ~8 seconds.

Falls back to silent WAV placeholders when the Piper binary is missing
or the voice model is not found.

Config (harness/.env or env vars):
  PIPER_BINARY   — path to piper.exe
  PIPER_VOICE    — path to the .onnx voice model
"""
from __future__ import annotations
import re
import struct
import subprocess
import wave
from pathlib import Path
from rich.console import Console

from config import PIPER_BINARY, PIPER_VOICE

console = Console()


def can_generate() -> bool:
    """True when the Piper binary and voice model are both present."""
    return Path(PIPER_BINARY).exists() and Path(PIPER_VOICE).exists()


def generate_audio(task, spec: dict, output_dir: Path) -> list[str]:
    """Generate audio files for an audio task via Piper TTS.

    Returns list of written file paths (may be silent WAV placeholders
    if Piper is unavailable).
    """
    if not can_generate():
        console.print(
            f"  [yellow]Piper TTS not found at {PIPER_BINARY} — writing silent WAV "
            "placeholders. Install Piper and set PIPER_BINARY / PIPER_VOICE in .env.[/yellow]"
        )
        return _write_all_placeholders(task, output_dir)

    written: list[str] = []

    for file_path in task.files:
        ext = Path(file_path).suffix.lower()
        if ext not in (".wav", ".mp3", ".ogg"):
            continue

        dest = output_dir / file_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        narration_text = _build_narration(task, spec)
        console.print(f"  [dim]Generating narration via Piper TTS: {file_path}…[/dim]")

        try:
            _piper_synthesize(narration_text, dest)
            console.print(f"  [green]✓ Generated: {file_path}[/green]")
            written.append(file_path)
        except Exception as exc:
            console.print(
                f"  [yellow]Piper TTS failed for {file_path}: {exc} — using silent placeholder.[/yellow]"
            )
            _write_silent_wav(dest)
            written.append(file_path)

    return written


def _piper_synthesize(text: str, dest: Path) -> None:
    """Run Piper TTS, writing a WAV to dest.  Piper reads text from stdin."""
    # Piper writes raw PCM to stdout when --output_file is omitted with
    # --output_raw, but using --output_file is simpler and more reliable.
    from permissions import observe
    observe("render", detail="piper TTS synthesis")  # roadmap #6: observe-only
    result = subprocess.run(
        [
            PIPER_BINARY,
            "--model", PIPER_VOICE,
            "--output_file", str(dest),
        ],
        input=text.encode("utf-8"),
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[-400:]
        raise RuntimeError(f"piper exited {result.returncode}: {stderr}")
    if not dest.exists() or dest.stat().st_size < 44:
        raise RuntimeError("piper produced no output file")


def _build_narration(task, spec: dict) -> str:
    """Build the narration text from the task objective and creative brief."""
    brief = spec.get("creative_brief", {}) if spec else {}

    # Prefer an explicit narration/voiceover field in the brief
    narration = (
        brief.get("narration")
        or brief.get("voiceover")
        or brief.get("dialogue")
        or ""
    )
    if narration and isinstance(narration, str):
        return narration.strip()

    # Fall back to the task objective, cleaned up as spoken prose
    text = task.objective or "Narrator speaks."
    # Strip technical noise: file paths, brackets, parentheses
    text = re.sub(r"\[.*?\]|\(.*?\)", "", text)
    text = re.sub(r"\S+\.\w{2,4}\b", "", text)  # strip filenames
    text = re.sub(r"\s+", " ", text).strip()
    return text or "Scene narrator."


def _write_all_placeholders(task, output_dir: Path) -> list[str]:
    written = []
    for file_path in task.files:
        if Path(file_path).suffix.lower() in (".wav", ".mp3", ".ogg"):
            dest = output_dir / file_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            _write_silent_wav(dest)
            written.append(file_path)
    return written


def _write_silent_wav(dest: Path, duration_seconds: int = 1) -> None:
    """Write a minimal silent mono WAV."""
    sample_rate = 22050
    n_frames = sample_rate * duration_seconds
    with wave.open(str(dest), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(n_frames * 2))
