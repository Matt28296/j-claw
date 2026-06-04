"""Asset generation worker — uses local Stable Diffusion WebUI API (AUTOMATIC1111/Forge/ComfyUI).

Requires a running SD WebUI instance at SD_API_URL (default http://localhost:7860).
Falls back to SVG color-block placeholders when SD is not running.
No cloud API keys needed — fully local.
"""
from __future__ import annotations
import base64
import struct
import zlib
import urllib.request
import urllib.error
import json
from pathlib import Path
from rich.console import Console

from config import SD_API_URL, ASSET_PROVIDER

console = Console()

_TXT2IMG_PATH = "/sdapi/v1/txt2img"

# Image/asset extensions this worker handles. Tasks producing only these are routed here
# (see scheduler._is_asset_task) so the code worker never has to emit binary base64 in JSON.
_ASSET_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".bmp", ".svg"}
_RASTER_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
# Deterministic placeholder palette (RGB), picked by filename hash for variety.
_PLACEHOLDER_COLORS = [
    (99, 102, 241), (16, 185, 129), (245, 158, 11),
    (239, 68, 68), (59, 130, 246), (139, 92, 246),
]


def can_generate() -> bool:
    """True when SD WebUI is reachable and asset generation is not disabled."""
    if ASSET_PROVIDER == "none":
        return False
    try:
        urllib.request.urlopen(f"{SD_API_URL}/sdapi/v1/sd-models", timeout=2)
        return True
    except Exception:
        return False


def generate_assets(task, spec: dict, output_dir: Path) -> list[str]:
    """
    Generate image assets for an asset task via local SD WebUI.
    Returns list of written file paths (may be .svg placeholders if SD unavailable).
    """
    if not can_generate():
        console.print(
            f"  [yellow]SD WebUI not reachable at {SD_API_URL} — writing valid placeholder assets. "
            "Start AUTOMATIC1111/Forge and set SD_API_URL in .env to enable real asset generation.[/yellow]"
        )
        return _write_placeholders(task, output_dir)

    goal = spec.get("goal", "game asset")
    style_hint = _extract_style(spec)
    written: list[str] = []

    for file_path in task.files:
        ext = Path(file_path).suffix.lower()
        if ext not in _ASSET_EXTS:
            continue
        if ext not in _RASTER_EXTS:
            # SD only emits raster images; .svg/.ico/.gif/.bmp get a deterministic placeholder.
            written.extend(_write_placeholder_for(file_path, output_dir))
            continue

        asset_name = Path(file_path).stem.replace("_", " ").replace("-", " ")
        brief = spec.get("creative_brief", {}) if spec else {}
        visual = brief.get("visual_identity", {})
        style = visual.get("style", "")
        palette = visual.get("palette", "")
        prompt = task.objective
        if style: prompt = prompt + ", " + style
        if palette: prompt = prompt + ", " + palette + " color palette"
        prompt = (
            f"{prompt}, {asset_name} for a {goal}, "
            f"{style_hint}, transparent background, clean edges, no text"
        )
        negative = "blurry, low quality, distorted, text, watermark, signature, background clutter"

        console.print(f"  [dim]Generating asset via SD WebUI: {file_path}…[/dim]")
        try:
            payload = json.dumps({
                "prompt": prompt[:800],
                "negative_prompt": negative,
                "steps": 20,
                "width": 512,
                "height": 512,
                "cfg_scale": 7,
                "sampler_name": "DPM++ 2M Karras",
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{SD_API_URL}{_TXT2IMG_PATH}",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            image_data = base64.b64decode(data["images"][0])
            dest = output_dir / file_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(image_data)
            console.print(f"  [green]✓ Generated: {file_path}[/green]")
            written.append(file_path)

        except Exception as exc:
            console.print(f"  [yellow]SD generation failed for {file_path}: {exc} — using placeholder.[/yellow]")
            written.extend(_write_placeholder_for(file_path, output_dir))

    return written


def _extract_style(spec: dict) -> str:
    """Pull style keywords from spec for better SD prompts."""
    hints = []
    for item in spec.get("constraints", []) + spec.get("features", []):
        item_lower = item.lower()
        for keyword in ("pixel art", "cartoon", "realistic", "flat design", "minimalist", "retro", "neon", "dark", "bright"):
            if keyword in item_lower:
                hints.append(keyword)
                break
    return ", ".join(hints) if hints else "clean 2D game art style, flat shading"


def _make_solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Build a valid solid-color truecolor PNG using only the stdlib.

    Deterministic and always-valid — replaces the local model trying (and reliably failing)
    to emit base64 PNG bytes inside a JSON content field.
    """
    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    r, g, b = rgb
    raw = (b"\x00" + bytes((r, g, b)) * width) * height  # filter byte 0 per scanline + RGB pixels
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def _write_placeholder_for(file_path: str, output_dir: Path) -> list[str]:
    """Write a valid placeholder at the EXACT requested path so references resolve (no 404).

    Raster extensions get a real solid-color PNG written under their own name; .svg gets a
    colored SVG block. Other extensions fall back to a PNG (valid bytes, harmless).
    """
    ext = Path(file_path).suffix.lower()
    color = _PLACEHOLDER_COLORS[hash(file_path) % len(_PLACEHOLDER_COLORS)]
    dest = output_dir / file_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if ext == ".svg":
        name = Path(file_path).stem[:12]
        hexc = "#%02x%02x%02x" % color
        dest.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256">'
            f'<rect width="256" height="256" rx="24" fill="{hexc}"/>'
            f'<text x="128" y="140" font-size="28" fill="white" text-anchor="middle" '
            f'font-family="sans-serif">{name}</text></svg>',
            encoding="utf-8",
        )
    else:
        dest.write_bytes(_make_solid_png(256, 256, color))
    return [str(Path(file_path))]


def _write_placeholders(task, output_dir: Path) -> list[str]:
    """Write valid placeholders for every asset file a task declares (used when SD is off)."""
    written: list[str] = []
    for file_path in task.files:
        if Path(file_path).suffix.lower() in _ASSET_EXTS:
            written.extend(_write_placeholder_for(file_path, output_dir))
    return written
