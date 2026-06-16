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
        negative = (
            "blurry, low quality, distorted, text, watermark, "
            "signature, background clutter, ugly, deformed"
        )

        console.print(f"  [dim]Generating asset via {ASSET_PROVIDER}: {file_path}…[/dim]")
        dest = output_dir / file_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            if ASSET_PROVIDER == "comfyui":
                image_data = _comfyui_txt2img(prompt[:600], negative)
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

def _comfyui_checkpoint() -> str:
    """Return the checkpoint name to use: COMFYUI_CHECKPOINT env var or the
    first model listed by ComfyUI's object_info endpoint."""
    if COMFYUI_CHECKPOINT:
        return COMFYUI_CHECKPOINT
    try:
        url = f"{COMFYUI_API_URL}/object_info/CheckpointLoaderSimple"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        models = data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        if models:
            return models[0]
    except Exception:
        pass
    return ""


def _comfyui_txt2img(prompt: str, negative: str) -> bytes | None:
    """Submit a txt2img workflow to ComfyUI and return PNG bytes.

    Uses the ComfyUI prompt API (async): POST /prompt → poll /history/{id}
    → GET /view for the output image.
    """
    import uuid

    checkpoint = _comfyui_checkpoint()
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
                "sampler_name": "euler_ancestral",
                "scheduler": "normal",
                "seed": seed,
                "steps": 20,
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
