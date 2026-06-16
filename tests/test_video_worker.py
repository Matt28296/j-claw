"""Unit tests for harness/video_worker.py render binding and cwd handling.

These cover the regressions that caused factory rehearsal test #4 (the noir
film) to fail repeatedly: ffmpeg run from the wrong directory, the wrong
ffmpeg line selected for a multi-output scene, multi-line commands dropped,
and a trailing flag clobbered by the output-token replacement.

A fake subprocess.run records the args/cwd instead of encoding, so no real
ffmpeg is needed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import video_worker  # noqa: E402


SPEC_FILM = {"stack": "film"}


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """Patch can_generate→True and capture each subprocess.run invocation."""
    calls: list[dict] = []

    def _fake_run(parts, **kwargs):
        calls.append({"parts": list(parts), "cwd": kwargs.get("cwd")})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video_worker, "can_generate", lambda: True)
    monkeypatch.setattr(video_worker.subprocess, "run", _fake_run)
    return calls


def _make_task(files, output_files=None):
    return SimpleNamespace(files=files, output_files=output_files or {})


def test_ffmpeg_runs_with_output_dir_as_cwd(tmp_path, fake_ffmpeg):
    """Relative input paths only resolve when cwd is the scene output dir."""
    (tmp_path / "render.sh").write_text(
        "ffmpeg -y -framerate 24 -i frames/%05d.png -pix_fmt yuv420p video/scene_raw.mp4\n",
        encoding="utf-8",
    )
    task = _make_task(["video/scene_raw.mp4"])

    written, failures = video_worker.generate_video(task, SPEC_FILM, tmp_path)

    assert not failures
    assert len(fake_ffmpeg) == 1
    assert fake_ffmpeg[0]["cwd"] == str(tmp_path.resolve())
    # Input stayed relative; harness supplies cwd so it resolves.
    assert "frames/%05d.png" in fake_ffmpeg[0]["parts"]


def test_multi_output_scene_selects_matching_command(tmp_path, fake_ffmpeg):
    """With two ffmpeg lines, the one whose output matches the declared file wins."""
    (tmp_path / "render.sh").write_text(
        "ffmpeg -y -i frames/a/%05d.png video/scene1_raw.mp4\n"
        "ffmpeg -y -i frames/b/%05d.png video/scene2_raw.mp4\n",
        encoding="utf-8",
    )
    task = _make_task(["video/scene2_raw.mp4"])

    written, failures = video_worker.generate_video(task, SPEC_FILM, tmp_path)

    assert not failures
    assert len(fake_ffmpeg) == 1
    # Must have chosen the scene2 command (its input), not the first line.
    assert "frames/b/%05d.png" in fake_ffmpeg[0]["parts"]


def test_multiline_continued_command_is_captured_whole(tmp_path, fake_ffmpeg):
    """A backslash-continued ffmpeg invocation is joined into one command."""
    (tmp_path / "render.sh").write_text(
        "ffmpeg -y \\\n"
        "  -framerate 24 \\\n"
        "  -i frames/%05d.png \\\n"
        "  -pix_fmt yuv420p video/scene_raw.mp4\n",
        encoding="utf-8",
    )
    task = _make_task(["video/scene_raw.mp4"])

    written, failures = video_worker.generate_video(task, SPEC_FILM, tmp_path)

    assert not failures
    parts = fake_ffmpeg[0]["parts"]
    # All flags from the continued lines survived.
    assert "-framerate" in parts and "24" in parts
    assert "-i" in parts and "frames/%05d.png" in parts


def test_output_token_replacement_does_not_clobber_trailing_flag(tmp_path, fake_ffmpeg):
    """When the last token is a flag/value (output set via -y earlier), append the
    destination instead of overwriting the flag."""
    # Output declared up front, last token is a filter value (not a path).
    (tmp_path / "render.sh").write_text(
        "ffmpeg -i frames/%05d.png video/scene_raw.mp4 -vf scale=1280:720\n",
        encoding="utf-8",
    )
    task = _make_task(["video/scene_raw.mp4"])

    written, failures = video_worker.generate_video(task, SPEC_FILM, tmp_path)

    parts = fake_ffmpeg[0]["parts"]
    # The scale filter value must survive untouched.
    assert "scale=1280:720" in parts
    # The real output path was appended, pointing at the scene dir.
    assert str((tmp_path / "video" / "scene_raw.mp4").resolve()) in parts


def test_missing_ffmpeg_line_is_a_film_failure(tmp_path, fake_ffmpeg):
    """A film deliverable with no ffmpeg command must fail, not write a stub."""
    (tmp_path / "notes.txt").write_text("no command here\n", encoding="utf-8")
    task = _make_task(["video/scene_raw.mp4"])

    written, failures = video_worker.generate_video(task, SPEC_FILM, tmp_path)

    assert "video/scene_raw.mp4" in failures
    assert not fake_ffmpeg  # ffmpeg never invoked


def _seed_frames(tmp_path, n=3):
    frames = tmp_path / "frames"
    frames.mkdir()
    for i in range(1, n + 1):
        (frames / f"scene1_frame_{i:04d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")


def test_synthetic_source_ignoring_real_frames_fails(tmp_path, fake_ffmpeg):
    """When real frames exist but the command renders a lavfi/color grey source,
    the render is failed so the heal loop rewrites it — not passed as grey video."""
    _seed_frames(tmp_path)
    (tmp_path / "render.sh").write_text(
        "ffmpeg -y -f lavfi -i color=c=0x1a1a1a:size=1280x720:rate=24:duration=7 "
        "-c:v libx264 -pix_fmt yuv420p video/scene_raw.mp4\n",
        encoding="utf-8",
    )
    task = _make_task(["video/scene_raw.mp4"])

    written, failures = video_worker.generate_video(task, SPEC_FILM, tmp_path)

    assert "video/scene_raw.mp4" in failures
    assert "synthetic" in failures["video/scene_raw.mp4"].lower()
    assert not fake_ffmpeg  # never ran the grey render


def test_real_frame_encode_with_synthetic_audio_bed_passes(tmp_path, fake_ffmpeg):
    """Encoding the real frames is fine even with a synthetic aevalsrc AUDIO bed."""
    _seed_frames(tmp_path)
    (tmp_path / "render.sh").write_text(
        "ffmpeg -y -framerate 24 -i frames/scene1_frame_%04d.png "
        "-f lavfi -i aevalsrc=0.05*sin(2*PI*110*t):d=7 "
        "-c:v libx264 -pix_fmt yuv420p -shortest video/scene_raw.mp4\n",
        encoding="utf-8",
    )
    task = _make_task(["video/scene_raw.mp4"])

    written, failures = video_worker.generate_video(task, SPEC_FILM, tmp_path)

    assert not failures
    assert len(fake_ffmpeg) == 1  # ran: uses real frames, audio bed allowed
