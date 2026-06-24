"""Unsloth QLoRA fine-tuner for the j-claw worker LoRA (runs in WSL2 on the 3060 Ti).

Usage:
  python train_worker.py --config sample_config.json
  python train_worker.py --help          # works without importing unsloth

Conservative 8 GB defaults (4-bit quant, LoRA rank 8, batch 1 + grad-accum 4, grad-checkpointing).

EMITS LAST (in this order, so promote_worker.py can hash a stable identity):
  <out_dir>/ARTIFACT_MANIFEST.json   — file list + metadata (read by promote_worker)
  <out_dir>/.sync_complete           — empty marker; its absence means mid-transfer (DO NOT write earlier)

The system prompt used here is a synchronized copy of harness/worker.py::_SYSTEM_PROMPT.
If that prompt changes, bump TRAIN_WORKER_VERSION and update the copy below.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

TRAIN_WORKER_VERSION = 1

_TRAINING_DIR = Path(__file__).resolve().parent   # j-claw/training/
_REPO = _TRAINING_DIR.parent                      # j-claw/
_DEFAULT_DATASET = _REPO / "jclaw-training" / "datasets" / "curated_v001.jsonl"
_DEFAULT_OUT_DIR = _REPO / "jclaw-training" / "adapters" / "jclaw-worker-v001"


def _resolve_path(p: str) -> Path:
    """Relative paths in the config are resolved from the repo root (j-claw/), not CWD."""
    path = Path(p)
    return path if path.is_absolute() else (_REPO / path).resolve()


_ARTIFACT_MANIFEST = "ARTIFACT_MANIFEST.json"
_SYNC_COMPLETE = ".sync_complete"

# Synchronized copy of harness/worker.py::_SYSTEM_PROMPT (bump TRAIN_WORKER_VERSION if changed)
_SYSTEM_PROMPT = """\
You are a precise code-writing assistant in an automated pipeline.
You receive a single engineering task and write the exact file contents it requires.

Rules:
- Output ONLY a valid JSON object — no markdown, no prose, no explanation.
- The JSON must match this schema exactly:
  {"files": [{"path": "relative/path.ext", "content": "complete file content"}]}
- Every file listed in the task's "files" array must appear in your output.
- Write complete, working file contents. Never truncate, never use placeholders.
- Dependency files show what already exists on disk — do not re-emit them.
- OPTIONAL: you MAY add a top-level "lesson" object as a SIBLING of "files" (never inside a file
  entry) capturing the single key technique or gotcha for this task, e.g.
  {"files":[...], "lesson":{"solution_technique":"...","prompt_hint":"one-sentence rule","anti_pattern":"the mistake to avoid"}}.
  It is metadata for the build's memory, NOT a file. Omit it if you have nothing useful to add.
"""


# ── dataset ───────────────────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _build_user_message(row: dict) -> str:
    """Reconstruct the user JSON payload (same shape as harness/worker.py::_build_user_message)."""
    inp = row.get("input") or {}
    task_raw = inp.get("task") or {}
    spec = inp.get("spec") or {}
    deps = inp.get("dependency_files") or {}
    arch = spec.get("architecture") or {}
    stack = arch.get("stack") or spec.get("stack") or inp.get("context", {}).get("stack") or "vanilla"
    payload = {
        "task": {
            "id": task_raw.get("id", ""),
            "type": task_raw.get("type", ""),
            "objective": task_raw.get("objective", ""),
            "files": task_raw.get("files", []),
            "acceptance_criteria": task_raw.get("acceptance_criteria", []),
        },
        "project_context": {
            "goal": spec.get("goal", ""),
            "stack": stack,
            "architecture": arch,
        },
        "existing_dependency_files": {tid: files for tid, files in deps.items()},
    }
    return json.dumps(payload, indent=2)


def _build_assistant_response(row: dict) -> str:
    out = row.get("output") or {}
    files = out.get("files") or []
    return json.dumps({"files": files})


def _stack_note(row: dict) -> str:
    """Best-effort stack prompt note (no harness import). Just the stack name."""
    inp = row.get("input") or {}
    spec = inp.get("spec") or {}
    arch = spec.get("architecture") or {}
    stack = (arch.get("stack") or spec.get("stack") or
             (inp.get("context") or {}).get("stack") or "vanilla")
    return f"Stack: {stack}"


def rows_to_conversations(rows: list[dict]) -> list[list[dict]]:
    convs = []
    for row in rows:
        system = _SYSTEM_PROMPT + "\n" + _stack_note(row)
        user = _build_user_message(row)
        assistant = _build_assistant_response(row)
        if not (row.get("input") or {}).get("task"):
            continue
        convs.append([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ])
    return convs


# ── config ────────────────────────────────────────────────────────────────────

_DEFAULTS: dict = {
    "base_model": "unsloth/Qwen2.5-Coder-7B-Instruct",
    "dataset": str(_DEFAULT_DATASET),
    "out_dir": str(_DEFAULT_OUT_DIR),
    "lora_rank": 8,
    "lora_alpha": 16,
    "lora_dropout": 0.0,
    "load_in_4bit": True,
    "per_device_train_batch_size": 1,
    "gradient_accumulation_steps": 4,
    "gradient_checkpointing": True,
    "num_train_epochs": 3,
    "learning_rate": 2e-4,
    "warmup_steps": 10,
    "max_seq_length": 2048,
    "seed": 42,
    "split": "train",                  # "train" = train rows; "heldout" = heldout; "all" = all
}


def load_config(config_path: Path) -> dict:
    cfg = dict(_DEFAULTS)
    if config_path.exists():
        try:
            overrides = json.loads(config_path.read_text(encoding="utf-8"))
            cfg.update(overrides)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"WARNING: could not read config {config_path}: {exc} — using defaults")
    return cfg


# ── training (lazy unsloth import) ───────────────────────────────────────────

def run_training(cfg: dict) -> int:
    print("importing unsloth (may take a moment) ...")
    try:
        from unsloth import FastLanguageModel
        from trl import SFTTrainer, SFTConfig
        from transformers import TrainingArguments
        import torch
    except ImportError as exc:
        print(f"ERROR: {exc}")
        print("Install training deps: pip install -r requirements-wsl.txt")
        return 1

    base_model = cfg["base_model"]
    max_seq = cfg["max_seq_length"]

    dataset_path = _resolve_path(cfg["dataset"])
    if not dataset_path.exists():
        print(f"ERROR: dataset not found: {dataset_path}")
        print("Run: cd harness && python -m training.export_dataset")
        return 1

    out_dir = _resolve_path(cfg["out_dir"])

    rows = _load_jsonl(dataset_path)
    split_filter = cfg.get("split", "train")
    if split_filter in ("train", "heldout"):
        rows = [r for r in rows if (r.get("metadata") or {}).get("split") == split_filter]
    conversations = rows_to_conversations(rows)
    if not conversations:
        print(f"ERROR: no usable rows (split={split_filter!r}) in {dataset_path}")
        return 1
    print(f"dataset: {len(conversations)} conversation(s) from {dataset_path.name} (split={split_filter!r})")

    print(f"loading model: {base_model} (4bit={cfg['load_in_4bit']}, max_seq={max_seq}) ...")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=base_model,
        max_seq_length=max_seq,
        load_in_4bit=cfg["load_in_4bit"],
        dtype=None,
    )

    print(f"applying LoRA (rank={cfg['lora_rank']}, alpha={cfg['lora_alpha']}) ...")
    model = FastLanguageModel.get_peft_model(
        model,
        r=cfg["lora_rank"],
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
        lora_alpha=cfg["lora_alpha"],
        lora_dropout=cfg["lora_dropout"],
        bias="none",
        use_gradient_checkpointing="unsloth" if cfg["gradient_checkpointing"] else False,
        random_state=cfg["seed"],
    )

    # Apply Qwen chat template and format conversations
    tokenizer = FastLanguageModel.get_tokenizer(tokenizer)
    try:
        from unsloth.chat_templates import get_chat_template
        tokenizer = get_chat_template(tokenizer, chat_template="qwen-2.5")
    except Exception:
        pass

    def _format(conv: list[dict]) -> str:
        try:
            return tokenizer.apply_chat_template(conv, tokenize=False, add_generation_prompt=False)
        except Exception:
            parts = []
            for m in conv:
                parts.append(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n")
            return "".join(parts)

    texts = [_format(c) for c in conversations]

    from datasets import Dataset as HFDataset
    hf_dataset = HFDataset.from_dict({"text": texts})

    training_args = SFTConfig(
        output_dir=str(out_dir / "checkpoints"),
        per_device_train_batch_size=cfg["per_device_train_batch_size"],
        gradient_accumulation_steps=cfg["gradient_accumulation_steps"],
        warmup_steps=cfg["warmup_steps"],
        num_train_epochs=cfg["num_train_epochs"],
        learning_rate=cfg["learning_rate"],
        fp16=not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        logging_steps=1,
        optim="adamw_8bit",
        seed=cfg["seed"],
        report_to="none",
        max_seq_length=max_seq,
        dataset_text_field="text",
        packing=False,
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=hf_dataset,
        args=training_args,
    )

    print("training ...")
    t0 = time.monotonic()
    trainer.train()
    print(f"training done in {time.monotonic() - t0:.0f}s")

    print(f"saving adapter to {out_dir} ...")
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))

    _write_artifact_manifest(out_dir, base_model, cfg)
    print(f"wrote {_ARTIFACT_MANIFEST}")
    _write_sync_complete(out_dir)
    print(f"wrote {_SYNC_COMPLETE} — Syncthing can now sync the adapter")
    return 0


# ── artifact manifest (must be written before .sync_complete) ─────────────────

_ADAPTER_FILES = {
    "adapter_config.json", "adapter_model.safetensors", "adapter_model.bin",
    "tokenizer.json", "tokenizer.model", "tokenizer_config.json", "special_tokens_map.json",
}
_IGNORE_SUBSTR = (".tmp", ".syncthing", "~syncthing~", ".stfolder", ".ds_store",
                  "__pycache__", "optimizer", "checkpoint-", ".lock", ".part")


def _write_artifact_manifest(out_dir: Path, base_model: str, cfg: dict) -> None:
    files = []
    for f in sorted(out_dir.rglob("*")):
        if not f.is_file():
            continue
        low = f.name.lower()
        if any(ig in str(f).lower() for ig in _IGNORE_SUBSTR):
            continue
        if low not in _ADAPTER_FILES:
            continue
        files.append(f.relative_to(out_dir).as_posix())

    manifest = {
        "schema_version": 1,
        "train_worker_version": TRAIN_WORKER_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_model": base_model,
        "lora_rank": cfg.get("lora_rank"),
        "lora_alpha": cfg.get("lora_alpha"),
        "max_seq_length": cfg.get("max_seq_length"),
        "num_train_epochs": cfg.get("num_train_epochs"),
        "dataset": cfg.get("dataset"),
        "files": files,
    }
    (out_dir / _ARTIFACT_MANIFEST).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _write_sync_complete(out_dir: Path) -> None:
    (out_dir / _SYNC_COMPLETE).write_text(
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), encoding="utf-8"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Unsloth QLoRA fine-tuner for j-claw worker LoRA (3060 Ti / WSL2).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--config", default="sample_config.json",
                    help="JSON config file (default: sample_config.json)")
    ap.add_argument("--dry-run", action="store_true",
                    help="load dataset and print stats, skip training")
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = _build_parser().parse_args(argv)
    cfg = load_config(Path(args.config))

    if args.dry_run:
        dataset_path = _resolve_path(cfg["dataset"])
        rows = _load_jsonl(dataset_path) if dataset_path.exists() else []
        split_filter = cfg.get("split", "train")
        if split_filter in ("train", "heldout"):
            rows = [r for r in rows if (r.get("metadata") or {}).get("split") == split_filter]
        convs = rows_to_conversations(rows)
        print(f"dry-run: {len(convs)} conversation(s) from {dataset_path} (split={split_filter!r})")
        print(f"config: {json.dumps(cfg, indent=2)}")
        return 0

    return run_training(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
