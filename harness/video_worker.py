from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console

console = Console()

_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi"}

# Minimal valid MP4 stub (ftyp + free + mdat boxes, ~40 bytes)
_MP4_STUB = (
    b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomavc1"
    b"\x00\x00\x00\x08free"
    b"\x00\x00\x00\x08mdat"
)


def can_generate() -> bool:
    return shutil.which("ffmpeg") is not None


def generate_video(task, spec: dict, output_dir: Path) -> list[Path]:
    written: list[Path] = []

    for rel_path, content in task.output_files.items():
        out = output_dir / rel_path
        out.parent.mkdir(parents=True, exist_ok=True)

        suffix = Path(rel_path).suffix.lower()
        if suffix not in _VIDEO_EXTS:
            out.write_text(content, encoding="utf-8")
            written.append(out)
            continue

        # It's a video file — try to extract an ffmpeg command from the content
        cmd_line: str | None = None
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("ffmpeg "):
                cmd_line = stripped
                break

        success = False
        if cmd_line and can_generate():
            # Replace the last token (output path) with the real destination
            parts = cmd_line.split()
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
