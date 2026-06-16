"""Smoke tests for the local film media workers (Piper TTS + FluidSynth music).

These exercise the real local binaries when present and assert that the
generated audio is *real* (contains non-zero samples), not the silent-WAV
fallback. They skip cleanly on a host where the binaries/soundfont are not
installed, so the suite stays portable (e.g. CI without the E:\\tools stack).

Real-vs-silent is checked with the stdlib ``wave`` module — no ffmpeg needed.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harness"))

import audio_worker  # noqa: E402
import music_worker  # noqa: E402

# Silence rich console output so the success glyph (✓) can't trip a legacy
# cp1252 stdout when the suite runs in a non-UTF-8 terminal.
try:
    from rich.console import Console

    audio_worker.console = Console(file=io.StringIO())
except Exception:  # pragma: no cover - rich always present in this project
    pass


NOIR_SPEC = {
    "goal": "Make a 30 second noir film about a detective in 1940s Chicago",
    "creative_brief": {
        "genre": "noir",
        "tone": "smoky, tense",
        "narration": "The rain hammered the window as Detective Cole lit another "
        "cigarette. In this city, everyone had a secret.",
    },
}


def _wav_has_sound(path: Path) -> bool:
    """True if the WAV contains at least one non-zero PCM sample.

    The silent-placeholder fallback writes all-zero frames, so any non-zero
    byte proves real synthesized/rendered audio.
    """
    with wave.open(str(path), "rb") as wf:
        if wf.getnframes() == 0:
            return False
        frames = wf.readframes(wf.getnframes())
    return any(b != 0 for b in frames)


class GenreDetectionTests(unittest.TestCase):
    """Pure-function checks — no binaries required."""

    def test_noir_brief_detects_jazz(self) -> None:
        self.assertEqual(music_worker._detect_genre(NOIR_SPEC), "jazz")

    def test_horror_brief_detects_horror(self) -> None:
        self.assertEqual(
            music_worker._detect_genre({"creative_brief": {"tone": "scary, dark"}}),
            "horror",
        )

    def test_unknown_brief_defaults_ambient(self) -> None:
        self.assertEqual(
            music_worker._detect_genre({"creative_brief": {"tone": "neutral"}}),
            "ambient",
        )

    def test_duration_parsed_from_objective(self) -> None:
        self.assertEqual(music_worker._parse_duration("a 30 second score"), 30)
        self.assertEqual(music_worker._parse_duration("a 2 minute theme"), 120)


@unittest.skipUnless(
    audio_worker.can_generate(), "Piper binary/voice not installed on this host"
)
class PiperNarrationTests(unittest.TestCase):
    def test_generates_real_speech_wav(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            task = SimpleNamespace(files=["narration.wav"], objective="detective monologue")
            written = audio_worker.generate_audio(task, NOIR_SPEC, out)
            self.assertEqual(written, ["narration.wav"])
            wav = out / "narration.wav"
            self.assertTrue(wav.exists())
            # A 1s silent placeholder is ~44 KB; real narration is much larger.
            self.assertGreater(wav.stat().st_size, 100_000)
            self.assertTrue(_wav_has_sound(wav), "narration WAV is silent")


@unittest.skipUnless(
    music_worker.can_generate(), "FluidSynth binary/soundfont not installed on this host"
)
class FluidSynthMusicTests(unittest.TestCase):
    def test_generates_real_jazz_score(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            task = SimpleNamespace(files=["score.wav"], objective="30 second noir score")
            written = music_worker.generate_music(task, NOIR_SPEC, out)
            self.assertEqual(len(written), 1)
            wav = Path(written[0])
            self.assertTrue(wav.exists())
            self.assertGreater(wav.stat().st_size, 1_000_000)
            self.assertTrue(_wav_has_sound(wav), "music WAV is silent")


if __name__ == "__main__":
    unittest.main()
