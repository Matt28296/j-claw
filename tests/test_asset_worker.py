"""Pure-function tests for the style-aware ComfyUI asset worker.

These cover the brief→style classification, the per-style prompt modifiers,
and the checkpoint-selection fallback logic. None of them touch ComfyUI:
the network call (``_available_checkpoints``) and the configured checkpoint
names are monkeypatched, so the suite runs anywhere the deps import.
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harness"))

import asset_worker  # noqa: E402

# Silence rich console output (mirrors the media-worker suite).
try:
    from rich.console import Console

    asset_worker.console = Console(file=io.StringIO())
except Exception:  # pragma: no cover - rich always present in this project
    pass


def _task(objective: str = "") -> SimpleNamespace:
    return SimpleNamespace(objective=objective, files=[])


class DetectImageStyleTests(unittest.TestCase):
    def test_no_style_cues_defaults_realistic(self) -> None:
        self.assertEqual(
            asset_worker._detect_image_style(_task(), {"goal": "a scene"}),
            "realistic",
        )

    def test_none_spec_does_not_crash_and_defaults_realistic(self) -> None:
        self.assertEqual(asset_worker._detect_image_style(_task(), None), "realistic")

    def test_anime_brief_detected(self) -> None:
        spec = {"creative_brief": {"visual_style": "vibrant anime, cel-shaded"}}
        self.assertEqual(asset_worker._detect_image_style(_task(), spec), "anime")

    def test_cartoon_objective_detected(self) -> None:
        spec = {"goal": "a mascot"}
        self.assertEqual(
            asset_worker._detect_image_style(_task("a cartoon comic mascot"), spec),
            "anime",
        )

    def test_noir_film_brief_is_realistic(self) -> None:
        """The project's actual noir use case must route to the realistic model."""
        spec = {
            "goal": "Make a 30 second noir film about a detective",
            "creative_brief": {"genre": "noir", "visual_style": "cinematic, photoreal"},
        }
        self.assertEqual(asset_worker._detect_image_style(_task(), spec), "realistic")

    def test_anime_film_is_not_outvoted_by_medium_words(self) -> None:
        """Regression: 'film'/'cinematic' are domain noise in a film pipeline and
        must not outvote an explicit anime cue."""
        self.assertEqual(
            asset_worker._detect_image_style(_task("Make an anime film about a robot"), {}),
            "anime",
        )
        self.assertEqual(
            asset_worker._detect_image_style(_task("an anime cinematic short"), {}),
            "anime",
        )

    def test_tie_falls_back_to_realistic(self) -> None:
        # One anime cue ("anime") vs one realistic cue ("photo ") → tie → realistic.
        spec = {"creative_brief": {"visual_style": "anime photo "}}
        self.assertEqual(asset_worker._detect_image_style(_task(), spec), "realistic")


class StyleModifierTests(unittest.TestCase):
    def test_realistic_modifiers(self) -> None:
        pos, neg = asset_worker._style_modifiers("realistic")
        self.assertIn("photorealistic", pos)
        self.assertIn("anime", neg)

    def test_anime_modifiers(self) -> None:
        pos, neg = asset_worker._style_modifiers("anime")
        self.assertIn("anime", pos)
        self.assertIn("photorealistic", neg)


class CheckpointSelectionTests(unittest.TestCase):
    """Monkeypatch the config-derived names and the availability probe."""

    def setUp(self) -> None:
        self._saved = {
            name: getattr(asset_worker, name)
            for name in (
                "COMFYUI_CHECKPOINT",
                "COMFYUI_CHECKPOINT_REALISTIC",
                "COMFYUI_CHECKPOINT_ANIME",
                "_available_checkpoints",
            )
        }
        asset_worker.COMFYUI_CHECKPOINT = ""
        asset_worker.COMFYUI_CHECKPOINT_REALISTIC = "real.safetensors"
        asset_worker.COMFYUI_CHECKPOINT_ANIME = "anime.safetensors"

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(asset_worker, name, value)

    def _set_available(self, names: list[str]) -> None:
        asset_worker._available_checkpoints = lambda: names

    def test_explicit_override_wins_for_every_style(self) -> None:
        asset_worker.COMFYUI_CHECKPOINT = "override.safetensors"
        self._set_available(["real.safetensors", "anime.safetensors"])
        self.assertEqual(asset_worker._comfyui_checkpoint("realistic"), "override.safetensors")
        self.assertEqual(asset_worker._comfyui_checkpoint("anime"), "override.safetensors")

    def test_style_matched_when_installed(self) -> None:
        self._set_available(["real.safetensors", "anime.safetensors"])
        self.assertEqual(asset_worker._comfyui_checkpoint("anime"), "anime.safetensors")
        self.assertEqual(asset_worker._comfyui_checkpoint("realistic"), "real.safetensors")

    def test_falls_back_to_other_when_preferred_missing(self) -> None:
        self._set_available(["real.safetensors"])  # anime model not installed
        self.assertEqual(asset_worker._comfyui_checkpoint("anime"), "real.safetensors")

    def test_trusts_config_name_when_list_unavailable(self) -> None:
        self._set_available([])  # ComfyUI unreachable
        self.assertEqual(asset_worker._comfyui_checkpoint("anime"), "anime.safetensors")
        self.assertEqual(asset_worker._comfyui_checkpoint("realistic"), "real.safetensors")


if __name__ == "__main__":
    unittest.main()
