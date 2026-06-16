"""Asset generation worker.

Supports two backends:
  ASSET_PROVIDER=comfyui  — ComfyUI (port 8188, DirectML/CUDA/CPU)
  ASSET_PROVIDER=sd       — AUTOMATIC1111 / Forge (port 7860)
  ASSET_PROVIDER=none     — disabled; always write placeholders

Falls back to SVG/PNG color-block placeholders when the backend is
unreachable or generation fails, so the pipeline is never blocked.
"""
from __future__ import annotations
import base64
import json
import os
import random
import struct
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from rich.console import Console

from config import (
    SD_API_URL, ASSET_PROVIDER,
    COMFYUI_API_URL, COMFYUI_CHECKPOINT, COMFYUI_WIDTH, COMFYUI_HEIGHT,
    COMFYUI_CHECKPOINT_REALISTIC, COMFYUI_CHECKPOINT_ANIME,
    COMFYUI_STEPS, COMFYUI_SAMPLER, COMFYUI_SCHEDULER,
)

console = Console()

_A1111_TXT2IMG = "/sdapi/v1/txt2img"
_ASSET_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".bmp", ".svg"}
_RASTER_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_PLACEHOLDER_COLORS = [
    (99, 102, 241), (16, 185, 129), (245, 158, 11),
    (239, 68, 68), (59, 130, 246), (139, 92, 246),
]
_COMFYUI_POLL_INTERVAL = 4    # seconds between history polls
_COMFYUI_TIMEOUT_STEPS = 150  # 150 × 4s = 600s max per image

# Keywords that route image generation to the realistic vs anime checkpoint.
# Keep these lists free of words that appear in nearly every brief this (film)
# pipeline handles — e.g. "film"/"cinematic" describe the medium and shot
# framing, not the render style, so they must not outvote an explicit "anime"
# cue. Also avoid keywords that are substrings of another in the same list
# ("toon" ⊂ "cartoon"), which would double-count a single match.
_ANIME_KEYWORDS = (
    "anime", "cartoon", "manga", "cel shaded", "cel-shaded", "comic",
    "pixel art", "chibi", "illustrat", "hand-drawn", "stylized sketch",
)
_REALISTIC_KEYWORDS = (
    "realistic", "photoreal", "photo-real", "photograph", "photo ",
    "live action", "live-action", "documentary", "noir", "lifelike",
    "hyperreal", "8k", "4k", "dslr", "portrait photo",
)


def can_generate() -> bool:
    """True when the configured asset backend is reachable."""
    if ASSET_PROVIDER == "none":
        return False
    if ASSET_PROVIDER == "comfyui":
        try:
            urllib.request.urlopen(f"{COMFYUI_API_URL}/system_stats", timeout=2)
            return True
        except Exception:
            return False
    # sd (A1111/Forge)
    try:
        urllib.request.urlopen(f"{SD_API_URL}/sdapi/v1/sd-models", timeout=2)
        return True
    except Exception:
        return False


def generate_assets(task, spec: dict, output_dir: Path) -> list[str]:
    """Generate image assets for an asset task.

    Returns a list of written file paths (may contain placeholder paths when
    the backend is unavailable or generation fails for a specific file).
    """
    if not can_generate():
        backend = "ComfyUI" if ASSET_PROVIDER == "comfyui" else "SD WebUI"
        console.print(
            f"  [yellow]{backend} not reachable — writing placeholder assets. "
            f"Start {backend} to enable real generation.[/yellow]"
        )
        return _write_placeholders(task, output_dir)

    goal = spec.get("goal", "scene")
    style_hint = _extract_style(spec)
    img_style = _detect_image_style(task, spec)  # "realistic" | "anime"
    quality_pos, quality_neg = _style_modifiers(img_style)
    brief = spec.get("creative_brief", {}) if spec else {}
    written: list[str] = []

    for file_path in task.files:
        ext = Path(file_path).suffix.lower()
        if ext not in _ASSET_EXTS:
            continue
        if ext not in _RASTER_EXTS:
            written.extend(_write_placeholder_for(file_path, output_dir))
            continue

        asset_name = Path(file_path).stem.replace("_", " ").replace("-", " ")
        visual = (brief.get("visual_identity") or {})
        style = visual.get("style", "")
        palette = visual.get("palette", "")
        prompt = task.objective
        if style:
            prompt += f", {style}"
        if palette:
            prompt += f", {palette} color palette"
        prompt = f"{prompt}, {asset_name} for {goal}, {style_hint}, no text"
        prompt = f"{quality_pos}, {prompt}"
        negative = (
            "blurry, low quality, distorted, text, watermark, "
            f"signature, background clutter, ugly, deformed, {quality_neg}"
        )

        console.print(
            f"  [dim]Generating asset via {ASSET_PROVIDER} ({img_style} style): {file_path}…[/dim]"
        )
        dest = output_dir / file_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            if ASSET_PROVIDER == "comfyui":
                image_data = _comfyui_txt2img(prompt[:600], negative, img_style)
            else:
                image_data = _a1111_txt2img(prompt[:800], negative)

            if image_data:
                dest.write_bytes(image_data)
                console.print(f"  [green]✓ Generated: {file_path}[/green]")
                written.append(file_path)
            else:
                raise ValueError("backend returned no image data")
        except Exception as exc:
            console.print(
                f"  [yellow]Generation failed for {file_path}: {exc} — using placeholder.[/yellow]"
            )
            written.extend(_write_placeholder_for(file_path, output_dir))

    return written


# ── ComfyUI backend ───────────────────────────────────────────────────────────

def _available_checkpoints() -> list[str]:
    """Checkpoints ComfyUI reports as installed (empty list if it can't be reached)."""
    try:
        url = f"{COMFYUI_API_URL}/object_info/CheckpointLoaderSimple"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        return data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0] or []
    except Exception:
        return []


def _comfyui_checkpoint(style: str = "realistic") -> str:
    """Pick the checkpoint for the requested style.

    Priority: explicit COMFYUI_CHECKPOINT override → the style-matched model when
    installed → the other configured model when installed → first available →
    the preferred name (trusting config when ComfyUI's list is unavailable).
    """
    if COMFYUI_CHECKPOINT:
        return COMFYUI_CHECKPOINT
    available = _available_checkpoints()
    preferred = COMFYUI_CHECKPOINT_ANIME if style == "anime" else COMFYUI_CHECKPOINT_REALISTIC
    # Use the style-matched checkpoint when it's installed (or when we can't
    # verify the list — trust the configured name).
    if preferred and (preferred in available or not available):
        return preferred
    # Style-matched model not installed: fall back to the other configured one.
    other = COMFYUI_CHECKPOINT_REALISTIC if style == "anime" else COMFYUI_CHECKPOINT_ANIME
    if other and other in available:
        return other
    return available[0] if available else (preferred or "")


def _comfyui_txt2img(prompt: str, negative: str, style: str = "realistic") -> bytes | None:
    """Submit a txt2img workflow to ComfyUI and return PNG bytes.

    Uses the ComfyUI prompt API (async): POST /prompt → poll /history/{id}
    → GET /view for the output image. ``style`` selects the checkpoint.
    """
    import uuid

    checkpoint = _comfyui_checkpoint(style)
    if not checkpoint:
        raise RuntimeError("No ComfyUI checkpoint available")

    seed = random.randint(0, 2 ** 32 - 1)
    client_id = str(uuid.uuid4())

    workflow = {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["4", 1], "text": prompt},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["4", 1], "text": negative},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "batch_size": 1,
                "height": COMFYUI_HEIGHT,
                "width": COMFYUI_WIDTH,
            },
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "cfg": 7,
                "denoise": 1,
                "latent_image": ["5", 0],
                "model": ["4", 0],
                "negative": ["7", 0],
                "positive": ["6", 0],
                "sampler_name": COMFYUI_SAMPLER,
                "scheduler": COMFYUI_SCHEDULER,
                "seed": seed,
                "steps": COMFYUI_STEPS,
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "jclaw", "images": ["8", 0]},
        },
    }

    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode("utf-8")
    req = urllib.request.Request(
        f"{COMFYUI_API_URL}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())

    prompt_id = result["prompt_id"]
    console.print(f"  [dim]ComfyUI prompt queued: {prompt_id[:8]}…[/dim]")

    # Poll until done
    for step in range(_COMFYUI_TIMEOUT_STEPS):
        time.sleep(_COMFYUI_POLL_INTERVAL)
        history_url = f"{COMFYUI_API_URL}/history/{prompt_id}"
        with urllib.request.urlopen(history_url, timeout=10) as resp:
            history = json.loads(resp.read())

        if prompt_id not in history:
            continue

        entry = history[prompt_id]
        status = entry.get("status", {})
        if status.get("status_str") == "error" or status.get("completed") is False:
            messages = status.get("messages", [])
            raise RuntimeError(f"ComfyUI generation error: {messages}")

        outputs = entry.get("outputs", {})
        images = outputs.get("9", {}).get("images", [])
        if not images:
            continue

        img_info = images[0]
        view_url = (
            f"{COMFYUI_API_URL}/view"
            f"?filename={urllib.request.pathname2url(img_info['filename'])}"
            f"&subfolder={img_info.get('subfolder', '')}"
            f"&type={img_info.get('type', 'output')}"
        )
        with urllib.request.urlopen(view_url, timeout=30) as resp:
            return resp.read()

        break  # should not reach here

    raise TimeoutError(
        f"ComfyUI did not complete after {_COMFYUI_TIMEOUT_STEPS * _COMFYUI_POLL_INTERVAL}s"
    )


# ── A1111 / Forge backend ─────────────────────────────────────────────────────

def _a1111_txt2img(prompt: str, negative: str) -> bytes | None:
    """Call AUTOMATIC1111/Forge /sdapi/v1/txt2img and return PNG bytes."""
    payload = json.dumps({
        "prompt": prompt,
        "negative_prompt": negative,
        "steps": 20,
        "width": 512,
        "height": 512,
        "cfg_scale": 7,
        "sampler_name": "DPM++ 2M Karras",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{SD_API_URL}{_A1111_TXT2IMG}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return base64.b64decode(data["images"][0])


# ── Style extraction ──────────────────────────────────────────────────────────

def _detect_image_style(task, spec: dict) -> str:
    """Decide whether the brief wants a 'realistic' or 'anime' image.

    Scans the objective, goal, and creative brief for style keywords. Anime/
    cartoon cues route to the anime checkpoint; everything else (the default)
    routes to the realistic checkpoint.
    """
    spec = spec or {}
    brief = spec.get("creative_brief", {}) or {}
    visual = brief.get("visual_identity") or {}
    text = " ".join(str(x) for x in [
        getattr(task, "objective", ""),
        spec.get("goal", ""),
        brief.get("genre", ""), brief.get("tone", ""),
        brief.get("visual_style", ""), brief.get("style", ""),
        visual.get("style", ""),
        " ".join(spec.get("constraints", []) or []),
        " ".join(spec.get("features", []) or []),
    ]).lower()

    anime_hits = sum(1 for k in _ANIME_KEYWORDS if k in text)
    real_hits = sum(1 for k in _REALISTIC_KEYWORDS if k in text)
    return "anime" if anime_hits > real_hits else "realistic"


def _style_modifiers(style: str) -> tuple[str, str]:
    """Return (positive_prefix, extra_negative) quality tags for the style."""
    if style == "anime":
        return (
            "masterpiece, best quality, highly detailed, anime style, vibrant",
            "photorealistic, realistic photo, 3d render",
        )
    return (
        "masterpiece, best quality, highly detailed, sharp focus, photorealistic, "
        "professional photograph, realistic skin texture, natural lighting",
        "anime, cartoon, illustration, painting, sketch, 3d render, cgi",
    )


def _extract_style(spec: dict) -> str:
    hints = []
    for item in spec.get("constraints", []) + spec.get("features", []):
        item_lower = item.lower()
        for keyword in (
            "pixel art", "cartoon", "realistic", "flat design",
            "minimalist", "retro", "neon", "dark", "bright", "noir",
            "cinematic", "anime", "illustrated",
        ):
            if keyword in item_lower:
                hints.append(keyword)
                break
    return ", ".join(hints) if hints else "clean 2D art style, flat shading"


# ── Placeholder generation ────────────────────────────────────────────────────

def _make_solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    r, g, b = rgb
    raw = (b"\x00" + bytes((r, g, b)) * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def _write_placeholder_for(file_path: str, output_dir: Path) -> list[str]:
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
    written: list[str] = []
    for file_path in task.files:
        if Path(file_path).suffix.lower() in _ASSET_EXTS:
            written.extend(_write_placeholder_for(file_path, output_dir))
    return written
