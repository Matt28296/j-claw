# Session Handoff — Reliability Hardening Wave (2026-06-19, updated EOD)

Branch: `run/moba-monitored`. Operator: Matthew. Repo `github.com/Matt28296/j-claw`. **`origin/main` and `origin/run/moba-monitored` are both at `2eb2c05`** (Waves 1–5 are ON MAIN). **Local is 1 ahead = `33b451a` (free-CLI planning tier) — committed but NOT YET PUSHED**, and this handoff + README have uncommitted edits (push pending operator OK). Canonical suite **275 OK** (skipped=1) + worktree_manager 24 + standalone wave scripts.

---

## ⏩⏩⏩⏩⏩⏩ LATEST (2026-06-19, deep night) — Wave 5 hardening DONE+MERGED, free-CLI planning gap fixed, FORMAT-5 HK2 smoke RUNNING

Head: **`33b451a`** (`0 0` vs origin; main == branch). Commits since `eaf654e`:
`d750508` (PAID_ORCH_ENABLED kill-switch + D4 crash catch) → `f15cbc0` (3 metered-orch leaks gated) →
`2eb2c05` (**Wave 5**) → `33b451a` (**free-CLI planning tier**).

**Wave 5 — pre-MOBA hardening (`2eb2c05`), 3-wave orchestration (build → adversarial review → integration), disjoint file lanes, no worktree isolation:**
- **A** (`main.py`, `scheduler.py`): Scheduler is now a context manager → SIGINT / crash / `BuildCostCeilingExceeded` can't leave orphaned worktrees; `memory_lint` auto-wired warn-only on `--continue` + each FORMAT-5 sub-project start (doubly guarded, never blocks).
- **B** (`state_writer.py`): `_write_json_atomic` retries `os.replace` with backoff (~0.18s) on Windows `[WinError 5]` under concurrent writers; degrades gracefully + **warns on exhaustion** (diagnosability).
- **C** (`worktree_manager.py`): `_cleanup_worktree` + `create()` stale-dir both use an `onerror` chmod-retry handler (read-only git objects actually deleted on Windows); removed dead `merge_and_remove` (no live caller; husk fix uses copy-then-remove).
- **D** (`test_wave5_dryrun.py`): zero-network proof that `FORCE_FORMAT5=1` routes to the decomposing path and aborts honestly on a stubborn flat spec.
- Review wave: all 4 lanes **PASS, no blockers** (2 substantive nits applied by hand: B warn-on-exhaustion, C `create()` handler). Integration: all wired into the canonical runner, **275 OK**. Committed + **merged to main (fast-forward)**.

**Free-CLI planning gap — FIXED (`33b451a`).** Found live during the smoke: `planning_call` went Codex (free) → Anthropic (paid), skipping the existing `_claude_cli_tier`. So when Codex latched off, the Creative Director jumped straight to the metered Anthropic API (observed `400 credit balance too low` on the $0 box) instead of the free Claude Max CLI. Inserted **Tier 2 = `_claude_cli_tier`** between Codex and the paid tiers — both $0 rungs are now exhausted before any metered call. +2 regression tests (`TestPlanningCall` 8/8).

**FORMAT-5 HK2 smoke — COMPLETED (supervised, $0-enforced), HK2 INCONCLUSIVE / blocked on free-tier capacity.** Ran `FORCE_FORMAT5=1 MAX_PAID_WORKER_CALLS=0 PAID_ORCH_ENABLED=false` on intent "URL shortener: FastAPI backend + React/TS frontend + Python CLI" → `projects/_fmt5_smoke` (gitignored; dir since removed, log `harness/_fmt5_smoke.log` is the surviving record).
- ✅ **Decomposition TRIGGERED live** ("Oversize project — decomposing … must decompose via FORMAT 5") into 3 sub-projects (`backend_api`, `frontend_react`, `cli_client`) — first real proof of FORCE_FORMAT5 on the decomposing path (beyond the dry-run test).
- ✅ **Fail-closed proven.** With paid capped at $0 and both free tiers down, the pipeline spent **$0** and crashed honestly rather than reaching for the metered key. The cost guardrails worked as designed.
- ❌ **All 3 sub-projects CRASHED at spec generation** — every free orchestrator/planning tier was unavailable: (a) **Codex latched off** (OAuth/quota exhausted), and (b) **ClaudeCliOrchestrator failed validation: `claude -p exited 1`** — note the captured envelope shows `terminal_reason":"completed"` with empty `iterations:[]`/`modelUsage:{}`, i.e. the CLI process ran but returned no usable spec and/or the validator rejected it. Anthropic paid disabled → "credit balance too low" on the $0 box. CD was *skipped* (planning_call exhausted all tiers) for the same reason.
- ⏳ **HK2 NOT PROVEN** — builds died at spec generation before producing real code/`api_contracts.md`, so there was nothing to inspect for husk survival. Output dir is gitignored + now gone, so no post-hoc check possible. HK2 remains open; needs a re-run where at least one free orchestrator tier actually plans.
- 🔴 **NEW BLOCKER surfaced: the free-first chain has no working rung right now.** `33b451a` correctly *inserts* the Claude Max CLI tier, but the `ClaudeCliOrchestrator` (`claude -p`) rung itself fails validation in this env (exit 1 despite terminal_reason=completed). Combined with Codex latched, any $0-enforced build currently crashes at planning/spec. This — not reliability code — is what blocks HK2/MOBA.

**NEXT (operator decision needed):**
1. **Unblock a free orchestrator tier** — either (a) diagnose/fix `ClaudeCliOrchestrator` `claude -p exited 1` (likely invocation/output-format or validator mismatch — the CLI completes but returns empty content), or (b) wait for Codex OAuth quota to reset, then re-run the $0 smoke.
2. **OR** authorize a *budget-capped paid* run (`PAID_ORCH_ENABLED=true` + Anthropic credits) to prove HK2 once on the decomposing path — costs money, needs go-ahead.
3. Once a build actually generates specs + code → confirm HK2 (files survive in EVERY sub-project dir + `api_contracts.md` present+non-empty) → MOBA unblocked solo+supervised. HK1 concurrent-pair proof only needed before allowing two concurrent builds.
4. Housekeeping: `33b451a` (free-CLI planning tier + tests) is committed but **NOT pushed**; this handoff + README edits are uncommitted. Push pending operator OK. Plan: `~/.claude/plans/what-is-the-steps-virtual-sketch.md`.

---

## ⏩⏩⏩⏩⏩ EARLIER (2026-06-19, late night) — D3 ran (OAuth-bypass bug found+FIXED), D4 re-run proving it, + FORCE_FORMAT5 knob + memory_lint shipped

Head of branch: **`eaf654e`** (`0 0` vs origin, tree clean, suite **221 green**). Three commits since `85a9e81`: **`38baff1`** (OAuth fix), **`3498b2f`** (FORCE_FORMAT5), **`eaf654e`** (memory_lint). Supersedes the "NEXT: D3" plan below.

**D3 (Gate-3 flat CLI-devtoolkit, all prior fixes live) — RAN → BUILD CONVERGED 14/14 on $0 rungs, then both REVIEW stages died "credit balance too low".** This exposed the session's headline bug.

**🔑 OAuth-bypass bug — FIXED (`38baff1` fix(cost): final review + OpenClaw stamp run on OAuth, not the metered key).**
- *Root cause:* Claude Code's non-interactive auth precedence puts `ANTHROPIC_API_KEY` AHEAD of the subscription OAuth. Any `claude` subprocess that inherits the key silently meters the "free" call — and **fails "credit balance too low" because the metered ANTHROPIC_API_KEY account has $0 credits**. The worker rung already scrubbed the key (so the *build* ran free), but the two control-plane review paths did NOT: `final_review.py` called the metered API directly (no OAuth path at all), and `handoff.py`'s OpenClaw stamp passed raw `os.environ` to the `claude` CLI.
- *Fix:* new **`config.claude_cli_env()`** = single source of truth for the credential scrub (`CLAUDE_CLI_ENV_BLOCKLIST`); `handoff.py` stamp uses it; `final_review.py` gained a **CLI-first (OAuth) path** with the metered API only as fallback (and now reviews even on a box with NO API key); `worker.py` repointed at the shared helper. **+3 regression tests** (env scrub, overlay, final-review-prefers-CLI); fixed 2 tests that asserted API-only behavior.
- *Proof:* live smoke `claude --print` with scrubbed env → `OAUTH_OK` rc 0, no credit error. Suite 211 green at that commit.

**D4 (`blgkn8nej`, D3 re-run with the OAuth fix live) — RAN, IN PROGRESS at handoff time. The OAuth fix is CONFIRMED working.**
- Build **converged 20/20 tasks entirely on $0 rungs** (FORCE_FORMAT5 was OFF, so still a FLAT build — 20-task DAG, not decomposing).
- **Mid-build, grok AND codex BOTH exhausted their real auth/quota** (not the spurious-timeout bug — the latch fix `fda2e4d` was seen working live: *"grok timed out (transient) — NOT latching"*). The remaining tasks + ALL heal cycles rode the **claude_cli OAuth (Max subscription) rung** — graceful free-ladder degradation, **never touched paid, $0 throughout**.
- **Both review stages now run on OAuth:** log shows `Running final Claude Code review … → via claude CLI (subscription OAuth)` — the exact path that died in D3. **No credit-balance error.** ✅ Headline fix proven end-to-end.
- The OAuth final review **honestly flagged real issues** (5, then more: e.g. `pyproject.toml` missing `version`/`dynamic`) → heal cycles 1→2→3. Honest gate working (no false pass). Convergence is the SLOW part — same standing finding: **cost discipline airtight; convergence gated on worker output quality.** *(Final disposition + `est. cost` line not yet captured at handoff write — D4 still healing on OAuth at $0. Check tail of `harness/_d4_run.log`.)*
- **Process lesson:** do NOT spawn a Codex subagent (e.g. codex-rescue) DURING a build that relies on the codex rung — they share the operator's Codex OAuth quota and contend (observed: codex rung died right after a codex-rescue debate agent was launched).

**Two test-harness/reliability features shipped (settled via a design debate; Codex was rate-limited until Jun 20 00:29, so decided solo per the Codex-Gate-Fallback policy — reversible design → solo):**
1. **`3498b2f` FORCE_FORMAT5 knob.** D3/D4 came out FLAT because the TA scales intents down (~17–20 files). New `config.FORCE_FORMAT5` (default off) + `MIN_SUBPROJECT_COUNT` (default 3): at depth 0 only, inject a `decomposition_required` directive into the orchestrator payload; retry once if still flat; then **abort honestly** (a flat build wouldn't exercise FORMAT 5). Sub-projects (depth>0) never forced. +3 tests.
2. **`eaf654e` memory_lint.py** — warn-only `project_memory/` staleness pre-flight. Checks: `missing_file_citation`, `contract_no_source` (api_contracts route absent from source tree — high value: FORMAT-5 sub-projects SHARE api_contracts across worktrees), `orphan_meta`. **Never mutates/blocks**; writes `project_memory/lint_report.json` so unattended warnings aren't lost; always exits 0. Standalone CLI + `lint_project_memory()`. **Proven on real D3 memory** (caught `project_summary.md` citing `src/devtoolkit/cli_rename.py` + test files never built under those names). +7 tests. *Trigger-wiring into `--continue` + FORMAT-5 sub-project-start is a documented follow-up — module + CLI are done, the auto-hook is NOT yet wired.*

**Settled plan for the FORMAT-5 smoke (the linchpin — still the ONLY HK2 proof):**
- ① **Sequential** FORMAT-5 smoke with `FORCE_FORMAT5=1` (fresh dir `projects/_fmt5_smoke`) → proves **HK2** (husk on decomposing path) + flow. Success = goes FORMAT-5 **and** files survive in EVERY sub-project **and `api_contracts.md` present+non-empty in every sub-project output dir**.
- ② **Controlled concurrent pair** (same output parent, different names) → proves **HK1** (a CONCURRENT-builds bug — a sequential run CANNOT prove it). **PAUSE before ② — needs operator go-ahead, run supervised** (concurrency stresses the husk hardest).

**NEXT (immediate):** finish watching D4 to its honest verdict + `est. cost` (still running, $0). Then **item (3): launch the sequential FORMAT-5 smoke** (`FORCE_FORMAT5=1`, `projects/_fmt5_smoke`) once D4 releases the worktrees (never concurrent with D4 — HK1 risk). Then pause for go-ahead before the concurrent HK1 pair. Then pre-MOBA hardening → MOBA.

**Open findings (NOT fixed):** (1) Windows `mission_control.json` atomic-rename race (`[WinError 5]`) — cosmetic, retry-with-backoff before MOBA-scale concurrency; (2) memory_lint trigger auto-wiring; (3) codex-subagent-during-build contention (process discipline).

---

## ⏩⏩⏩⏩ EARLIER (2026-06-19, night) — D1 take-2 RAN → 3 cost/reliability fixes shipped + pushed; verdict = cost-disciplined but worker-quality-gated

Head of branch: **`85a9e81`** (`0 0` vs origin, tree clean, suite **208 green**). Repo deep-verified this session: remote `github.com/Matt28296/j-claw`, `.env` confirmed **untracked + gitignored** (real ANTHROPIC/GOOGLE keys safe), no stray untracked files to commit, all run artifacts (`projects/`, `.venv`, `*.log`) properly ignored. **Three commits shipped + PUSHED today.** Supersedes the "D1 take-2 NEXT" plan below.

**D1 take-2 (`brn7zfroz`) — RAN supervised, FAILED to converge.** Heal regressed **13 → 14**; 7 tasks failed all the way up the ladder. **Root cause = worker output quality, NOT cost machinery:** grok (the $0 integration rung) emits chat-wrapper JSON (`{"response":...}` / `{"output":...}`) instead of the `{"files":[...]}` contract → tasks never satisfy the validator → endless heal churn. **Cost discipline HELD** (build stayed within budget) but convergence is now provably gated on worker contract-adherence, not reliability bugs.

**Three fixes shipped (all pushed `0cce294..85a9e81`):**
1. **`fda2e4d` fix(reliability): don't latch a free OAuth rung on a lone timeout.** A single subprocess timeout (`TimeoutExpired`, e.g. one 300s claude/codex/grok call) was permanently latching that **free** rung off for the *entire* build via `_oauth_unavailable`, cascading all downstream work onto **paid** Anthropic. Now a timeout only latches after `OAUTH_TIMEOUT_LATCH_THRESHOLD` (default **2**) **consecutive** timeouts, and a success **resets the streak** (`_should_latch_oauth` / `_note_oauth_success`, per-provider counter under `_oauth_lock`). Non-timeout failures (auth/quota) still latch immediately. +82 test lines.
2. **`10f3ec5` fix(worker): salvage single-file output from chat-wrapper JSON shapes.** Directly targets the proven D1 take-2 blocker. `_salvage_single_file` now recovers content from `{"content"|"code"|"file"|"response"|"output"|"result"|"answer"|"text": ...}` wrappers (extracting a fenced code block from the value when present) instead of failing the whole task. Multi-file and input-echo shapes are still correctly rejected. +52 test lines.
3. **`85a9e81` fix(cost): cap paid orchestrator/planning calls (`MAX_PAID_ORCH_CALLS`).** D1 take-2 produced **38 paid orchestrator calls = $0.81** because both free orchestrator rungs latched and every heal re-plan fell to paid Sonnet — the orchestrator/planning paid path had **only the $5 cost ceiling, no call-count cap**. New `MAX_PAID_ORCH_CALLS` (config default **12**, `=12` if unset) gated by `_reserve_paid_orch_call()`, wired into BOTH `Orchestrator.call()` (orchestrator.py) and `planning_call`'s Sonnet→Opus tier (worker.py). Counter resets in `reset_paid_budget()`. +60 test lines.

**Open finding (recorded, NOT fixed):** Windows atomic-rename race — `[WinError 5] Access is denied` on `mission_control.json.<pid>.tmp -> mission_control.json` under concurrent writers (4 parallel workers + dashboard). Intermittent; cosmetic (write retries) but should get a retry-with-backoff or per-writer temp path before MOBA-scale concurrency.

**NEXT (immediate): D3** — re-run the Gate 3 flat CLI-devtoolkit intent with ALL fixes live (free-first orchestration + pyproject build + latch + salvage + orch-cap). Purpose: prove (a) the salvage fix lets grok tasks actually converge, (b) latch no longer dumps work onto paid, (c) the orch-cap holds spend near $0. Metered, needs operator go-ahead. Then **C** `memory_lint.py` → **D2/gate3** small live FORMAT-5 decomposing smoke (still the only HK2 proof) → **E** MOBA.

---

## ⏩⏩⏩ EARLIER (2026-06-19, evening) — D1 RAN → regressed → 2 cost defects found + FIXED + pushed

Head of branch: **`f385162`** (`0 0` vs origin, working tree clean, suite **191 green**). This session ran D1, killed it on an operator cost directive, and shipped two fixes. **Supersedes the "D1 NEXT" plan in the section below.**

**D1 (re-run Gate 3 flat intent, `INTEGRATION_FIRST_ROUTING=true`) — RAN supervised, then KILLED mid heal-cycle-2.**
- **Convergence: 9 → 13, REGRESSING** (vs baseline 13→14→11). Routing gave a **cleaner first pass (9 issues vs baseline 13)** ✅ but **did NOT fix convergence** ❌ — the "Heal loop regressing" detector fired (9→13, 28% overlap). Same non-convergence story as baseline.
- **Routing placement worked:** integration tasks (`deps>0`) correctly started on the `$0` grok rung; `deps=0` stayed local (ollama). But grok's output-JSON discipline is weak → heavy reliance on codex-escalation; that churn also **exhausted `CODEX_PLANNING_RESERVE` (12)** at the orchestrator layer.
- **HK2 (husk on the *decomposing* path) STILL UNPROVEN** — D1 was again a FLAT build.

**Two defects surfaced, both causing avoidable METERED spend — BOTH FIXED in `f385162` (committed + pushed):**
1. **Orchestrator free-first hole** — `make_orchestrator(difficulty="complex")` built a **paid Sonnet→Opus composite with NO free rungs**. When the heal loop bumped difficulty to `complex` on REVIEW_FAILED, every re-plan went straight to **paid Anthropic, bypassing available `$0` Codex/Claude-Max**. *This was the literal "free Claude bypassed while available."* Fixed → `complex` is now free-first (Codex → Claude-Max → paid Sonnet→Opus **last-resort backstop**), mirroring the `medium` branch. (`medium` and the emergency chain were already free-first.)
2. **`verification.py` python-build bug** — the `python` ecosystem `build` was hardcoded to `pip install -r requirements.txt`, but `detect_ecosystem()` also returns `"python"` for **pyproject.toml-only** packages → guaranteed failure (no requirements.txt) → made `task-016` **unwinnable**, burning the worker ladder **up to the paid rung** on a task no model could pass. Fixed → new `_run_python_build()` handles both manifests (like the FastAPI path).
- New regression tests lock both (191 OK, +4 since `190705a`).

**Cost policy set by operator directive: FREE-FIRST, PAID-LAST-RESORT.** `.env` (gitignored): `MAX_PAID_WORKER_CALLS` was unset (silently **15**) → briefly **0** (over-correction) → now **5** (bounded genuine last-resort; `$5` ceiling still caps dollars). The free OAuth caps (`=40`) are **loop-breakers + shared-Max-pool guards, NOT paid-pushers** — safe to raise codex/grok if hard builds need headroom; keep `claude_cli` moderate (shared with interactive Claude Code).

**Housekeeping DONE:** `.jclaw_worktrees` pruned empty (incl. the stale `task-015` + the killed-D1 `task-027` orphan + their `wt-task-*` branches).

**NEXT (immediate): D1 take-2** — re-run the SAME Gate 3 flat intent now that both fixes are live. Purpose: (a) validate free-first orchestration + the pyproject build fix end-to-end (currently unit-tested only, NOT live-proven), and (b) get a CLEAN convergence number (the first D1 was confounded by the bypass + the unwinnable `task-016`). Metered but should stay on `$0` rungs; needs operator go-ahead. Then **C** `memory_lint.py` → **D2** small live FORMAT-5 smoke (still the only way to prove HK2) → **E** MOBA.

---

## ⏩⏩ LATEST (2026-06-19, late) — Gate 3 ran, cost-fix shipped, Phase 4 (minimal) shipped — ⚠ SUPERSEDED by the section above (D1 has since RUN)

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
