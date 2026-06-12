from __future__ import annotations
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console

console = Console()

_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi"}
# Text artefacts a director task may emit to drive an ffmpeg render
# (edit scripts / shot lists / manifests).
_SCRIPT_EXTS = {".sh", ".txt", ".ffmpeg", ".cmd", ".json"}

# Minimal valid MP4 stub (ftyp + free + mdat boxes, ~40 bytes)
_MP4_STUB = (
    b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomavc1"
    b"\x00\x00\x00\x08free"
    b"\x00\x00\x00\x08mdat"
)


def can_generate() -> bool:
    return shutil.which("ffmpeg") is not None


def generate_video(task, spec: dict, output_dir: Path) -> tuple[list[Path], dict[str, str]]:
    """Render the video outputs declared in task.files.

    For each declared video file we look for an ffmpeg command to drive the
    render. The command can come from:
      1. task.output_files[<file>] — inline content, if the code worker
         produced this task (rare for video tasks).
      2. Any ffmpeg edit-script / shot-list / manifest already on disk in
         output_dir (written by an upstream "film"/"video-editor" director
         task — see _STACK_PROMPTS in worker.py).

    Returns (written, failures). For film/video-editor stacks a video that
    cannot actually be rendered is a FAILURE (rel_path → reason) — the video
    IS the deliverable, and a silent placeholder would pass ffprobe and report
    a hollow green build. For other stacks (e.g. a game wanting a cutscene
    file) a graceful placeholder is still written so downstream tasks and
    verification have a real file.

    Mirrors music_worker/audio_worker by iterating task.files (NOT
    task.output_files, which is never populated for video tasks routed
    straight to this worker by the scheduler).
    """
    written: list[Path] = []
    failures: dict[str, str] = {}
    from config import spec_stack
    video_is_deliverable = spec_stack(spec) in ("film", "video-editor")

    # Inline content the worker may have attached (keyed by relative path).
    inline = getattr(task, "output_files", {}) or {}

    # Edit scripts already on disk that may carry ffmpeg commands.
    disk_scripts = _collect_disk_scripts(output_dir)

    declared = list(getattr(task, "files", []) or [])
    # Fall back to inline keys if the task declared no files (legacy behaviour).
    if not declared and inline:
        declared = list(inline.keys())

    for rel_path in declared:
        out = output_dir / rel_path
        out.parent.mkdir(parents=True, exist_ok=True)

        suffix = Path(rel_path).suffix.lower()

        # Non-video declared file with inline content → write it verbatim
        # (e.g. an edit script / manifest the worker produced).
        if suffix not in _VIDEO_EXTS:
            if rel_path in inline:
                out.write_text(inline[rel_path], encoding="utf-8")
                written.append(out)
            continue

        # It's a video file — try to find an ffmpeg command to render it.
        cmd_line = _find_ffmpeg_command(inline.get(rel_path), disk_scripts)

        success = False
        reason = ""
        if not can_generate():
            reason = "ffmpeg is not installed"
        elif not cmd_line:
            reason = (
                "no executable 'ffmpeg …' line found in task output or any edit "
                "script on disk — the director task must emit one (e.g. in render.sh)"
            )
        else:
            # Replace the last token (output path) with the real destination.
            try:
                parts = shlex.split(cmd_line)
            except ValueError:
                parts = cmd_line.split()
            if parts:
                parts[-1] = str(out)
                console.print(f"  [dim]video: running ffmpeg for {rel_path}[/dim]")
                try:
                    result = subprocess.run(
                        parts,
                        timeout=180,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        success = True
                    else:
                        reason = (
                            f"ffmpeg exited {result.returncode}: "
                            f"{(result.stderr or '').strip()[-500:] or 'no stderr'}"
                        )
                except Exception as exc:  # noqa: BLE001
                    reason = f"ffmpeg error: {exc}"

        if success:
            written.append(out)
        elif video_is_deliverable:
            # Film/video-editor: never fake the deliverable — fail the task so the
            # EXECUTION_ERROR refinement loop gets a precise, actionable signal.
            console.print(f"  [red]video: render failed for {rel_path} — {reason[:200]}[/red]")
            failures[rel_path] = reason
        else:
            console.print(f"  [yellow]video: {reason[:120]}; writing placeholder[/yellow]")
            _write_placeholder(out)
            written.append(out)

    return written, failures


def assemble_film(scene_clips: list[Path], output_path: Path) -> tuple[bool, str]:
    """Concatenate scene clips (topological order) into one film.

    Tries the concat demuxer with stream copy first (fast, lossless); if the
    scenes' codec parameters don't match, falls back to a re-encode. Scenes
    from one pipeline run share the architect's resolution/codec settings, so
    the copy path is the common case.
    """
    if not scene_clips:
        return False, "assemble_film: no scene clips to assemble"
    if not can_generate():
        return False, "assemble_film: ffmpeg is not installed"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = output_path.parent / (output_path.stem + ".concat.txt")
    list_file.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in scene_clips),
        encoding="utf-8",
    )
    base = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file)]
    attempts = [
        (base + ["-c", "copy", str(output_path)], "stream copy"),
        (base + ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 "-movflags", "+faststart", str(output_path)], "re-encode"),
    ]
    try:
        last_err = ""
        for cmd, label in attempts:
            console.print(f"  [dim]video: assembling {len(scene_clips)} scene(s) ({label}) → {output_path.name}[/dim]")
            try:
                result = subprocess.run(cmd, timeout=600, capture_output=True, text=True)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                continue
            if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1024:
                return True, f"assembled {len(scene_clips)} clip(s) via concat ({label})"
            last_err = (result.stderr or "").strip()[-500:] or f"exit {result.returncode}"
        return False, f"assemble_film failed: {last_err}"
    finally:
        try:
            list_file.unlink()
        except OSError:
            pass


def _collect_disk_scripts(output_dir: Path) -> list[str]:
    """Read text edit-scripts/manifests already written to output_dir."""
    scripts: list[str] = []
    if not output_dir.exists():
        return scripts
    for p in sorted(output_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in _SCRIPT_EXTS:
            try:
                scripts.append(p.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    return scripts


def _find_ffmpeg_command(inline_content: str | None, disk_scripts: list[str]) -> str | None:
    """Extract the first 'ffmpeg ...' command line from any available source."""
    sources: list[str] = []
    if inline_content:
        sources.append(inline_content)
    sources.extend(disk_scripts)
    for content in sources:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("ffmpeg "):
                return stripped
    return None


def _write_placeholder(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if can_generate():
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "color=black:s=1280x720:rate=24:duration=1",
            "-vcodec", "libx264",
            "-pix_fmt", "yuv420p",
            str(path),
        ]
        console.print(f"  [dim]video: generating 1-second black placeholder → {path.name}[/dim]")
        try:
            subprocess.run(cmd, timeout=60, capture_output=True, check=True)
            return
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [yellow]Placeholder ffmpeg failed ({exc}); writing stub bytes[/yellow]")
    # ffmpeg unavailable or failed — write minimal MP4 stub
    path.write_bytes(_MP4_STUB)
    console.print(f"  [dim]video: wrote MP4 stub → {path.name}[/dim]")
