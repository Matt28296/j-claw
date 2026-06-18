"""Music generation worker — algorithmic MIDI + FluidSynth rendering.

Generates genre-appropriate MIDI from the creative brief's tone/mood,
then renders to WAV using FluidSynth + the FluidR3_GM soundfont.
No ML, no GPU, no internet required.

Genre dispatch (from creative_brief.genre / tone / keywords):
  jazz / noir / detective → walking bass + piano chord comping (General MIDI)
  horror / dark / tense   → low strings tremolo + dissonant pad
  epic / action / battle  → brass fanfare + driving percussion
  romance / gentle        → strings + soft piano melody
  default                 → ambient pad + soft piano

Config (harness/.env):
  FLUIDSYNTH_BINARY    — path to fluidsynth.exe
  FLUIDSYNTH_SOUNDFONT — path to .sf2 soundfont
"""
from __future__ import annotations
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Import lazily so the module loads even if midiutil is absent
try:
    from midiutil import MIDIFile
    _MIDI_OK = True
except ImportError:
    _MIDI_OK = False

from config import FLUIDSYNTH_BINARY, FLUIDSYNTH_SOUNDFONT

_SAMPLE_RATE = 44100
_DEFAULT_DURATION = 30  # seconds


# ── General MIDI program numbers ──────────────────────────────────────────────
_GM = {
    "acoustic_grand_piano": 0,
    "bright_acoustic_piano": 1,
    "electric_piano": 4,
    "jazz_organ": 17,
    "strings": 48,       # String Ensemble 1
    "slow_strings": 49,
    "synth_strings": 50,
    "brass": 61,         # Brass Section
    "french_horn": 60,
    "clarinet": 71,
    "saxophone": 66,     # Tenor Sax
    "double_bass": 43,
    "pad_new_age": 88,
    "pad_choir": 91,
    "pad_atmosphere": 99,
    "tremolo_strings": 44,
    "orchestra_hit": 55,
}

# Drum channel (General MIDI always channel 9)
_DRUM_CH = 9
_KICK = 36
_SNARE = 38
_HIHAT_CLOSED = 42
_HIHAT_OPEN = 46
_CRASH = 49
_RIDE = 51


def can_generate() -> bool:
    return (
        _MIDI_OK
        and Path(FLUIDSYNTH_BINARY).exists()
        and Path(FLUIDSYNTH_SOUNDFONT).exists()
    )


def generate_music(task, spec: dict, output_dir: Path) -> list[Path]:
    """Generate music for a music/audio task.

    Returns a list of written Paths. Falls back to a silent WAV when
    FluidSynth or the soundfont is unavailable.
    """
    duration = _parse_duration(getattr(task, "objective", ""))
    genre = _detect_genre(spec)
    written: list[Path] = []
    audio_exts = {".wav", ".mp3", ".ogg"}

    for filename in getattr(task, "files", []):
        p = Path(filename)
        if p.suffix.lower() not in audio_exts:
            continue
        dest = output_dir / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)

        generated = False
        if can_generate():
            try:
                generated = _render_music(genre, duration, dest)
            except Exception as exc:
                logger.warning("music_worker: render failed (%s) — using silent placeholder", exc)

        if not generated:
            _write_silent_wav(dest, duration)
            logger.info("music_worker: wrote silent placeholder %s (%ds)", dest.name, duration)

        written.append(dest)

    return written


# ── Genre detection ───────────────────────────────────────────────────────────

def _detect_genre(spec: dict) -> str:
    brief = spec.get("creative_brief", {}) if spec else {}
    text = " ".join([
        str(brief.get("genre", "")),
        str(brief.get("tone", "")),
        str(brief.get("mood", "")),
        str(spec.get("goal", "")),
        str(spec.get("description", "")),
    ]).lower()

    if any(w in text for w in ("jazz", "noir", "detective", "1940", "chicago", "smoky", "club")):
        return "jazz"
    if any(w in text for w in ("horror", "scary", "dark", "tense", "suspense", "thriller")):
        return "horror"
    if any(w in text for w in ("epic", "action", "battle", "war", "adventure", "hero")):
        return "epic"
    if any(w in text for w in ("romance", "love", "gentle", "soft", "tender", "peaceful")):
        return "romance"
    return "ambient"


# ── MIDI composition ──────────────────────────────────────────────────────────

def _render_music(genre: str, duration: int, dest: Path) -> bool:
    """Build a MIDI file, render with FluidSynth, write to dest. Returns True on success."""
    midi = _compose(genre, duration)
    with tempfile.NamedTemporaryFile(suffix=".mid", delete=False) as f:
        midi_path = f.name
        midi.writeFile(f)

    try:
        wav_path = str(dest) if dest.suffix.lower() == ".wav" else dest.with_suffix(".wav")
        cmd = [
            FLUIDSYNTH_BINARY,
            "-ni",                        # no interactive, no user interface
            "-F", wav_path,               # output file (must precede soundfont)
            "-r", str(_SAMPLE_RATE),      # sample rate (must precede soundfont)
            "-q",                         # quiet — suppress info messages
            FLUIDSYNTH_SOUNDFONT,
            midi_path,
        ]
        from permissions import observe
        observe("render", detail="fluidsynth MIDI→WAV synthesis")  # roadmap #6: observe-only
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"fluidsynth exited {result.returncode}: {stderr}")

        if dest.suffix.lower() != ".wav" and Path(wav_path).exists():
            Path(wav_path).rename(dest)
        return dest.exists() and dest.stat().st_size > 1000
    finally:
        try:
            os.unlink(midi_path)
        except OSError:
            pass


def _compose(genre: str, duration: int) -> "MIDIFile":
    """Return a MIDIFile object for the given genre and duration (seconds)."""
    bpm = {"jazz": 120, "horror": 60, "epic": 140, "romance": 72, "ambient": 80}.get(genre, 90)
    beats = int(duration * bpm / 60)

    composers = {
        "jazz":    _compose_jazz,
        "horror":  _compose_horror,
        "epic":    _compose_epic,
        "romance": _compose_romance,
        "ambient": _compose_ambient,
    }
    return composers.get(genre, _compose_ambient)(beats, bpm)


def _compose_jazz(beats: int, bpm: int) -> "MIDIFile":
    """Walking bass + piano chords in C minor (jazz/noir style)."""
    midi = MIDIFile(2)
    midi.addTempo(0, 0, bpm)
    midi.addTempo(1, 0, bpm)

    bass_ch, piano_ch = 0, 1
    midi.addProgramChange(0, bass_ch, 0, _GM["double_bass"])
    midi.addProgramChange(1, piano_ch, 0, _GM["acoustic_grand_piano"])

    # Walking bass line in C minor (C2 D2 Eb2 G2)
    walk = [36, 38, 39, 43, 36, 38, 39, 43]  # MIDI notes
    for beat in range(beats):
        note = walk[beat % len(walk)]
        midi.addNote(0, bass_ch, note, beat, 1, 80)

    # Piano: sparse minor 7th chords on beats 1 and 3
    # Cm7 = C3 Eb3 G3 Bb3
    chord = [48, 51, 55, 58]
    for beat in range(0, beats, 2):
        for n in chord:
            midi.addNote(1, piano_ch, n, beat, 1.8, 60)

    return midi


def _compose_horror(beats: int, bpm: int) -> "MIDIFile":
    """Low tremolo strings + dissonant cluster for tension."""
    midi = MIDIFile(2)
    midi.addTempo(0, 0, bpm)
    midi.addTempo(1, 0, bpm)

    midi.addProgramChange(0, 0, 0, _GM["tremolo_strings"])
    midi.addProgramChange(1, 1, 0, _GM["pad_atmosphere"])

    # Low sustained cluster: C2 C#2 D2
    for beat in range(0, beats, 4):
        for n in [36, 37, 38]:
            midi.addNote(0, 0, n, beat, 4, 70)
    # Pad swell every 8 beats
    for beat in range(0, beats, 8):
        for n in [48, 51, 54]:  # Cm cluster
            midi.addNote(1, 1, n, beat, 7, 50)

    return midi


def _compose_epic(beats: int, bpm: int) -> "MIDIFile":
    """Brass fanfare + driving snare."""
    midi = MIDIFile(3)
    for t in range(3):
        midi.addTempo(t, 0, bpm)

    midi.addProgramChange(0, 0, 0, _GM["brass"])
    midi.addProgramChange(1, 1, 0, _GM["strings"])
    # Channel 9 = drums (no program change needed)

    # Brass: C major arpeggio pattern
    arp = [60, 64, 67, 72, 67, 64]
    for beat in range(beats):
        note = arp[beat % len(arp)]
        midi.addNote(0, 0, note, beat, 0.9, 90)

    # Strings: sustained chords
    for beat in range(0, beats, 4):
        for n in [48, 52, 55]:  # C major
            midi.addNote(1, 1, n, beat, 3.8, 65)

    # Drums: kick on 1 & 3, snare on 2 & 4
    for beat in range(beats):
        if beat % 4 in (0, 2):
            midi.addNote(2, _DRUM_CH, _KICK, beat, 0.5, 100)
        if beat % 4 in (1, 3):
            midi.addNote(2, _DRUM_CH, _SNARE, beat, 0.5, 85)
        midi.addNote(2, _DRUM_CH, _HIHAT_CLOSED, beat, 0.25, 60)

    return midi


def _compose_romance(beats: int, bpm: int) -> "MIDIFile":
    """Strings + soft piano melody in C major."""
    midi = MIDIFile(2)
    for t in range(2):
        midi.addTempo(t, 0, bpm)

    midi.addProgramChange(0, 0, 0, _GM["strings"])
    midi.addProgramChange(1, 1, 0, _GM["bright_acoustic_piano"])

    # Strings: sustained C major chord progression C–Am–F–G
    prog = [[48, 52, 55], [45, 48, 52], [41, 45, 48], [43, 47, 50]]
    for i, beat in enumerate(range(0, beats, 4)):
        chord = prog[i % len(prog)]
        for n in chord:
            midi.addNote(0, 0, n, beat, 3.8, 60)

    # Piano: simple stepwise melody C D E G A
    melody = [60, 62, 64, 67, 69, 67, 64, 62]
    for i, beat in enumerate(range(0, beats, 1)):
        midi.addNote(1, 1, melody[i % len(melody)], beat, 0.9, 70)

    return midi


def _compose_ambient(beats: int, bpm: int) -> "MIDIFile":
    """Slow pad swells + occasional piano note."""
    midi = MIDIFile(2)
    for t in range(2):
        midi.addTempo(t, 0, bpm)

    midi.addProgramChange(0, 0, 0, _GM["pad_new_age"])
    midi.addProgramChange(1, 1, 0, _GM["electric_piano"])

    # Pad: long sustained notes
    for beat in range(0, beats, 8):
        for n in [48, 52, 55, 60]:
            midi.addNote(0, 0, n, beat, 7.5, 50)

    # Sparse piano
    sparse = [0, 3, 5, 12, 16, 20]
    for offset in sparse:
        if offset < beats:
            midi.addNote(1, 1, 60 + (offset % 7), offset, 1.5, 55)

    return midi


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_duration(text: str) -> int:
    text_lower = text.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-\s]?minute", text_lower)
    if m:
        return max(1, int(round(float(m.group(1)) * 60)))
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-\s]?second", text_lower)
    if m:
        return max(1, int(round(float(m.group(1)))))
    return _DEFAULT_DURATION


def _write_silent_wav(path: Path, duration_seconds: int = 30) -> None:
    import wave
    n_frames = _SAMPLE_RATE * duration_seconds
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(bytes(n_frames * 2))
