"""Asset generation worker — calls DALL-E 3 to create image files for pipeline tasks."""
from __future__ import annotations
import base64
import os
from pathlib import Path
from rich.console import Console

from config import OPENAI_API_KEY, ASSET_PROVIDER, ASSET_MODEL

console = Console()


def can_generate() -> bool:
    """True when asset generation is configured and an API key is available."""
    return ASSET_PROVIDER != "none" and bool(OPENAI_API_KEY)


def generate_assets(task, spec: dict, output_dir: Path) -> list[str]:
    """
    Generate image assets for an asset task. Returns list of written file paths.
    Task must have type='asset' and files listing .png/.jpg/.webp paths to create.
    Each file's description comes from the task objective.
    """
    if not can_generate():
        console.print(
            "  [yellow]Asset generation skipped — set OPENAI_API_KEY and ASSET_PROVIDER=dalle "
            "in .env to enable.[/yellow]"
        )
        return _write_placeholder_svgs(task, output_dir)

    try:
        from openai import OpenAI
    except ImportError:
        console.print("  [yellow]openai package not installed — pip install openai to enable asset generation.[/yellow]")
        return _write_placeholder_svgs(task, output_dir)

    client = OpenAI(api_key=OPENAI_API_KEY)
    written: list[str] = []

    goal = spec.get("goal", "game asset")
    style_hint = _extract_style(spec)

    for file_path in task.files:
        ext = Path(file_path).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            continue

        asset_name = Path(file_path).stem.replace("_", " ").replace("-", " ")
        prompt = (
            f"{task.objective}. "
            f"Asset: {asset_name} for a {goal}. "
            f"{style_hint} "
            "Transparent background if possible. Clean game art style."
        )

        console.print(f"  [dim]Generating asset: {file_path}…[/dim]")
        try:
            response = client.images.generate(
                model=ASSET_MODEL,
                prompt=prompt[:1000],
                n=1,
                size="1024x1024",
                response_format="b64_json",
            )
            image_data = base64.b64decode(response.data[0].b64_json)
            dest = output_dir / file_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(image_data)
            console.print(f"  [green]✓ Generated: {file_path}[/green]")
            written.append(file_path)
        except Exception as exc:
            console.print(f"  [yellow]Asset generation failed for {file_path}: {exc} — using placeholder.[/yellow]")
            written.extend(_write_placeholder_svgs_for(file_path, output_dir))

    return written


def _extract_style(spec: dict) -> str:
    """Pull style hints from spec constraints/features for better DALL-E prompts."""
    hints = []
    for item in spec.get("constraints", []) + spec.get("features", []):
        item_lower = item.lower()
        for keyword in ("pixel", "cartoon", "realistic", "flat", "minimalist", "retro", "neon", "dark", "bright"):
            if keyword in item_lower:
                hints.append(keyword + " art style")
                break
    return ", ".join(hints) if hints else "clean 2D game art style"


def _write_placeholder_svgs(task, output_dir: Path) -> list[str]:
    written = []
    for file_path in task.files:
        if Path(file_path).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
            written.extend(_write_placeholder_svgs_for(file_path, output_dir))
    return written


def _write_placeholder_svgs_for(file_path: str, output_dir: Path) -> list[str]:
    """Write a simple colored SVG placeholder when image generation is unavailable."""
    colors = ["#6366f1", "#10b981", "#f59e0b", "#ef4444", "#3b82f6", "#8b5cf6"]
    color = colors[hash(file_path) % len(colors)]
    name = Path(file_path).stem[:12]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
        f'<rect width="64" height="64" rx="8" fill="{color}"/>'
        f'<text x="32" y="38" font-size="10" fill="white" text-anchor="middle" font-family="sans-serif">{name}</text>'
        f'</svg>'
    )
    # Write as .svg alongside the requested path
    svg_path = Path(file_path).with_suffix(".svg")
    dest = output_dir / svg_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(svg, encoding="utf-8")
    return [str(svg_path)]
