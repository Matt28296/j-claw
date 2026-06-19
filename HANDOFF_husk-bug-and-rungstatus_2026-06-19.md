# Session Handoff — Reliability Hardening Wave (2026-06-19, updated EOD)

Branch: `run/moba-monitored`. Operator: Matthew. **PUSHED & IN SYNC with `origin/run/moba-monitored`** (`github.com/Matt28296/j-claw`) — `git rev-list --left-right --count` reads `0 0`, working tree clean. (The earlier "UNCOMMITTED Wave 4" caveat is superseded — Wave 4 shipped as `236965d`.)

---

## ⏩⏩ LATEST (2026-06-19, late) — Gate 3 ran, cost-fix shipped, Phase 4 (minimal) shipped

Head of branch: **`190705a`** (`0 0` vs origin). Sequence since Wave 4:

1. **Gate 3 — RAN (supervised, metered) → honest FAIL.** Intent = non-game CLI devtoolkit. The Technical Architect scaled it to `mvp`/21 files, so it came out a **FLAT build and NEVER triggered FORMAT-5** — meaning **HK2 (husk on the *decomposing* path) is STILL UNPROVEN.** It honest-FAILed after 3 heal cycles (issues 13→14→11, never converged), cost **$0.85**, no ceiling trip, 120 files landed real, clean worktree teardown, ClaudeCli **live-validated** as a worker (6 tasks). ✅ All reliability machinery validated on the flat path (husk, HK1 namespacing, $0-ladder escalation, regression detection, honest gates / no false-pass, cost discipline). **KEY FINDING:** the build needed 60 tasks and still failed — the worker tier drifts on interface contracts in integration-heavy code → this is a **worker-quality/routing** problem (Phase 4), not a reliability regression.
2. **Free-rung caps retuned in `.env`** (gitignored): `claude_cli 10→40`, `codex 20→40`, `CODEX_PLANNING_RESERVE 6→12`, `grok 40` — all free caps now exceed the paid backstop (`MAX_PAID_WORKER_CALLS=15`), so $0 rungs deplete before any paid call. (The old `claude_cli=10 < paid=15` was an inversion: paid spend started while free subscription headroom remained. Caps kept finite — the $5 ceiling guards only PAID, so the call cap is the only free-rung circuit-breaker.)
3. **`0306d71` fix(cost):** `_handle_oversize` triangular-**double-counted** the reported aggregate cost on FORMAT-5 builds (operator-facing over-report; enforcement was correct). Found by reading the counter code; fixed (read build-global accumulator once) + regression test.
4. **`190705a` feat(routing): Phase 4 minimal slice.** `route_task` now starts integration-heavy tasks (`deps>0` + non-trivial type) at the first **$0 OAuth rung (grok)** instead of the weak local model. Safety: never lowers rung, never jumps to a *metered* rung on its own (no-OAuth ladder → local-first). Toggle `INTEGRATION_FIRST_ROUTING` (default on). Suite **187 green**.

**Settled plan order (debated w/ Codex):** A ✅ commit cost-fix → B ✅ Phase 4 minimal slice → **D1 NEXT: re-run the SAME Gate 3 flat intent with routing ON** (cheapest variable-isolated proof routing helped; metered but should cost < $0.85) → C `memory_lint.py` (staleness pre-flight) → D2 small live FORMAT-5 smoke (must explicitly validate HK1) → E MOBA. Rationale: critical-path-first, each step isolates one variable so failures stay diagnosable.

**Immediate next action:** D1 — supervised re-run of the Gate 3 flat intent (`projects/_gate3_clitoolkit` style) with `INTEGRATION_FIRST_ROUTING=true`; success = issue count drops/converges. Needs operator go-ahead (metered).

Housekeeping still open: prune the pre-existing stale `task-015` worktree (`C:\Users\Tyler\Desktop\.jclaw_worktrees`).

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
