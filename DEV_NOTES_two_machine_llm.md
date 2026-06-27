# Dev Notes — Two-Machine Local-LLM Support (Phase 1A + 1B — COMPLETE)

> Status (updated 2026-06-25): **LOOP FULLY CLOSED end-to-end across both machines.** All phases
> complete: Phase 1A routing + dataset export (9070 XT), Phase 1B node agent + QLoRA trainer + GGUF
> export + eval pipeline (3060 Ti). The full run was proven:
> export_dataset → Syncthing → QLoRA train → adapter → Syncthing → merge→GGUF (`stage_candidate`) →
> `eval_worker --deep` (A/B vs qwen2.5-coder:7b-instruct, local Ollama, RED/GREEN verifier) → gated promote.
> Deep-eval verdict on the 8-row smoke corpus: **`insufficient_evidence` / not-promotable**
> (`verify_attempted_n=0 < MIN_ATTEMPTED_N=20`), `no_paid_provider_called=true` — gate correctly refusing
> on thin data. For a real promotion: ≥20 held-out rows + prefer Path A (torch 2.6.0+cu124 + current
> unsloth — the older-unsloth pin caused serial API-drift fixes: get_tokenizer/FP8BackendType/xformers/triton).
> Full design rationale lives in the plan file `~/.claude/plans/i-am-currently-talking-calm-eclipse.md`.

## 1. Why this exists / goal
Let j-claw use a **second PC** for local LLM work without spending money or weakening safety:
- **9070 XT PC** = the PRIMARY, always-on j-claw runner + primary Ollama worker. Everything here runs
  on this box: the scheduler, routing decisions, the dashboard, and dataset export.
- **3060 Ti PC** = an OPTIONAL second box that is *either* a **sidecar Ollama worker** (extra capacity
  for low-risk tasks while `RUNNING`) *or* a **LoRA trainer** (then removed from serving) — **never both
  at once**.

Hard invariant carried over from the existing harness: **a local-Ollama infrastructure failure must
never escalate to a paid cloud provider.** Adding a second node must not open a hole in that.

## 2. Machine responsibilities (who runs what)
| Component | Runs on | Status |
|---|---|---|
| Routing registry, scheduler, dashboard, worker calls | 9070 XT | ✅ Phase 1A done |
| Dataset export (`harness/training/`) | 9070 XT | ✅ done |
| `node_agent.py` train/serve lifecycle | 3060 Ti | ✅ Phase 1B done (2026-06-24) |
| `train_worker.py` (Unsloth QLoRA) | 3060 Ti (WSL2) | ✅ done — end-to-end trained (2026-06-24) |
| `eval_worker.py` / `promote_worker.py` | 9070 XT / 3060 Ti | ✅ done — ran deep eval (2026-06-25) |

The 3060 Ti is fully wired. Both machines are on Matt28296's Tailscale tailnet; Syncthing syncs
`jclaw-training/` and `node_state/` bidirectionally. For 3060 Ti day-to-day operations see
`SETUP_3060TI.md` at the repo root.

## 3. Phase 1A — local-LLM node routing (9070 XT)

### Files
- **`harness/node_registry.py` (new)** — the routing brain. Parses the node pool, reads each node's
  JSON state file (with a *serving lease*), health-checks `/api/tags` (cached), tracks per-node inflight,
  and chooses where an Ollama call goes. Public API: `choose_ollama_node(task)` / `release_ollama_node`
  / `reserved_node` (context manager) / `node_snapshot` / `primary_id`. **Its universe is local Ollama
  nodes only — it never returns a cloud provider.**
- **`harness/config.py`** — added the node-pool config block (`LOCAL_LLM_NODES`, `PRIMARY_LLM_NODE`,
  `TRAINER_NODE`, `NODE_STATE_DIR`, `NODE_HEALTH_TIMEOUT_S`, `NODE_STATE_TTL_S`,
  `NODE_MAX_INFLIGHT_DEFAULT`, `SIDECAR_ALLOWED_TASK_TYPES`).
- **`harness/worker.py`** — `_call_provider(...)` and `_call_ollama(...)` now take `task`; `_call_ollama`
  routes through the registry, and a new helper `_ollama_call_on(node_id, url, …)` makes the actual call
  and **releases the node's inflight slot in `finally`**. The pre-existing no-escalation guard is intact.
- **`harness/state_writer.py`** — new `llm_nodes` state key + `refresh_llm_nodes()` (snapshot → state,
  best-effort) + `on_node_update()`.
- **`harness/scheduler.py`** — calls `sw.refresh_llm_nodes()` at each dispatch batch (best-effort).
- **`dashboard/index.html`** — a read-only "Local LLM Nodes" panel (`renderLlmNodes`) colored by mode
  (RUNNING=green; DRAINING/TRAINING/EXPORTING/EVALUATING/RETURNING=yellow; OFFLINE/TRAINING_FAILED=red).
- **`.gitignore`** — `node_state/`, `harness/node_state/`, `jclaw-training/`.
- **`harness/test_node_registry.py` (new)** — 13 tests.

### How routing decides (the rules)
A call goes to the **sidecar** only if ALL of: its state is `RUNNING`, the file is fresh (within
`NODE_STATE_TTL_S`), it advertises a valid `serving_allowed` lease (`serving_allowed_until` in the
future), the task's `type` is in `SIDECAR_ALLOWED_TASK_TYPES`, it has spare `max_inflight` capacity, and
its `/api/tags` health probe passes. Otherwise it **fails closed to the primary**.

### Key design decisions (and why)
1. **The PRIMARY always uses `OLLAMA_HOST` and is never capacity-gated.** This guarantees zero regression:
   existing single-machine setups behave exactly as before, and the primary is always available as the
   fallback of last resort. `max_inflight` gates **only** the sidecar.
2. **In-call fallback closes a safety hole.** If the registry routes to the sidecar and that call fails
   with an *infrastructure* error, `_call_ollama` retries once on the primary. Only when **every** local
   node fails does the infra error propagate — and the existing guard in `worker.execute_task`
   (`provider == "ollama" and _is_ollama_unavailable(exc)`) turns that into a hard failure, **never a
   paid-cloud call**. Capability errors (bad/non-JSON output) propagate immediately so the normal ladder
   still escalates. This is covered by a test.
3. **A serving *lease*, not just a file flag.** A state file alone is a weak "is it safe to serve" signal
   across two machines (file-sync lag). The sidecar must advertise `serving_allowed` + an expiry, and a
   short `NODE_STATE_TTL_S` (10 s) means a stale file quickly reads as OFFLINE. (Phase 1B's `node_agent`
   will additionally drain + stop Ollama before training — see §5.)
4. **Single-routing-process is an explicit assumption.** j-claw runs one scheduler/ThreadPoolExecutor,
   so the per-process inflight counters ARE authoritative — no SQLite/file lock needed. Documented in
   `config.py` and `.env.example`; do not run two routers against one sidecar.
5. **Health checks are cached** (`_HEALTH_TTL_S`) so the hot path never blocks on the network per call.

## 4. Dataset export — `harness/training/` (9070 XT)
Builds supervised-fine-tuning (SFT) rows from **verified** build artifacts so a future worker LoRA can be
trained on j-claw's own successful output. Pure stdlib — **no torch/Unsloth/LLM imports.**

### Files
- **`secret_scrub.py`** — deterministic secret/PII scrubbing, ported from The-Brain's `brain/redact.py`
  and extended (OpenRouter keys, JWTs, OAuth/Bearer, generic `KEY=value` env assignments incl. prefixed
  names like `ANTHROPIC_API_KEY=`, emails, Windows/Unix user paths). API: `scrub_text`, `scrub_obj`
  (recursive, never crashes on non-strings), `contains_secret`, `SCRUBBER_VERSION`.
- **`export_dataset.py`** — the exporter (CLI: `python -m training.export_dataset`).
- **`README.md`** — usage.
- **`harness/test_export_dataset.py` (new)** — 5 tests.

### Quality gate (why these rows and not others)
- **Project-level:** include a project only if its `REVIEW.md` says `VERDICT: PASS` **and** every task is
  `done`. This is the same honest-review signal The-Brain uses to "graduate" a build, so the dataset
  inherits that bar (no training on builds the reviewer rejected).
- **Per-task negative gates:** declared output files must exist on disk and be non-empty; no stub markers
  (`NotImplementedError`, `TODO: implement`, `lorem ipsum`, …); no embedded secret (a row whose output
  code contains a key is **dropped**, not just masked).
- **Recon gotcha handled:** a task's output file *content* is **not** in `tasks_done.json` — it's on disk
  in the project dir. The exporter maps each task's declared `files` → `project_dir/<rel>` and reads them.
- **Scrub-before-write:** every row AND the manifest pass through `secret_scrub.scrub_obj` before writing.

### Output
`jclaw-training/datasets/curated_v001.jsonl` + `manifest_v001.json` (row/skip counts, **exclusion reasons
+ counts**, content SHA-256s, scrubber version, and a deterministic `train`/`heldout` split (~20% held
out, stable by `project::task_id`)). Each row also carries its split in `metadata.split`.

### Verified behaviour
Running on the real `harness/projects/**`: 9 project units scanned, **8 rows** exported from exactly the
two `VERDICT: PASS` units (`_verify_tipcalc`, `_fmt5_smoke_run1/cli_tool`); 64 tasks correctly skipped
(57 `missing_output_file` from crashed builds, 5 `no_pass_review`, 2 `empty_output`). `requirements.txt`
confirmed un-polluted.

## 5. Eval + Promote (9070 XT) — BUILT (2026-06-24)

The 9070-XT half of the LoRA pipeline now exists, built **high-fidelity** after a deep read of the
harness (and two Codex second-opinion rounds). Key decision: eval/promote live in **`harness/training/`**
(NOT the repo-root `training/` the first plan suggested) so eval can reuse the LIVE worker behaviour
instead of a low-fidelity reimplementation.

- **`harness/evaluation_contract.py` (new)** — a small VERSIONED public surface so eval never imports
  harness privates directly (refactor-drift guard): `task_from_dataset_row` (fail-closed on missing
  type/objective/files), `build_worker_prompt` (exact `_SYSTEM_PROMPT + _STACK_PROMPTS[stack]` + the
  structured user payload), `parse_worker_output` (the real tolerant `_parse_and_validate`, incl.
  single-file salvage), `verify_task` (`run_verification`; `auto-passed:` ⇒ *skipped*, not *pass*),
  `ollama_version`, `versions()`. Pinned by golden tests.
- **`harness/training/eval_worker.py` (new)** — deterministic (temp 0 + seed) local-Ollama generation,
  **local-host enforced** (no-paid). **Two tiers:** *fast* (default — parse + path-safety + static
  AST/stub, executes nothing) caps at `smoke_passed`; *deep* (`--deep`) runs the real verifier with
  **red/green discrimination controls** (`verify(before)` must fail, `verify(before+gold)` must pass,
  `verify(before+candidate)` is the score). Promotion gates on **effective evidence**:
  `verify_attempted_n ≥ MIN_ATTEMPTED_N` (20), bounded `skip_rate`, paired per-row wins vs base on
  *attempted* rows, no parse regression, no per-stack collapse, no regression past budget vs a previous
  adapter. Verdicts: `no_candidate`/`smoke_passed`/`insufficient_evidence`/`compared{promotable}`. Writes
  `eval_<ts>.json` with full provenance.
- **`harness/training/promote_worker.py` (new)** — refuses unless verdict `promotable` AND the eval's
  `candidate_hash` matches a **canonical** adapter/GGUF hash (allowlist / `ARTIFACT_MANIFEST.json`,
  ignores Syncthing junk; `.sync_complete` guard). Writes a Modelfile, **prints** `ollama create`, runs
  it only with `--apply`; `PROMOTION_STATUS.json` carries hashes + provenance + rollback command.
- **Tests (all green, harness venv):** `test_eval_contract.py` (7 golden) + `test_eval_worker.py`
  (10 logic). No regression in `test_node_registry.py` (13) / `test_export_dataset.py` (5).

**Why deep eval is gated/opt-in:** it executes candidate-produced code via the real build/test
toolchain. Run it only in a hardened/hermetic env (network off, no secrets, resource limits). True
**container isolation** and a frozen **canary eval pack** (so deep eval isn't permanently
`insufficient_evidence` on the ~1–2-row live corpus) are documented **before-trust** work, not yet built.

## 6. Phase 1B — COMPLETE (2026-06-25)

> **Setting up the 3060 Ti? Follow `SETUP_3060TI.md` at the repo root** — the full machine/environment
> walkthrough and day-to-day operations reference.

All components are built and committed on `feat/two-machine-llm`. The full pipeline was proven end-to-end on 2026-06-24/25:

**`harness/node_agent.py`** (3060 Ti) — CLI `running|offline|train|status|heartbeat`; hardened train
sequence `DRAINING` → wait inflight→0 → `TRAINING` → run `TRAINING_COMMAND` → `EXPORTING` → `RETURNING`
→ `RUNNING` only after a live generation probe; `TRAINING_FAILED` keeps the node out of routing until
an explicit `running`. Heartbeat loop (`/tmp/hb_loop.sh`, every 4s) keeps the 45s TTL alive; does NOT
survive WSL restarts — see `SETUP_3060TI.md` for the restart recipe.

**`training/train_worker.py`** (Unsloth QLoRA, 8 GB 3060 Ti defaults, WSL2) + `training/requirements-wsl.txt`
+ `training/sample_config.json`. Emits `ARTIFACT_MANIFEST.json` + `.sync_complete` last. Syncthing
pushes the adapter back to the 9070 XT automatically.

**GGUF export pipeline** (`convert_gguf.sh` + `run_stage.py`) — merges LoRA adapter into base model,
quantizes to `q4_k_m` via llama.cpp, stages as `jclaw-worker:cand` in Ollama. Uses WSL-native `/tmp/`
for intermediate files (DrvFS `safetensors` atomic rename fails on `/mnt/c/`).

**Version stack (3060 Ti WSL2 training venv):**
```
torch 2.5.0+cu121
unsloth 2024.12.12  (--no-deps)    unsloth-zoo 2024.12.7  (--no-deps)
transformers 4.47.1                trl 0.14.0
accelerate 1.2.0                   triton 3.1.0  (3.2.0 breaks WSL2)
xformers 0.0.28.post2              python-dotenv 1.2.2
ollama 0.6.2
```

**Eval result (2026-06-25):** `eval_worker --deep` on the v001 adapter → `insufficient_evidence`
(`verify_attempted_n=0 < MIN_ATTEMPTED_N=20`). Gate working correctly — only 1 held-out row in the
8-row training set. To get a promotable verdict: 9070 XT runs `export_dataset` with ≥20 passing
projects, pushes via Syncthing, 3060 Ti re-trains + re-evals.

## 7. How to run / test

### 9070 XT (orchestrator)
```powershell
cd harness
python test_node_registry.py        # Phase 1A routing + no-paid invariant (13)
python test_export_dataset.py        # dataset export gates (5)
python test_eval_contract.py         # eval contract golden tests (7)
python test_eval_worker.py           # eval logic tests (10)
python -m training.export_dataset    # build the dataset from current verified projects
python -m training.eval_worker --candidate-model jclaw-worker:cand          # fast smoke
python -m py_compile config.py node_registry.py worker.py state_writer.py scheduler.py `
  evaluation_contract.py training/secret_scrub.py training/export_dataset.py `
  training/eval_worker.py training/promote_worker.py
```
Single-machine default (no sidecar configured) behaves exactly as before: every Ollama call routes to the
primary (`OLLAMA_HOST`), uncapped.

### 3060 Ti (trainer + sidecar) — see `SETUP_3060TI.md` for full detail

```bash
# Start heartbeat (run from Git Bash or WSL after each WSL restart):
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- bash -c "
  nohup bash -c 'while true; do cd /mnt/c/Users/Matthew/j-claw && python harness/node_agent.py heartbeat 2>/dev/null; sleep 4; done' > /tmp/hb_loop.log 2>&1 &
  echo Heartbeat PID \$!"

# Run training:
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- bash -c "
  cd /mnt/c/Users/Matthew/j-claw
  source training/.venv/bin/activate
  python training/train_worker.py --config training/sample_config.json 2>&1"

# Export GGUF + stage as Ollama candidate:
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- bash /mnt/c/Users/Matthew/j-claw/convert_gguf.sh
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- bash -c "cd /mnt/c/Users/Matthew/j-claw && source training/.venv/bin/activate && python run_stage.py"

# Run deep eval (must run from harness/ dir):
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- bash -c "
  cd /mnt/c/Users/Matthew/j-claw/harness &&
  OLLAMA_HOST=http://172.23.240.1:11434
  /mnt/c/Users/Matthew/j-claw/training/.venv/bin/python -m training.eval_worker --deep \
    --candidate-model jclaw-worker:cand \
    --candidate-hash <HASH_FROM_RUN_STAGE> \
    --base-model qwen2.5-coder:7b-instruct \
    --dataset /mnt/c/Users/Matthew/j-claw/jclaw-training/datasets/curated_v001.jsonl \
    --out-dir /mnt/c/Users/Matthew/j-claw/jclaw-training/evals 2>&1"
```
