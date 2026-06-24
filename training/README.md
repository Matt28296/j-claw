# `training/` — the 3060 Ti LoRA trainer (deferred)

This top-level dir is the **GPU trainer half** of the worker-LoRA pipeline. It runs in **WSL2 on the
3060 Ti** and is the only part that imports the heavy stack (Unsloth / torch / transformers / peft).
It is **authored when the 3060 Ti is set up** — nothing here runs on the 9070 XT.

The 9070 XT owns everything else, and those tools live in **`harness/training/`** (not here), because
they reuse the live harness (worker prompt, tolerant parser, verifier):

| Stage | Script | Lives in | Runs on | Status |
|---|---|---|---|---|
| Export curated SFT rows | `export_dataset.py` | `harness/training/` | 9070 XT | built |
| **Train** the QLoRA adapter | `train_worker.py` | **`training/` (here)** | **3060 Ti (WSL2)** | **deferred** |
| Evaluate base vs candidate | `eval_worker.py` | `harness/training/` | 9070 XT | built |
| Promote into Ollama | `promote_worker.py` | `harness/training/` | 9070 XT | built |
| Serve/train lifecycle | `node_agent.py` | `harness/` | 3060 Ti | deferred (Phase 1B) |

**No training dependency ever enters `harness/requirements.txt`.** The trainer's deps will live in
`training/requirements-wsl.txt` (added during 3060 Ti setup) and install only inside WSL2.

## Syncthing layout (shared between the two machines)
```
jclaw-training/
  datasets/   curated_v001.jsonl + manifest_v001.json   (9070 XT exporter output; rows carry metadata.split)
  adapters/   jclaw-worker-vNNN/                          (3060 Ti trainer output: adapter + ARTIFACT_MANIFEST.json + .sync_complete + optional GGUF)
  evals/      eval_<ts>.json                              (9070 XT eval_worker output)
```
Keep this tree on Syncthing so the 3060 Ti's adapter reaches the 9070 XT for eval/promote. **Windows↔WSL
path drift (`C:\...` vs `/mnt/c/...`) is the #1 silent failure** — set explicit roots per box in `.env`.
The trainer must write **`ARTIFACT_MANIFEST.json`** (the allowlist of files that define the adapter) and a
**`.sync_complete`** marker **last**, so `promote_worker.py` can hash a stable identity and refuse a
mid-sync adapter.

## End-to-end (once the 3060 Ti is set up)
```powershell
# 9070 XT: build the dataset from current verified builds
cd harness; python -m training.export_dataset

# 3060 Ti (WSL2): train the candidate adapter   (this script — deferred)
python train_worker.py --config sample_config.json

# 9070 XT: stage the candidate GGUF into Ollama as a temp tag, then evaluate (deep, gated)
cd harness; python -m training.eval_worker --deep --candidate-model jclaw-worker:cand \
    --candidate-hash <canonical adapter/gguf hash> --prev-model jclaw-worker:v000

# 9070 XT: promote ONLY if the eval verdict is promotable (prints the command; --apply to run it)
python -m training.promote_worker --eval ../jclaw-training/evals/eval_<ts>.json --new-tag jclaw-worker:v001
```

## Safety invariants carried through the pipeline
- **No paid provider is ever called** during eval — local Ollama only (non-local host refused).
- **No training on rejected builds** — the exporter emits rows only from `REVIEW.md VERDICT: PASS` projects.
- **Promotion is gated + manual** — needs a `promotable` eval, a candidate-hash match, and `--apply`; every
  promotion records a rollback command.
- **deep eval executes candidate code** — run it only hardened/hermetic (network off, no secrets, limits).
  True container isolation is documented before-trust work, not yet built.
