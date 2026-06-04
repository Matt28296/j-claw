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


def generate_video(task, spec: dict, output_dir: Path) -> list[Path]:
    """Render the video outputs declared in task.files.

    For each declared video file we look for an ffmpeg command to drive the
    render. The command can come from:
      1. task.output_files[<file>] — inline content, if the code worker
         produced this task (rare for video tasks).
      2. Any ffmpeg edit-script / shot-list / manifest already on disk in
         output_dir (written by an upstream "film"/"video-editor" director
         task — see _STACK_PROMPTS in worker.py).
    When no command is found, or ffmpeg is unavailable, a graceful placeholder
    is written so downstream tasks and verification still have a real file.

    Mirrors music_worker/audio_worker by iterating task.files (NOT
    task.output_files, which is never populated for video tasks routed
    straight to this worker by the scheduler).
    """
    written: list[Path] = []

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
        if cmd_line and can_generate():
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
                        console.print(
                            f"  [yellow]ffmpeg exited {result.returncode} for {rel_path}; "
                            f"writing placeholder[/yellow]"
                        )
                except Exception as exc:  # noqa: BLE001
                    console.print(f"  [yellow]ffmpeg error ({exc}); writing placeholder[/yellow]")

        if not success:
            _write_placeholder(out)

        written.append(out)

    return written


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
