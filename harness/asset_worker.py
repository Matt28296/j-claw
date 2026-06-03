"""Asset generation worker — uses local Stable Diffusion WebUI API (AUTOMATIC1111/Forge/ComfyUI).

Requires a running SD WebUI instance at SD_API_URL (default http://localhost:7860).
Falls back to SVG color-block placeholders when SD is not running.
No cloud API keys needed — fully local.
"""
from __future__ import annotations
import base64
import urllib.request
import urllib.error
import json
from pathlib import Path
from rich.console import Console

from config import SD_API_URL, ASSET_PROVIDER

console = Console()

_TXT2IMG_PATH = "/sdapi/v1/txt2img"


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
            f"  [yellow]SD WebUI not reachable at {SD_API_URL} — writing SVG placeholders. "
            "Start AUTOMATIC1111/Forge and set SD_API_URL in .env to enable real asset generation.[/yellow]"
        )
        return _write_placeholder_svgs(task, output_dir)

    goal = spec.get("goal", "game asset")
    style_hint = _extract_style(spec)
    written: list[str] = []

    for file_path in task.files:
        ext = Path(file_path).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            continue

        asset_name = Path(file_path).stem.replace("_", " ").replace("-", " ")
        prompt = (
            f"{task.objective}, {asset_name} for a {goal}, "
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
            written.extend(_write_placeholder_svgs_for(file_path, output_dir))

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


def _write_placeholder_svgs(task, output_dir: Path) -> list[str]:
    written = []
    for file_path in task.files:
        if Path(file_path).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
            written.extend(_write_placeholder_svgs_for(file_path, output_dir))
    return written


def _write_placeholder_svgs_for(file_path: str, output_dir: Path) -> list[str]:
    """Write a simple colored SVG placeholder when SD is unavailable."""
    colors = ["#6366f1", "#10b981", "#f59e0b", "#ef4444", "#3b82f6", "#8b5cf6"]
    color = colors[hash(file_path) % len(colors)]
    name = Path(file_path).stem[:12]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
        f'<rect width="64" height="64" rx="8" fill="{color}"/>'
        f'<text x="32" y="38" font-size="10" fill="white" text-anchor="middle" font-family="sans-serif">{name}</text>'
        f'</svg>'
    )
    svg_path = Path(file_path).with_suffix(".svg")
    dest = output_dir / svg_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(svg, encoding="utf-8")
    return [str(svg_path)]
