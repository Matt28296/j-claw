# Session Handoff — Reliability Hardening Wave (2026-06-19, updated EOD)

Branch: `run/moba-monitored`. Operator: Matthew. **NOW PUSHED & IN SYNC with `origin/run/moba-monitored`** (`github.com/Matt28296/j-claw`) — `git rev-list --left-right --count` reads `0 0`. The earlier "local-only" status is superseded. ⚠️ **One caveat:** there are currently **UNCOMMITTED Wave 4 changes in the working tree** (8 `harness/*.py` files + 5 new `test_wave4_*.py`) from an in-flight orchestration — see the Wave 4 section. Do NOT commit/disturb the tree until that workflow reports its GO/NO-GO.

---

## ⏩ LATER UPDATE (2026-06-19, post-EOD) — Wave 3 DONE, Wave 4 IN FLIGHT

**Wave 3 (adversarial review) — COMPLETE.** Four adversarial reviewers re-examined the seven landed reliability commits. Two BLOCKERS were verified against live code (not just asserted). Theme: **nearly every serious defect fails specifically on the FORMAT-5 *decomposing* path — the path the MOBA takes.** Findings, by severity:
- 🔴 **C1 (BLOCKER)** — cost ceiling is per-SUB-PROJECT not per-build: `reset_costs()` re-arms at every `run_project` (`main.py:255`) and a tripped `BuildCostCeilingExceeded` is swallowed by `except Exception` at `main.py:795`. A 10-scene FORMAT-5 build can spend 10×$5=$50 unattended. **Directly contradicts the memory's "cost-ceiling ✅ for FORMAT-5."**
- 🔴 **C2 (BLOCKER)** — 3 metered Anthropic call sites bypass the ceiling (`final_review.py:88`, `handoff.py:227` stamp, `e2e_generator.py:181`); two also never `record_usage`. The stamp one runs on the failure-handoff path.
- 🔴 **H1 (BLOCKER)** — manual mode hard-returns `True` (`main.py:719`); a `--manual` build with failed tasks reports PASS.
- 🟠 Majors: **HK1** worktree path keyed only by `task_id` → husk regresses under concurrent builds; **HK2** worktree≠output_dir (why flat-build confidence doesn't transfer to FORMAT-5); **H2** pending/deadlocked tasks invisible to verdict; **H4** stamp false-green defaults; **CB4** cost accumulator unlocked under 4 parallel workers; **ENV** malformed env vars crash harness at import (`MAX_FORMAT5_DEPTH=0` disables all decomposition).
- ✅ Confirmed SOLID: manual-gate fail-closed (`1cc2836`), escape-valve recursion bound (`85d09d7`), $0-rung classification, asset `output_type` lock.
- 🟡 ClaudeCli (`8ad3ce6`): RISKY but **INERT** (`CLAUDE_CLI_ENABLED=false`) — defer to the pre-MOBA live smoke test; do NOT enable.

**Wave 4 (fix wave) — DONE + GREEN (UNCOMMITTED on the branch).** Ran as a 3-wave orchestration (build → review → integration). Approved plan: `C:\Users\Tyler\.claude\plans\what-is-the-order-cheerful-stardust.md`. Five build specialists worked disjoint file lanes directly on the branch (NO worktree isolation — that forks `main` and re-introduces the husk bug): A1 `main.py` (C1+H1+H2+H4; scheduler/project needed no change, helpers pre-existed), A2 the 4 metered-guard files (C2), A3 `cost.py` (CB4 lock), A4 `config.py` (ENV), A5 `worktree_manager.py` (HK1). Reviews: A1/A2/A4 PASS; A3 CONCERNS (test was tautological — repaired to force `setswitchinterval(1e-9)`, negative control drops ~40% updates unlocked); A5 CONCERNS (fix correct but namespacing broke 2 STALE tests in `tests/test_worktree_manager.py` that encoded the OLD bare-path invariant). Integration returned **NO_GO** on exactly that mechanical blocker. **Resolved by hand (this session):** updated the 2 stale worktree tests to the namespaced shape, and wired the 2 TestCase wave4 modules (`test_wave4_verdict`, `test_wave4_metered`) into the canonical `test_llm_layers.py` runner. **Now GREEN across all entry points:** `test_llm_layers.py` 180 OK (was 161), `tests/test_worktree_manager.py` 30 OK, `test_wave4_costlock`/`_env`/`_worktree` scripts all pass (cost-lock ✓ / env 34 ✓ / worktree 4 ✓) — ~244 tests, no regressions. Every code-level cross-check passed (build-global ceiling halts across sub-projects; C2 guards + A3 lock no deadlock; honest verdict reaches handoff; ENV floor; worktree cleanup targets namespaced dir). **COMMITTED + PUSHED as `236965d`** → `origin/run/moba-monitored` (branch `0 0` in sync; 17 files, +1468/−93).

Known non-blocking follow-ups: the 3 standalone wave4 scripts (`_costlock/_env/_worktree`) are run as scripts (not auto-discovered by the canonical runner) — fine for now, could be converted to `TestCase` later; A5's P2 husk-minors (non-atomic `_copy_tree`, missing Windows read-only handler in `_cleanup_worktree`) remain out of scope; CB4 left `record_ollama_usage`/`record_oauth_usage`/`record_role_event` unlocked (telemetry only, no money-safety impact).

**Updated next steps:** (1) ✅ DONE — Wave 4 committed + pushed (`236965d`); (2) **gate 3** — small NON-GAME FORMAT-5 decomposing build (supervised, metered) — proves husk fix on the decomposing path + exercises C1 end-to-end; (3) pre-MOBA hardening (ClaudeCli live smoke, kill-switch, structured logging); (4) re-attempt MOBA. The original priority list below stands as the task catalog (P0+P1 now all addressed; P2 items remain).

---

## TL;DR — focus is the SYSTEM (reliable unattended factory), the MOBA is just the stress test
All of the following are **DONE, committed on `run/moba-monitored`, suite 161 green** (`PYTHONUTF8=1 PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe test_llm_layers.py`):
1. **Husk bug — FIXED + verified** (copy-before-remove; git-merge dropped). Verified 3 ways: unit regression test, real flat build (tip calc — files landed + survived heal), clean `main`. ⚠ Still UNVERIFIED on a FORMAT-5 *decomposing* build (the path a game takes).
2. **#6 ORCHESTRATOR_PROVIDER** flipped gemini→anthropic in `.env` (engages difficulty router).
3. **#7 ClaudeCliOrchestrator** built + unit-tested + wired into emergency/medium chains. ⚠ NEVER live-validated (no real `claude -p` orchestrator JSON produced).
4. **Cost ceiling / circuit-breaker** (`cd13a2c`) — per-build `MAX_BUILD_COST_USD` (default $5), fails closed via existing handoff path; $0 rungs don't count. Guards at `worker._call_anthropic`, `Orchestrator.call`, `scheduler.run` batch boundary.
5. **#6-bis honest gates** (`760ec40`) — `main._build_disposition()` now fails the build if any task failed verification + exhausted retries (was ignored → false PASS); `notify.py` no longer shows a green ✅ when the OpenClaw stamp flags issues.
6. **#5 escape valve** (`85d09d7`) — over-scoped sub-projects can decompose one more level (activates the dead `MAX_FORMAT5_DEPTH=3` knob; `=1` restores old strict rule); removed the `orchestrator.txt` contradiction. Cost-amplification risk backstopped by #4.
7. **#3 asset output_type** (`18f90d5`) — confirmed already fixed (asset rides un-enum'd `stack`/task `type`); added regression-lock tests only.

These were executed via a **4-scout parallel investigation wave** (one specialist per task → exact blueprints), then applied by the orchestrator conflict-free (only shared file was `main.py`, non-overlapping functions). Worktree-isolated writer-agents were AVOIDED (they fork `main`, missing the husk/#2 commits — the husk failure mode).

## Earlier same session
- **#2 unattended manual-gate crash — FIXED** (`1cc2836`): `verification._run_manual` called `Confirm.ask()` → EOF-crashed unattended runs + burned the ladder. Now fails CLOSED with no TTY (absence of approval ≠ approval), clean logged return. `TestManualGateUnattended`.
- Rung-status dashboard feature (committed earlier this session range).

## Roadmap / next steps (in priority order — reliability of the unattended factory)
**Pending from the agreed plan (Wave 3 + Wave 4 of the user's spawn-teams request were NOT run yet due to rate limit):**
1. **Wave 3 — Review:** one adversarial reviewer per landed task ("find where it's wrong / doesn't actually work"). READ-ONLY, safe to parallelize.
2. **Wave 4 — Integration:** whole-system coherence + full suite + cross-task wiring (does the cost-ceiling actually fire in the scheduler path; honest verdict reaches handoff; escape-valve recursion bounded).
3. **Verify husk fix on a small NON-GAME FORMAT-5 decomposing build** (3–4 sub-projects) — proves the core fix on the real path + surfaces #3/#5 empirically at low cost. THE linchpin; metered, run supervised.
4. **Pre-MOBA hardening:** #7 ClaudeCli live smoke test; kill-switch / clean-abort (no orphaned worktrees); structured per-build logging.
5. **Re-attempt the MOBA** as the capstone integration test.

De-prioritized housekeeping: README touch-ups (below), worker.py:569 SyntaxWarning (`\.` in an nginx string), prune evidence worktree `task-015`, Netlify takedown of `jclaw-verify-tipcalc`.

## Docs / repo staleness audit (2026-06-19 EOD)
- **GitHub:** branch unpushed (see top). Decide whether to push `run/moba-monitored`.
- **README.md:** the lines previously flagged stale (`:153` worktree create/**merge**/remove, `:522` "all generated code lands", `:60` "output in projects/") are now MOSTLY ACCURATE since the husk fix — code DOES land. Remaining nit: `:153` still says "merge" but the fix DROPPED the git-merge (now copy-then-remove). Low priority 1-line correction; not yet done.
- **Memory roadmap:** `project_jclaw_state.md` / `project_moba_test_2026-06-18.md` — update to reflect Wave 2 done (cost ceiling, honest gates, escape valve, #2, #3 lock).

## Verify / run
- Tests: `cd harness; $env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; .\.venv\Scripts\python.exe test_llm_layers.py` → 161 pass (skipped=1).
- venv: `harness/.venv`. Default per-build cost ceiling is now $5 — raise `MAX_BUILD_COST_USD` in `.env` for a big build.
