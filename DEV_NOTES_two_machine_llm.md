# Dev Notes — Two-Machine Local-LLM Support (Phase 1A + Dataset Export)

> Status (updated 2026-06-25): **LOOP FULLY CLOSED end-to-end across both machines.** Committed on this
> branch (`feat/two-machine-llm`): Phase 1A routing, dataset export, `harness/training/` eval/promote +
> `evaluation_contract.py`, and **`harness/training/stage_candidate.py`** (LoRA→GGUF→Ollama candidate
> staging that bridges train_worker's adapter output to eval_worker's Ollama-tag input). The 3060 Ti ran
> `node_agent.py` + `train_worker.py` (QLoRA, WSL2). A real run executed
> export → Syncthing → QLoRA train → adapter → Syncthing → merge→GGUF (`stage_candidate`) →
> `eval_worker --deep` (A/B vs qwen2.5-coder:7b-instruct, local Ollama, RED/GREEN verifier) → gated promote.
> Deep-eval verdict on the 8-row smoke corpus: **`insufficient_evidence` / not-promotable**
> (`verify_attempted_n=0 < MIN_ATTEMPTED_N=20`), `no_paid_provider_called=true` — the evidence gate
> correctly refusing on thin data. For a REAL promotion: a larger curated dataset (≥20 attempted held-out
> rows) and prefer Path A (torch 2.6.0+cu124 + CURRENT unsloth, which matches train_worker's API — the
> older-unsloth pin caused serial API-drift fixes: get_tokenizer/FP8BackendType/xformers/triton).
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
| `node_agent.py` train/serve lifecycle | 3060 Ti | ⏳ deferred (Phase 1B) |
| `train_worker.py` (Unsloth QLoRA) | 3060 Ti (WSL2) | ⏳ deferred |
| `eval_worker.py` / `promote_worker.py` | 9070 XT | ⏳ deferred |

The 3060 Ti has **not been touched** — it currently exists only as a default hostname in config. The
sidecar stays dormant until (a) `LOCAL_LLM_NODES` points at its real endpoint and (b) it writes a
`RUNNING` state file (which `node_agent.py`, not yet built, will do).

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

## 6. Still deferred — 3060 Ti work (build when that machine is set up)
> **Setting up the 3060 Ti? Follow `SETUP_3060TI.md` at the repo root** — the full machine/environment
> walkthrough (Ollama, WSL2+CUDA, Syncthing, routing wiring) plus the two scripts to build below.

- **Phase 1B — `harness/node_agent.py`** (runs on the 3060 Ti): CLI `running|offline|train|status|
  heartbeat`; hardened train sequence `DRAINING` → wait inflight→0 → **stop Ollama serving** (not just
  `keep_alive:0`) → `TRAINING` → run `TRAINING_COMMAND` → `EXPORTING`→`RETURNING`→`RUNNING` only after a
  live generation probe; `TRAINING_FAILED` keeps the node out of routing until an explicit `running`.
- **`training/train_worker.py`** (Unsloth QLoRA, 8 GB defaults, WSL2) + `training/requirements-wsl.txt`
  + a trainer `sample_config.json`. Must emit `ARTIFACT_MANIFEST.json` + a `.sync_complete` marker last
  (so promote can hash a stable identity).

## 7. How to run / test (9070 XT)
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
