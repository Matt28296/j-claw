"""Promote an evaluated candidate worker adapter into Ollama (runs on the 9070 XT). Pure stdlib.

Promotion is GATED and MANUAL by default:
  * the eval artifact's verdict must be ``promotable`` (so 'insufficient_evidence' / 'no_candidate'
    both refuse);
  * the eval's recorded ``candidate_hash`` must MATCH a CANONICAL hash of the adapter/GGUF on disk
    (you cannot promote something other than what was evaluated);
  * a Modelfile is written and the exact ``ollama create`` command is PRINTED — it runs only with
    ``--apply``;
  * every attempt writes ``PROMOTION_STATUS.json`` with candidate / dataset / eval hashes, the new
    tag, the previous tag (for rollback), and the rollback command.

Canonical hashing (the Syncthing failure modes Codex flagged): we hash an ALLOWLIST of adapter-defining
files (or the files listed in an ``ARTIFACT_MANIFEST.json`` the trainer writes last), sorted by relative
path, ignoring caches / Syncthing temp files / optimizer state — so the identity is stable across the
machine round-trip. A GGUF, when present, is hashed directly (it is what Ollama actually loads).

Run from the harness dir:
    python -m training.promote_worker --eval ../jclaw-training/evals/eval_<ts>.json --new-tag jclaw-worker:v001
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

HARNESS = Path(__file__).resolve().parent.parent
REPO = HARNESS.parent
DEFAULT_ADAPTER_DIR = REPO / "jclaw-training" / "adapters" / "jclaw-worker-v001"
DEFAULT_STATUS = REPO / "jclaw-training" / "PROMOTION_STATUS.json"

# Files that DEFINE a LoRA adapter's identity (everything else in the dir is incidental).
_ADAPTER_ALLOW = {
    "adapter_model.safetensors", "adapter_model.bin", "adapter_config.json",
    "tokenizer.json", "tokenizer.model", "tokenizer_config.json", "special_tokens_map.json",
}
# Substrings that mark a file as NOT part of the artifact (Syncthing temp, caches, trainer junk).
_IGNORE_SUBSTR = (".tmp", ".syncthing", "~syncthing~", ".stfolder", ".ds_store", "thumbs.db",
                  "__pycache__", "optimizer", "checkpoint-", ".lock", ".part")
_SYNC_MARKER = ".sync_complete"   # trainer writes this LAST; its absence => possibly mid-transfer


def _sha256_file(p: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ollama_version() -> str:
    """Best-effort `ollama --version` — recorded because deterministic gen is not stable across
    Ollama/model/runtime versions (Codex provenance point)."""
    try:
        proc = subprocess.run(["ollama", "--version"], capture_output=True, text=True, timeout=10)
        return (proc.stdout or proc.stderr or "").strip()[:120] or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _canonical_adapter_files(adapter_dir: Path) -> list[Path]:
    """The deterministic file list that defines the adapter. Prefer an ARTIFACT_MANIFEST.json (written
    last by the trainer with an explicit file list); else fall back to the allowlist."""
    manifest = adapter_dir / "ARTIFACT_MANIFEST.json"
    if manifest.exists():
        try:
            listed = json.loads(manifest.read_text(encoding="utf-8")).get("files")
        except (OSError, json.JSONDecodeError, AttributeError):
            listed = None
        if isinstance(listed, list) and listed:
            return [adapter_dir / rel for rel in sorted(listed) if isinstance(rel, str)]
    out = []
    for f in sorted(adapter_dir.rglob("*")):
        if not f.is_file():
            continue
        low = f.name.lower()
        if any(ig in str(f).lower() for ig in _IGNORE_SUBSTR):
            continue
        if low in _ADAPTER_ALLOW:
            out.append(f)
    return out


def adapter_model_hash(adapter_dir: Path, gguf: Path | None) -> tuple[str | None, list[str]]:
    """Canonical identity hash. Returns (hash, member_rel_paths). Prefers a GGUF (what Ollama loads)."""
    if gguf and gguf.exists():
        return _sha256_file(gguf), [gguf.name]
    if not adapter_dir.exists():
        return None, []
    members = _canonical_adapter_files(adapter_dir)
    if not members:
        return None, []
    h = hashlib.sha256()
    rels = []
    for f in members:
        if not f.is_file():
            continue
        rel = f.relative_to(adapter_dir).as_posix()
        rels.append(rel)
        h.update(rel.encode("utf-8"))
        h.update(str(f.stat().st_size).encode("utf-8"))
        fh = _sha256_file(f)
        if fh:
            h.update(fh.encode("utf-8"))
    return h.hexdigest(), rels


def _load_eval(eval_path: Path) -> dict | None:
    try:
        data = json.loads(eval_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_modelfile(path: Path, gguf: Path | None, base_model: str, system: str | None) -> Path:
    lines = [f"FROM {gguf.as_posix()}" if (gguf and gguf.exists()) else f"FROM {base_model}"]
    if system:
        safe = system.replace('"', '\\"').replace("\n", " ").strip()
        lines.append(f'SYSTEM "{safe}"')
    lines.append("PARAMETER temperature 0.15")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(args: argparse.Namespace) -> int:
    eval_path = Path(args.eval)
    ev = _load_eval(eval_path)
    if ev is None:
        print(f"ERROR: cannot read eval artifact: {eval_path}")
        return 2

    verdict = ev.get("verdict") or {}
    if not verdict.get("promotable"):
        print(f"REFUSED: eval verdict[{verdict.get('kind', '?')}] is not promotable.")
        for r in verdict.get("reasons", ["(no reason recorded)"]):
            print(f"  - {r}")
        return 1

    adapter_dir = Path(args.adapter_dir)
    gguf = Path(args.gguf) if args.gguf else None
    if gguf is None and not (adapter_dir / _SYNC_MARKER).exists() and not args.skip_sync_check:
        print(f"REFUSED: no {_SYNC_MARKER} marker in {adapter_dir} — the adapter may be mid-sync. "
              "Re-run after sync completes, or pass --skip-sync-check.")
        return 1

    model_hash, members = adapter_model_hash(adapter_dir, gguf)
    if model_hash is None:
        print(f"ERROR: no adapter/GGUF found to hash (adapter_dir={adapter_dir}, gguf={gguf}).")
        return 2

    cand_hash = ev.get("candidate_hash")
    if not cand_hash:
        print("REFUSED: eval artifact has no candidate_hash -- cannot prove the evaluated model is the "
              "one being promoted. Re-run eval_worker.py with --candidate-hash <canonical adapter/model hash>.")
        return 1
    if cand_hash != model_hash:
        print("REFUSED: candidate hash mismatch -- the adapter/GGUF on disk is NOT what was evaluated.")
        print(f"  eval candidate_hash : {cand_hash}")
        print(f"  adapter/model hash  : {model_hash}")
        print(f"  hashed members      : {members}")
        return 1

    modelfile = Path(args.modelfile) if args.modelfile else (adapter_dir / "Modelfile")
    _write_modelfile(modelfile, gguf, args.base_model, args.system)
    create_cmd = ["ollama", "create", args.new_tag, "-f", str(modelfile)]
    print(f"gates passed. Modelfile written: {modelfile}")
    print("ollama create command:\n  " + " ".join(create_cmd))

    applied, apply_error = False, None
    if args.apply:
        print("--apply set: running ollama create ...")
        try:
            proc = subprocess.run(create_cmd, capture_output=True, text=True, timeout=args.apply_timeout)
            if proc.returncode == 0:
                applied = True
                print("ollama create succeeded.")
            else:
                apply_error = (proc.stderr or proc.stdout or "").strip()[:500]
                print(f"ollama create FAILED (rc={proc.returncode}): {apply_error}")
        except (OSError, subprocess.SubprocessError) as exc:
            apply_error = f"{type(exc).__name__}: {exc}"
            print(f"ollama create could not run: {apply_error}")
    else:
        print("(dry run -- re-run with --apply to actually create the Ollama model.)")

    rollback_cmd = (f"ollama rm {args.new_tag}" if not args.prev_tag
                    else f"ollama cp {args.prev_tag} {args.new_tag}  # restore previous adapter")
    ev_prov = ev.get("provenance") or {}
    status = {
        "schema_version": 2,
        "kind": "jclaw-worker-promotion",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "new_tag": args.new_tag,
        "previous_tag": args.prev_tag or None,
        "applied": applied,
        "apply_error": apply_error,
        "candidate_hash": model_hash,
        "hashed_members": members,
        "eval_artifact": str(eval_path),
        "eval_sha256": _sha256_file(eval_path),
        "eval_verdict": verdict,
        "dataset_manifest_sha256": ev.get("dataset_manifest_sha256"),
        "modelfile": str(modelfile),
        "modelfile_sha256": _sha256_text(modelfile.read_text(encoding="utf-8")),
        "gguf": str(gguf) if gguf else None,
        "rollback_command": rollback_cmd,
        "provenance": {
            "base_model": args.base_model,
            "ollama_version": _ollama_version(),
            "eval_mode": ev.get("mode"),
            "eval_code_version": ev_prov.get("eval_code_version"),
            "eval_contract_versions": ev_prov.get("contract_versions"),
            "eval_ollama_version": ev_prov.get("ollama_version"),
            "promote_worker_version": 2,
        },
    }
    status_path = Path(args.status)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {status_path}\nrollback: {rollback_cmd}")
    return 1 if (args.apply and not applied) else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Gated promotion of an evaluated j-claw worker adapter into Ollama.")
    ap.add_argument("--eval", required=True, help="path to the eval_<ts>.json from eval_worker.py")
    ap.add_argument("--new-tag", required=True, help="Ollama tag to create, e.g. jclaw-worker:v001")
    ap.add_argument("--prev-tag", default="", help="previously-promoted Ollama tag (recorded for rollback)")
    ap.add_argument("--adapter-dir", default=str(DEFAULT_ADAPTER_DIR), help="adapter dir to hash/promote")
    ap.add_argument("--gguf", default="", help="GGUF file to FROM in the Modelfile (preferred over adapter)")
    ap.add_argument("--modelfile", default="", help="Modelfile path to write (default: <adapter-dir>/Modelfile)")
    ap.add_argument("--base-model", default="qwen2.5-coder:14b", help="base model for the Modelfile when no GGUF")
    ap.add_argument("--system", default="", help="optional SYSTEM prompt for the Modelfile")
    ap.add_argument("--apply", action="store_true", help="actually run `ollama create` (default: print only)")
    ap.add_argument("--apply-timeout", type=float, default=600.0, help="timeout for ollama create (s)")
    ap.add_argument("--skip-sync-check", action="store_true", help="bypass the .sync_complete marker check")
    ap.add_argument("--status", default=str(DEFAULT_STATUS), help="where to write PROMOTION_STATUS.json")
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
