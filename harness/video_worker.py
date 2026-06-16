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

        # It's a video file — find the ffmpeg command that renders THIS output.
        cmd_line = _find_ffmpeg_command(inline.get(rel_path), disk_scripts, rel_path)

        success = False
        reason = ""
        if not can_generate():
            reason = "ffmpeg is not installed"
        elif not cmd_line:
            reason = (
                "no executable 'ffmpeg …' line found in task output or any edit "
                "script on disk — the director task must emit one (e.g. in render.sh)"
            )
        elif (
            video_is_deliverable
            and _scene_has_real_frames(output_dir)
            and _cmd_uses_synthetic_video(cmd_line)
            and not _cmd_uses_frames(cmd_line)
        ):
            # The scene generated real ComfyUI frames but this render command
            # synthesises a blank `lavfi`/`color=` source and ignores them — the
            # result passes ffprobe but is a grey placeholder, not the scene. Fail
            # so the heal loop rewrites the command to encode the actual frames.
            reason = (
                "render command uses a synthetic 'lavfi'/'color=' video source and "
                "ignores the generated frames in frames/ — encode the real frames "
                "instead (e.g. `-framerate <fps> -i frames/<pattern>.png`); do NOT "
                "substitute a solid color/lavfi background for the scene visuals"
            )
        else:
            try:
                parts = shlex.split(cmd_line)
            except ValueError:
                parts = cmd_line.split()
            if parts:
                # Point the command at the real destination. Only overwrite the
                # last token when it is actually the output path (matches the
                # declared basename or looks like a video file); otherwise the
                # script put the output elsewhere — append rather than clobber a
                # trailing flag/value.
                # Absolute output + absolute cwd so a relative output_dir can't
                # double-resolve (cwd/output_dir/cwd/output_dir/…).
                abs_out = out.resolve()
                abs_cwd = output_dir.resolve()
                if _is_output_token(parts[-1], rel_path):
                    parts[-1] = str(abs_out)
                else:
                    parts.append(str(abs_out))
                console.print(f"  [dim]video: running ffmpeg for {rel_path}[/dim]")
                try:
                    # Run from output_dir so the script's relative input paths
                    # (frames/%05d.png, audio/x.wav) resolve correctly. Running
                    # from the harness cwd was silently breaking every render.
                    result = subprocess.run(
                        parts,
                        cwd=str(abs_cwd),
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


def _scene_has_real_frames(output_dir: Path) -> bool:
    """True when the scene produced PNG frames meant to be encoded into the clip
    (ComfyUI output). Looks for pngs under a 'frames' dir or named like a frame."""
    if not output_dir.exists():
        return False
    for p in output_dir.rglob("*.png"):
        parts = {part.lower() for part in p.parts}
        if "frames" in parts or "frame" in p.stem.lower():
            return True
    return False


def _cmd_uses_synthetic_video(cmd_line: str) -> bool:
    """True when the ffmpeg command sources its VIDEO from a synthetic generator
    (`color=`/`lavfi … color`/`smptebars`/`testsrc`) rather than real frames.
    `aevalsrc` (synthetic AUDIO bed) does not count — audio beds are fine."""
    low = cmd_line.lower()
    return any(tok in low for tok in ("color=c", "color=s", "smptebars", "testsrc", "nullsrc"))


def _cmd_uses_frames(cmd_line: str) -> bool:
    """True when the ffmpeg command takes a frame-image sequence as input
    (a frames/ path, a printf glob like %04d, or an image2 glob)."""
    low = cmd_line.lower()
    return (
        "frames/" in low
        or "frame_" in low
        or "%0" in cmd_line  # printf sequence e.g. %04d / %05d
        or "-pattern_type glob" in low
        or "image2" in low
    )


def _is_output_token(token: str, rel_path: str) -> bool:
    """True when `token` looks like the render's output path for rel_path —
    either it ends with the declared basename, or it has a video extension (so a
    bare `out.mp4` still counts). Used to avoid clobbering a trailing flag."""
    base = Path(rel_path).name.lower()
    tok = token.strip().strip('"\'').replace("\\", "/").lower()
    if not tok or tok.startswith("-"):
        return False
    return tok.endswith(base) or Path(tok).suffix in _VIDEO_EXTS


def _join_continued_lines(content: str) -> list[str]:
    """Collapse shell/.cmd line continuations so a multi-line ffmpeg invocation
    is returned as one logical line. Handles POSIX `\\` and Windows `^`."""
    logical: list[str] = []
    buf = ""
    for raw in content.splitlines():
        line = raw.rstrip()
        if line.endswith("\\") or line.endswith("^"):
            buf += line[:-1] + " "
            continue
        buf += line
        logical.append(buf)
        buf = ""
    if buf:
        logical.append(buf)
    return logical


def _ffmpeg_lines(sources: list[str]) -> list[str]:
    """All 'ffmpeg …' logical command lines across the given sources, in order."""
    cmds: list[str] = []
    for content in sources:
        for line in _join_continued_lines(content):
            stripped = line.strip()
            if stripped.startswith("ffmpeg "):
                cmds.append(stripped)
    return cmds


def _find_ffmpeg_command(
    inline_content: str | None,
    disk_scripts: list[str],
    rel_path: str | None = None,
) -> str | None:
    """Return the ffmpeg command that renders `rel_path`.

    When a scene has multiple ffmpeg invocations (multiple outputs / a render +
    an edit script), prefer the one whose final token matches the declared
    output's basename. Only when nothing matches do we fall back to the first
    ffmpeg line — preserving the original single-output behaviour.
    """
    sources: list[str] = []
    if inline_content:
        sources.append(inline_content)
    sources.extend(disk_scripts)

    cmds = _ffmpeg_lines(sources)
    if not cmds:
        return None

    if rel_path:
        base = Path(rel_path).name.lower()
        for cmd in cmds:
            try:
                parts = shlex.split(cmd)
            except ValueError:
                parts = cmd.split()
            if parts and parts[-1].strip().strip('"\'').replace("\\", "/").lower().endswith(base):
                return cmd

    return cmds[0]


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
