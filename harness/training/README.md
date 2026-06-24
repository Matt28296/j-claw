# harness/training — dataset export (9070 XT)

Builds curated supervised-fine-tuning (SFT) data from **verified** j-claw build artifacts. Pure stdlib —
**no torch / Unsloth / LLM imports** (those live in the top-level `training/` folder for the 3060 Ti).

## What it does
`export_dataset.py` scans `harness/projects/**` and emits one SFT row per high-confidence completed task.

**High-confidence gate** (mirrors The-Brain's build "graduation" signal): a project contributes rows only
if `REVIEW.md` contains `VERDICT: PASS` **and** every task is `done`. Per task it also requires:
- the declared output files exist on disk and are non-empty (file *content* lives on disk, not in JSON);
- no stub/placeholder markers (`NotImplementedError`, `TODO: implement`, `lorem ipsum`, …);
- no embedded secret (a row whose output code contains a key/token is dropped, not just masked).

Every row **and** the manifest pass through `secret_scrub.scrub_obj` before writing.

## Output
```
jclaw-training/datasets/curated_v001.jsonl
jclaw-training/datasets/manifest_v001.json   # row/skip counts, exclusion reasons, hashes, splits
```
Each row carries a deterministic `metadata.split` of `train` or `heldout` (~20% held out, stable by
`project::task_id`).

## Run
```powershell
cd harness
python -m training.export_dataset            # writes to ../jclaw-training/datasets/
python -m training.export_dataset --out ../jclaw-training/datasets/curated_v001.jsonl
```

## Test
```powershell
cd harness
python test_export_dataset.py
```

## Not here (deferred — 3060 Ti work)
LoRA training (`train_worker.py`), eval (`eval_worker.py`), and promotion (`promote_worker.py`) live in
the repo-root `training/` folder; the train lifecycle on the sidecar is `harness/node_agent.py` (Phase 1B).
