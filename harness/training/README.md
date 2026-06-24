# harness/training — the 9070 XT side of the LoRA pipeline (export · eval · promote)

The always-on 9070 XT owns three stages: **export** curated data, **eval** a candidate worker model,
and **promote** it into Ollama. All three are **pure stdlib + (eval) local Ollama HTTP** — **no torch /
Unsloth / LLM imports**. The QLoRA trainer (`train_worker.py`) is the only 3060-Ti piece and lives in the
repo-root `training/` folder.

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

## eval_worker.py — A/B evaluate a candidate worker model (9070 XT)

Reuses the **versioned evaluation contract** (`harness/evaluation_contract.py`) so the numbers reflect
how j-claw *actually* uses a worker — the SAME prompt (`_SYSTEM_PROMPT + _STACK_PROMPTS[stack]` + the
structured user payload), the SAME tolerant parser (`_parse_and_validate`, incl. single-file salvage),
and the SAME verifier (`run_verification`). Generation is deterministic (temperature 0 + fixed seed) and
**local Ollama only** (a non-local `--ollama-host` is refused — the no-paid invariant).

Two tiers — **you can never promote from fast alone**:
- **fast** (default): parse + path-safety + static AST/stub checks. Executes nothing the model produced.
  Verdict caps at `smoke_passed`.
- **deep** (`--deep`): runs the REAL verifier with red/green DISCRIMINATION controls per row
  (`verify(before)` must fail, `verify(before+gold)` must pass, `verify(before+candidate)` is the score;
  `auto-passed:` ⇒ *skipped*, never *pass*). **deep executes candidate-produced code — run it only in a
  hardened/hermetic env (network off, no secrets, resource limits).** Container isolation is **before-trust
  work, not yet built.**

Promotion gates on **effective evidence**: `verify_attempted_n ≥ MIN_ATTEMPTED_N` (default 20), bounded
`skip_rate`, paired per-row wins over base on *attempted* rows, no parse regression, no per-stack collapse,
and no regression past budget vs a previous adapter. Verdicts: `no_candidate` · `smoke_passed` ·
`insufficient_evidence` · `compared{promotable}`. Writes `jclaw-training/evals/eval_<ts>.json` with full
provenance (contract versions, Ollama version, gen params, dataset hash). The live corpus is tiny (~1–2
held-out rows) so deep eval will return `insufficient_evidence` until a frozen **canary eval pack** exists
(a curated, never-trained set; convention reserved, content TBD).

```powershell
cd harness
python -m training.eval_worker --candidate-model jclaw-worker:cand          # fast smoke
python -m training.eval_worker --deep --candidate-model jclaw-worker:cand --prev-model jclaw-worker:v000
```

## promote_worker.py — gated promotion into Ollama (9070 XT)

Refuses unless the eval verdict is `promotable` **and** the eval's `candidate_hash` matches a CANONICAL
hash of the adapter/GGUF on disk (allowlist of adapter-defining files / an `ARTIFACT_MANIFEST.json`,
ignoring Syncthing junk; a `.sync_complete` marker guards mid-transfer). Writes a Modelfile, **prints**
`ollama create`, and runs it only with `--apply`. Every attempt writes `PROMOTION_STATUS.json` with the
candidate/dataset/eval hashes, Modelfile hash, full provenance, the previous tag, and a rollback command.

```powershell
cd harness
python -m training.promote_worker --eval ../jclaw-training/evals/eval_<ts>.json --new-tag jclaw-worker:v001
python -m training.promote_worker --eval ... --new-tag jclaw-worker:v001 --prev-tag jclaw-worker:v000 --apply
```

## Tests
```powershell
cd harness
python test_export_dataset.py     # exporter gates
python test_eval_contract.py      # golden: prompt composition, parser salvage, fail-closed, skip semantics
python test_eval_worker.py        # eval logic: path-safety, red/green, attempted-N gates, verdicts
```

## Not here (deferred — 3060 Ti work)
The QLoRA trainer (`train_worker.py`) + `requirements-wsl.txt` live in the repo-root `training/` folder;
the sidecar serve/train lifecycle is `harness/node_agent.py` (Phase 1B). Both are authored when the
3060 Ti is set up.
