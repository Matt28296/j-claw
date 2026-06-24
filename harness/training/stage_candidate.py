#!/usr/bin/env python
"""Stage a trained worker adapter as a servable Ollama candidate model for eval_worker.

The training side emits a LoRA adapter (safetensors), but eval_worker --candidate-model expects an
Ollama model TAG (it does local-Ollama A/B generation). This helper bridges that gap: it builds an
Ollama Modelfile and runs `ollama create`, producing a temp candidate tag, then prints the tag and the
canonical artifact hash that promote_worker requires.

Two input modes:
  --merged-gguf PATH    a standalone merged GGUF (e.g. unsloth save_pretrained_gguf output).
                        Modelfile: `FROM <gguf>`.  (Preferred — self-contained, no base needed.)
  --adapter-gguf PATH --base-tag TAG
                        a LoRA adapter converted to GGUF + an existing Ollama base tag.
                        Modelfile: `FROM <base-tag>\nADAPTER <adapter-gguf>`.

The canonical hash is sha256 of the GGUF file (matches promote_worker's allowlist/manifest check).
Local-only; never contacts a paid provider.

Usage:
  python -m training.stage_candidate --merged-gguf ../jclaw-training/adapters/jclaw-worker-v001-gguf/model.gguf \
      --tag jclaw-worker:cand
  # then:
  python -m training.eval_worker --deep --candidate-model jclaw-worker:cand \
      --candidate-hash <printed hash> --base-model qwen2.5-coder:7b --prev-model qwen2.5-coder:7b \
      --dataset ../jclaw-training/datasets/curated_v001.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ollama_bin() -> str:
    found = shutil.which("ollama")
    if not found:
        sys.exit("ERROR: `ollama` not on PATH — run this on a box with Ollama installed (the trainer/sidecar).")
    return found


def _ollama_has(tag: str) -> bool:
    try:
        out = subprocess.run([_ollama_bin(), "list"], capture_output=True, text=True, timeout=30)
        return any(line.split()[:1] == [tag] or line.startswith(tag + " ") or line.startswith(tag + "\t")
                   for line in out.stdout.splitlines())
    except Exception:  # noqa: BLE001
        return False


def build_modelfile(merged_gguf: Path | None, adapter_gguf: Path | None, base_tag: str | None) -> str:
    if merged_gguf is not None:
        return f"FROM {merged_gguf.as_posix()}\n"
    return f"FROM {base_tag}\nADAPTER {adapter_gguf.as_posix()}\n"  # type: ignore[union-attr]


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage a trained adapter as an Ollama candidate model.")
    ap.add_argument("--merged-gguf", type=Path, help="standalone merged GGUF (FROM <gguf>)")
    ap.add_argument("--adapter-gguf", type=Path, help="LoRA adapter as GGUF (needs --base-tag)")
    ap.add_argument("--base-tag", help="existing Ollama base tag for --adapter-gguf mode")
    ap.add_argument("--tag", default="jclaw-worker:cand", help="candidate Ollama tag to create")
    args = ap.parse_args()

    if args.merged_gguf:
        gguf = args.merged_gguf
        if args.adapter_gguf or args.base_tag:
            sys.exit("ERROR: use EITHER --merged-gguf OR (--adapter-gguf + --base-tag), not both.")
    elif args.adapter_gguf and args.base_tag:
        gguf = args.adapter_gguf
        if not _ollama_has(args.base_tag):
            sys.exit(f"ERROR: base tag '{args.base_tag}' not in Ollama — `ollama pull {args.base_tag}` first.")
    else:
        sys.exit("ERROR: provide --merged-gguf, or both --adapter-gguf and --base-tag.")

    if not gguf.exists():
        sys.exit(f"ERROR: GGUF not found: {gguf}")

    chash = _sha256(gguf)
    modelfile = build_modelfile(args.merged_gguf, args.adapter_gguf, args.base_tag)

    with tempfile.TemporaryDirectory() as td:
        mf = Path(td) / "Modelfile"
        mf.write_text(modelfile, encoding="utf-8")
        print(f"Modelfile:\n{modelfile}")
        print(f"Running: ollama create {args.tag} -f {mf}")
        r = subprocess.run([_ollama_bin(), "create", args.tag, "-f", str(mf)], text=True)
        if r.returncode != 0:
            return r.returncode

    if not _ollama_has(args.tag):
        sys.exit(f"ERROR: `ollama create` reported success but '{args.tag}' is not in `ollama list`.")

    print("\n=== STAGED ===")
    print(f"candidate_tag:  {args.tag}")
    print(f"candidate_hash: {chash}")
    print(f"gguf:           {gguf}")
    print("\nNext: python -m training.eval_worker --deep \\")
    print(f"  --candidate-model {args.tag} --candidate-hash {chash} \\")
    print("  --base-model <base ollama tag> --dataset <curated jsonl>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
