# Session Handoff — J-Claw + OpenClaw

Date: **2026-06-16/17, ninth session** (previous: eighth 2026-06-16, seventh 2026-06-15, sixth 2026-06-14/15, fifth 2026-06-13/14, fourth 2026-06-13, third 2026-06-12, second + morning 2026-06-12, first 2026-06-04). Operator: Matthew (Windows acct "Tyler"/GitHub TylerBeats).
Two systems:
- **OpenClaw** = Telegram bot front-end (routing only). Config: `C:\Users\Tyler\.openclaw\`
- **J-Claw** = the build pipeline. Code: `C:\Users\Tyler\Desktop\Jarvis-Claw\harness\`

**PRs #10–#113 are MERGED to `main`** (role-routing overhaul Phases 0–3: #92/#94/#95/#96, + Grok rung #91, + corrective fixes #98, + docs syncs #99–#102, + dead-`groq`-config removal #89, + CD validator hardening #103 + routing-review plan amendments #104, + orchestrator Gemini **per-day** quota latch + free-first Codex→Sonnet→Opus chain + offline Matrix agent-dashboard + j-claw OAuth-tier/token tracking **#105** (squash `3b71f54`), + **#107** Claude Max CLI OAuth worker rung `claude_cli` (squash `b4b7c62`, live-validated; ships inert in-repo but **now enabled in the operator's `harness/.env`**), + **#111** PR-#105 follow-up cleanups (squash `e78a62d`); **#106/#108/#109/#110/#112/#113** are docs syncs). Phases 0–3 were then audited by a 5-agent review team + Codex; the corrective fixes landed in **#98 (`811bab9`)**. Phase 4 (interpretation-risk routing + per-role quotas) is next. **Open PRs: #73** (DRAFT operator WIP salvage — leave parked).
Direct push to `main` is intentionally blocked — land changes via PR.

---

## ⏭️ NEXT — APPROVED ROADMAP (2026-06-17): Claude-Code-style upgrades

Prioritized via a Codex debate. **Full plan:** `C:\Users\Tyler\.claude\plans\after-verifying-that-the-vast-narwhal.md`. Each item is its OWN PR; **none started yet.**

Order most→least important:
1. **#5 Append-only replayable session log** — FIRST. The observability substrate; you can't safely gate/tune an unattended system you can't replay.
2. **#6 Action-risk classification + enforcement gateway** — one choke point scoring every dangerous op (install/deploy/git/delete) by blast radius.
3. **#1 Permission modes** — `read_only`/`ask_before_write`/`auto_safe`/`dangerous_skip`; a *separate policy layer* over #6 (ship together, design apart).
4. **#3 Git-worktree isolation per worker** — robustness/verify-before-merge, NOT core safety.
5. **#2 Hybrid patch editing** — strong rungs only; full-file stays default for weak Ollama workers (a global migration would fight the local-first reliability principle).
6. **#4 Connector capability-registry** — a small internal registry first, NOT full MCP (a trap for a single-operator system).

**Recommended first milestone:** #5 + a **logging-only** action gateway (classify + log, don't block yet; set gate thresholds from real evidence).
**Before the first impl PR:** re-scan the safety + worker-editing execution surfaces — 2 Explore agents hit the Claude session limit mid-planning, so that mapping is grounded in session knowledge + the Codex debate rather than a fresh sweep.

---

## ✅ DONE 2026-06-17 (ninth session continued) — PR-#105 follow-up cleanups (PR #111, MERGED squash `e78a62d`)

The four deferred follow-ups from the PR #105 review, implemented after a Codex second opinion +
a Codex implementation review (verdict: SAFE TO MERGE):
- **#6 (Codex-tier dedup)** — `CodexOrchestrator` and `worker.planning_call` duplicated the whole
  Codex protocol and had drifted on JSON parsing. Extracted a NEW `harness/llm_json.py`
  (`strip_fences` / `fix_json_strings` / `loads_tolerant` / `loads_llm_json_object` — the last
  preserves BOTH tolerances: trailing prose AND in-string literal newlines) and `worker._codex_tier`
  (shared Codex-only tier raising `_CodexTierUnavailable` / `_CodexTierInvalid`; `planning_call`
  catches → Anthropic, `CodexOrchestrator` converts → RuntimeError). A `reserve_attempt` callback
  preserves the exact check→reserve→increment order for `CODEX_PLANNING_RESERVE`.
- **#4** — worker schema-fail (`ValueError`) path now persists tokens (was drain+discard) so a
  task that ultimately schema-fails no longer under-reports usage.
- **#7** — 2s TTL cache on `/api/agents` (`_agents_payload`, mirrors `git_panel`) + an
  `_invalidate_agents_cache()` on the cancel/kill paths so a control action isn't masked by the
  cached snapshot.
- **#8** — five near-identical `apiClient.js` fetchers collapsed into one `_request()`.
- Tests: +5 `llm_json` parser tests + 1 schema-fail token-persist regression; `TestCodexOrchestrator`
  telemetry patch repointed to `worker.record_role_event`. Suites pass SEPARATELY (the repo's way):
  `test_llm_layers` 104/1, `harness/tests` 13, `test_agent_dashboard` 24, `test_mission_control` 8.
- **Known latent gap (NOT fixed, own follow-up):** running `test_llm_layers.py` *combined* with
  `harness/tests/` in one pytest process trips a pre-existing `TestRoleCutover` prompt-path
  FileNotFound (CWD pollution); reproduces on the unchanged tree. Run suites separately; fix later
  with a conftest CWD guard / absolute prompt paths.

---

## ✅ DONE 2026-06-17 (ninth session continued) — Claude Max CLI OAuth worker rung (PR #107, MERGED squash `b4b7c62`; ships inert in-repo, ACTIVATED in operator's .env)

Optional third $0 OAuth worker rung `claude_cli` mirroring the Codex/Grok rungs: headless `claude -p`
under the operator's **Claude Max** subscription, serving the same Sonnet/Opus models otherwise reached
via the metered Anthropic API. Changes **how** Claude-tier work is billed, not **when** a task escalates.
In `worker.py` (`_call_claude_cli`, `_extract_claude_text`, `_is_claude_cli_unavailable`, reservation +
`_claude_cli_disabled` latch) + `config.py` (`CLAUDE_CLI_*`, `OAUTH_PROVIDERS`). Built → reviewed/debated
with Codex → live-validated, in that order.

- **Codex review caught a critical bug:** the subprocess inherited `ANTHROPIC_API_KEY`, which Claude
  Code's auth precedence puts AHEAD of the subscription OAuth non-interactively — so the "free" rung
  would have silently billed the METERED API. Fixed: env scrubbed of `_CLAUDE_CLI_ENV_BLOCKLIST`.
- **Hardened constraint posture** (not a denylist): `--tools "" --strict-mcp-config --setting-sources ""
  --disable-slash-commands --no-session-persistence` + worker prompt via `--system-prompt-file` (the
  task JSON on stdin alone). `claude -p` is ALWAYS the full Claude Code harness — there is no bare-model
  path to the Max subscription — so it's constrained to behave as a pure generator.
- **Live smoke test (2026-06-17)** found `--safe-mode` is REJECTED by claude 2.1.179 despite being in
  `--help` (replaced with `--setting-sources ""`). Then confirmed: AUTH ✓ (API key scrubbed, call still
  succeeded → subscription), CONTRACT ✓ (clean `{"files":[...]}`, `is_error:false`, `num_turns:1`).
- **Ships inert in the repo** (`CLAUDE_CLI_ENABLED=false`, not in the default `WORKER_LADDER`) — but
  **ACTIVATED 2026-06-17 in the operator's `harness/.env`**: `CLAUDE_CLI_ENABLED=true` + `claude_cli::sonnet`
  inserted into `WORKER_LADDER` at rung 4 (after `codex::gpt-5.5`, before the metered `anthropic::` rungs),
  alongside the already-live Grok/Codex rungs. **Live-tested engaging end-to-end** via a forced-escalation
  run through the real `execute_task` ladder: a task climbed deepseek→grok→codex→**claude_cli**, `claude -p`
  ran live and returned a valid `{"files":[...]}` contract, **0 metered calls** (subscription billing),
  oauth reservation/cap respected, real usage tokens recorded. NB it only engages on genuine escalation
  (a task all four lower rungs failed) — rare on small builds, by design.
- **Enable-gate caveats that remain the operator's watch:** the usage-limit latch is unit-tested only
  (watch it trip cleanly on the first real limit hit); confirm the Max usage dashboard shows subscription
  usage with **no metered API charge**; ToS is the operator's call (a personal Max sub powering an
  automated build farm is a risk boundary — prefer Team/Enterprise or Console API billing for commercial
  use). Shares the interactive Max quota (cap=10, placed below Codex/Grok). 7 mocked tests; suite 98/1.

---

## ✅ DONE 2026-06-17 (ninth session continued) — orchestrator quota latch + free-first chain + offline dashboards (PR #105, MERGED squash `3b71f54`)

A two-team in-session agent swarm implemented three changes; after a rate-limit reset I verified them
directly (ran the full suites rather than re-spawning), caught and fixed the one regression the
verification team would have flagged, and wired the one integration gap the implementers self-flagged.
All committed on `feat/agent-mission-control` → **PR #105, MERGED** to `main` (squash `3b71f54`).
Reviewed (high-effort multi-agent) + Codex-debated post-merge; three fixes landed with it (quota-class
429 narrowed to per-day only so transient per-minute throttles don't latch; Codex planning reserve
made resettable; hardcoded Opus id → `config.OPUS_MODEL`). Verified: **136 passed, 1 skipped** across
all four suites.

### Orchestrator — Gemini quota latch + free-first Codex→Sonnet→Opus chain (`orchestrator.py`, `worker.py`, `config.py`, `main.py`)
- Quota-class 429 detection (`_is_quota_class_429`): a daily `RESOURCE_EXHAUSTED` latches Gemini off for
  the run (`_gemini_quota_disabled` + `GEMINI_QUOTA_FAILFAST` gate) and raises immediately so
  `CompositeOrchestrator` drops straight to the emergency chain — no chain-walk, no `retryDelay` sleep.
  Transient throttles / 5xx / timeouts and OpenRouter keep the legacy behaviour (gated on the Gemini
  subclass's `_quota_failfast`, default `False`).
- `CodexOrchestrator` (validate + retry) added; `CompositeOrchestrator` generalized from a single
  emergency to an ordered chain; `make_orchestrator` rewired **free-first** Codex→Sonnet→Opus. Grok left
  out (evidence-gated). `CODEX_PLANNING_RESERVE` added to config.
- `reset_orchestrator_run()` clears the per-run latch and is now wired into **both** `run_project` and
  `run_continuation` start, beside `reset_paid_budget()` — so neither budget nor latch leaks across runs
  (harmless no-op under the current subprocess-per-run model; closes the trap if anyone adds an
  in-process caller — confirmed with Codex).
- **Regression fixed:** two stale `__new__`-based test fixtures in `test_llm_layers.py` never set the new
  instance attributes (`_pinned_model`; `_quota_failfast`/`_provider_name`) that `call()` reads and
  production `__init__` provides — they blew up with `AttributeError`. Mirrored the defaults into both
  fixtures (test-only fix; production was correct). +14 new orchestrator/Codex tests.

### Dashboards — offline agent-dashboard + j-claw tiers/tokens (`agent_dashboard.py`, `agent_dashboard/`, `dashboard/index.html`, `state_writer.py`)
- Agent dashboard de-Tailwinded and split into `css/` + `js/`; **fully offline** (grep for
  `https?://`/CDN/googleapis across the assets → zero hits). Per-LLM token rollup on `/api/agents`.
- j-claw dashboard: Grok mislabel fixed via `_OAUTH_PREFIXES` (checked before cloud) → purple
  `OAUTH $0` badge; rung badges remapped (R0–1 local, R2–3 oauth, R4 sonnet, R5+ opus). Per-task
  `tokens_by_model` persisted to `mission_control.json` + rendered in the drawer/cost rollup. Honest
  caveat: Grok/Codex CLIs report 0 tokens today, so the entry is a "this model ran" marker until they
  expose counts.

### Commit structure (debated with Codex → 3 commits, not 4)
`4b4ed81` orchestrator (incl. test fixtures + new tests — they share `test_llm_layers.py`, so a separate
"fixtures" commit would split one bisect unit into two broken states), `4539b88` dashboards, + this docs
commit. Next: Phase 4 (interpretation-risk routing + per-role quotas) on its own branch.

---

## ✅ DONE 2026-06-16/17 (ninth session continued) — CD validator hardening (PR #103) + routing review captured as plan amendments

A routing-design thread (my analysis) plus a **critical Codex second opinion** produced one shipped
change and a set of revised plan amendments. Nothing here changes runtime routing yet except the
validator; #2–#5 are written into the plan doc for Phase 4 / Phase 5 to implement.

### PR #103 (OPEN, branch `fix/cd-validator-hardening`) — harden `CreativeDirector._validate`
Proposal item #1, the one piece with no reason to wait (self-contained to `creative_director.py`, no
cost/API surface change). The CD validator was the **only** gate on the CREATIVE_BRIEF contract the
Technical Architect consumes (downstream Python never branches on `output_type` by name — it's a
prompt-enforced soft contract), yet it only checked that `output_type` existed + `features` was
non-empty. A malformed brief passed silently and mis-routed the whole build.
- `_validate` lifted to a module-level pure function (mirrors `technical_architect._validate`).
- Enforces, against `creative_director.txt`'s declared contract: `output_type ∈
  {film,game,app,website,code}`; `scale ∈ {prototype,mvp,production}`; `features` count band (1–30);
  non-empty `visual_identity` for non-`code` outputs (code prompts are explicitly exempt per the
  prompt's minimal-defaults allowance).
- A failing check raises `ValueError` → `planning_call`'s existing fallback boundary (Codex → retry →
  Sonnet → Opus).
- **Two corrections to the proposal as written:** (1) the enum is `website`, NOT `web` — the proposal
  and two old tests had it wrong; verified against `creative_director.txt`. (2) **Intentional
  tightening:** a brief omitting `scale` now *escalates* (was: silent `mvp` default) — required
  because Phase 4 difficulty routing keys on `scale`.
- Tests: new `TestCreativeDirectorValidator` (11 cases incl. enum, count boundary, code-exempt) +
  updated two `TestRoleCutover` cases off the stale `"web"` value. **Suite 75 passed / 1 skipped.**

### Routing review → plan amendments (in `everything-should-be-set-idempotent-cupcake.md`, "Amendments")
Codex verdict: **sound-with-caveats** — the role split is directionally right, but product-scale is
too blunt as a CD routing signal and the proposal underweights semantic cross-checks between
artifacts. Decisions (REVISED from the original proposal; the plan's original target table is kept as
the decision trail):
- **#2 (REVISED)** — route CD by **interpretation-risk**, not product-scale. Codex's decisive
  counterexample: a "prototype" (cinematic command-center incident-response tool) is a harder
  *interpretation* than a "production" CRUD dashboard — scale routes those backward. **Structural
  blocker found:** the existing difficulty rating is computed *after* CD (`main.py:249`) from CD's own
  `scale`, so it cannot route CD — a NEW **pre-CD signal from raw intent** is required. Committed shape
  for Phase 4: a cheap **deterministic** risk heuristic ($0, no LLM, testable like the validator)
  scoring ambiguity / novelty / constraint-load; `scale` becomes one feature, not the axis.
- **#3 (REVISED)** — Opus-CD as an *escalation* on high-ambiguity / low-confidence, not just a
  validator fallback (validators catch shape, never strategically-poor-but-valid briefs).
- **#4 (REVISED)** — TA stays Codex-first, but escalate on architecture-risk signals (auth /
  persistence / RBAC / real-time / compliance) the allowed-stacks validator can't catch.
- **#5 (REVISED)** — Opus production-orchestrator (Phase 5), but gate the spend on upstream semantic
  checks passing first; drop "production = uniformly harder to orchestrate."
- **#6 (NEW, DEFERRED → telemetry-gated "Phase 6")** — LLM risk classifier + **cross-artifact
  semantic checks** (prompt↔brief↔spec↔DAG) + explicit `assumptions` fields + a semantic-vs-schema
  telemetry split. The semantic-check layer is an LLM-judge that needs its own failure-mode design
  before it gates anything; parked behind Phase-0 evidence.
- **Principle refinement:** "upstream beats downstream" — the artifacts form a chain; don't spend Opus
  late to paper over an upstream semantic defect.

---

## ✅ DONE 2026-06-16 (ninth session) — Grok OAuth worker rung LIVE + role-routing overhaul started

### Grok Build CLI OAuth worker rung (PR #91, MERGED `9cfc354`) — LIVE, $0
A second flat-rate OAuth worker rung, **Grok-first**: the live ladder is now
`qwen3:8b → deepseek-coder-v2:16b → grok::grok-build → codex::gpt-5.5 → sonnet → opus` (grok before
codex — abundant/weaker first, scarce/stronger second). Headless `grok -p -m grok-build
--output-format json` authenticates via the cached `~/.grok/auth.json` OAuth token (operator's
SuperGrok sub, matthew.t.a@hotmail.com) — **NO xAI API key, $0 marginal.** The earlier "no $0 headless
path" worry was overturned by the May–June 2026 Grok Build update (headless/device-code OAuth;
`XAI_API_KEY` is only a fallback). Setup done live: Grok Build CLI 0.2.54 installed
(`irm https://x.ai/cli/install.ps1|iex`, binary `~/.grok/bin/grok.exe`), logged in via
`grok login --device-auth`; a real `_call_grok` returned the exact `{"files":[...]}` contract at $0
(~6.7s). worker.py: `_call_grok` (single-flight `_grok_call_lock` — xAI rotates the OAuth refresh
token per use; isolated scratch cwd; UTF-8 forced), `_extract_grok_text` (unwraps the `.text`
envelope), `_is_grok_unavailable` (a transient 429 throttle does NOT latch — unlike codex; only
permanent auth/quota/exe failures latch). Enabled live in `harness/.env` (`GROK_CLI_ENABLED=true`).
NB: the real model id is **`grok-build`** (not the plan's assumed `grok-build-0.1`).

### Role-model routing overhaul — APPROVED PLAN, Phases 0–3 done, 4–5 pending
Plan (designed via 2 Codex design passes + a 2-round adversarial Codex review):
`C:\Users\Tyler\.claude\plans\everything-should-be-set-idempotent-cupcake.md`. Philosophy: maximize
local execution; exhaust free OAuth (Grok→Codex) before metered Anthropic; front-load reasoning into
planning (difficulty-routed: `prototype→Haiku`, `mvp→Codex`, `production→Sonnet/Opus`) to REDUCE
avoidable worker ambiguity (NOT eliminate Anthropic — environmental surprises survive any plan);
distill each strong-model rescue into a reusable local lesson.
- **Phase 0 (PR #92, MERGED `d2ddab1`)** — per-role instrumentation baseline in `cost.py`
  (`record_role_event` → `cost_summary()["roles"]`: attempts/success/schema_fails/fallbacks/latency +
  per-provider success ratios; `anthropic_avoided` = free-OAuth successes), wired into orchestrator /
  CD / TA / final-review / worker (record-only, NO routing change), persisted to mission_control.json.
  Suite 53 green. Gates all later phases via before/after metrics.
- **Phase 1 (PR #94, MERGED `ad4e3f7`)** — learning-loop distillation: `log_escalation` stores rich
  lesson fields (solution_technique/prompt_hint/…); `get_worker_hints` ranks techniques BEFORE
  warnings; `_parse_and_validate` enforces the strict file-entry boundary + extracts an optional
  top-level `lesson` (Codex must-fix #1 — never writable as a file). In-schema capture, no extra
  paid call. Suite 56 green.
- **Phase 2 (PR #95, MERGED `6fd8339`)** — `planning_call(system, user, validate_fn)` helper landed
  **inert**: Codex → 1 same-tier retry → Sonnet → Opus, each gated by validate_fn; Codex draws the
  shared OAuth reservation/latch; never hard-fails on Codex quota (always falls back to Anthropic).
  Suite 61 green.
- **Phase 3 (PR #96, MERGED `0c7fccf`)** — Creative Director + Technical Architect now route through `planning_call`
  (Codex-first), preserving their required-field / allowed-stack validation as the fallback boundary.
  `_call_anthropic` gained a `label` param so CD/TA Anthropic fallbacks attribute cost correctly.
  NB: this DROPS Haiku as the CD/TA primary — on the operator's box (Codex enabled) they now plan at
  $0 on Codex; if Codex is ever disabled they fall to Sonnet (pricier than the old Haiku, but more
  reliable for strict-schema planning — the documented trade-off). Suite 63 green.
- **Review pass + corrective fixes (PR #98, MERGED `811bab9`)** — a 5-agent review team + an independent Codex
  verification audited Phases 0–3 (Phases 1/2/3 + integration SOUND: learning-loop + telemetry chains
  connected end-to-end, no circular import, groq confirmed dead, no dormant Phase 4/5 code). Fixed:
  (1) `planning_call` now records real `latency_s` (was zeroed for CD/TA); (2) `CompositeOrchestrator`
  no longer double-counts/phantom-successes the emergency hop (premature record dropped — the emergency
  orchestrator records the real outcome); (3) CD/TA constructors no longer hard-require
  `ANTHROPIC_API_KEY` (the old guard blocked key-free Codex-first planning) + dead
  client/imports/`_strip_fences` removed. DEFERRED to Phase 4: the emergency-model override is
  ineffective (`make_orchestrator` patches `config.ORCHESTRATOR_MODEL` but `Orchestrator.call` reads the
  module import) — no-op today (both default to Sonnet), fixed when Phase 4 reworks orchestrator model
  selection. Suite 64 green.
- **Pending (one PR each, dependency-ordered — they share worker.py/orchestrator.py/config.py so
  cannot be parallelized):** P4 difficulty routing + per-role Codex quotas (`CODEX_PLANNING_RESERVE`,
  hard non-lending sub-caps, decrement-on-start) + `CodexOrchestrator` + evidence-gated Haiku→Grok
  triage → P5 cut INIT/DAG onto the router (last, highest blast radius).
- Per-cycle cost expectation: clean `mvp` ≈ $0 (Codex plans free, local executes); `production` ≈
  $0.10–0.30 (paid planning only); problem-heavy ≈ $0.30–0.70, hard-capped ~$1 by
  `MAX_PAID_WORKER_CALLS=15`.

---

## ✅ DONE 2026-06-16 (eighth session continued) — film test #4 unblocked: project-type + stills-to-motion fixes (PRs #86, #87, MERGED)

Test #4 (the noir film run) had **two distinct failure modes**, both now fixed. Per the handoff's earlier note, test #4 was "gated only on a real end-to-end run, not a known harness bug" — with these two PRs merged, **that is now fully true: no known harness bug blocks it.**

- **PR #86 (MERGED `632813a`) — wrong project type.** The architect was scaffolding film scenes as **Python CV projects** (OpenCV/numpy code) instead of media-generation tasks. Fixed so film briefs route to the asset/video media pipeline, not a code project.
- **PR #87 (MERGED `913ba46`) — throughput / stills-to-motion contract.** Per-frame SDXL generation is infeasible on this DirectML host (~16s/still → a 6s scene at 24fps ≈ 39 min, far past the 600s task timeout) — this is why test #4 scenes never rendered. Changed the contract in `harness/worker.py` (film-director worker prompt) and `orchestrator.txt` (FORMAT 5 rule 21): the **asset task produces 1–3 STILLS**; the **video task ANIMATES them** with ffmpeg `zoompan` (Ken Burns) + `xfade` to fill the scene duration. The exact ffmpeg recipe is embedded in the commit, hand-verified against a real 6s noir clip built from 2 stills (proof artifacts were validated then discarded as scratch).

These complement PR #71 (RAM/ffmpeg-cwd/synthetic-render-guard). **#71 = pipeline completes; #86 = right project shape; #87 = it renders within the timeout.** Next step is a real end-to-end test #4 run (factory-rehearsal item #3).

**Housekeeping (2026-06-16):** The #87 proof artifacts (`harness/_motion_proof/` — 2 input stills, the 6s noir render, extracted Ken-Burns/xfade frames) were validated, then **deleted as scratch**. A gitignore rule `/harness/_*` now covers underscore-prefixed scratch dirs/files under `harness/` (it rode upstream on `origin/main` via the PR #88 handoff-sync merge — a concurrent session scooped the chore commit into its branch, see [[feedback-concurrent-sessions]]). Also removed the stray `harness/_test4_noir.log`. Working tree clean, `main` = `origin/main`.

---

## ✅ DONE 2026-06-16 (eighth session continued) — Codex rung FULL live validation + UTF-8 stdin fix (PR #84, MERGED `527438e`)

An earlier partial live test (see the backend-smoke-test section below) ran a *direct* `_call_codex` with a benign ASCII prompt → valid JSON. That passed but was **incomplete** — it never routed a *real worker prompt* through `execute_task`. Doing that exposed a blocking bug:

- **Bug:** `_call_codex` called `subprocess.run(..., text=True)` with **no `encoding=`**, so on Windows stdin was encoded as the locale default (**cp1252**). Real worker prompts contain non-cp1252 glyphs (arrows, bullets, box chars — the same `▶`-class chars seen earlier this project). `codex exec` reads stdin **strictly as UTF-8**, so it rejected **every real prompt**: `Failed to read prompt from stdin: input is not valid UTF-8 (invalid byte at offset N)`. The rung would have failed on every real task — the ASCII test passed only because it dodged the failing bytes.
- **Fix:** `encoding="utf-8", errors="replace"` on the subprocess (also hardens the stdout/stderr decode side).
- **Verified live, end-to-end:** `execute_task` → codex rung → `model_used=codex/gpt-5.5`, correct `hello.html`, `$0` oauth telemetry, ~12s. Added a regression test asserting `encoding='utf-8'`. **Suite 41 green.**

**This is the real close-out of the "never run live" caveat — the rung is now validated through the full `execute_task` path, not just a direct call.** Lesson: a live smoke test must exercise the *real* prompt path; a benign hand-written prompt hid a production-blocking encoding bug.

---

## ✅ DONE 2026-06-16 (eighth session continued) — film-pipeline robustness fixes (PR #71, MERGED)

Factory rehearsal test #4 (the noir film run) failed three times; root-caused and fixed the
harness-side pipeline. All fixes verified on real ffmpeg / real data; **53 tests green.**

### Pipeline fixes (4 commits, Codex-reviewed)
- **`scheduler.py` — RAM OOM fix** (`663878b`): mixed DAG waves now run asset (ComfyUI) and
  code (Ollama/deepseek) tasks in **sequential sub-batches**; ComfyUI's resident checkpoint is
  freed (`asset_worker.free_comfyui_models()` → ComfyUI `/free`) before the ~8 GB code model
  loads. Warns if ComfyUI is up but won't free. Was: deepseek OOM ("requires 8.2 GiB") mid-heal.
- **`video_worker.py` — ffmpeg cwd + binding** (`4f31841`): ffmpeg now runs with
  `cwd=output_dir` (absolute), so render scripts' relative inputs (`frames/%05d.png`) resolve.
  This — not deepseek's script quality — was why scenes kept failing. Also: bind each declared
  output to the ffmpeg line that names it (fail closed when ambiguous), join `\`-continued
  multi-line commands, and only overwrite the output token when it's actually the output path.
- **`video_worker.py` — synthetic-render guard** (`b93f837`, hardened in `51370d1`): a film
  render that sources video from a synthetic `lavfi`/`color=`/`testsrc`/`smptebars` generator
  while real ComfyUI frames exist is now **failed** (so the heal loop rewrites it to encode the
  frames) instead of passing grey placeholder video. Detection tokenizes `-i` inputs (catches
  `color=black`/`color=gray`, not just `color=c`); synthetic `aevalsrc` audio beds still allowed.
- **`worker.py` + `orchestrator.txt` — frame contract** (`51370d1`): the code-worker film prompt
  previously told the worker to use synthetic `color=`/`testsrc` sources and NOT reference frame
  files — directly contradicting the guard and preventing heal convergence. Rewritten to mandate
  encoding the real ComfyUI frames (`-framerate <fps> -i frames/<pattern>.png`); synthetic video
  only when no frames exist. Added the working-directory contract + one-ffmpeg-per-output rule.

### Image-gen blocker — observed earlier, NOT reproducing on the current config (reconciled 2026-06-16)
When this PR was authored, ComfyUI frames came back as **RGB noise/static** and the cause was
attributed to `torch-directml` computing SDXL incorrectly on the RX 9070 XT (RDNA4/gfx1201), with
a planned ROCm migration. **A later same-session verification contradicts that for the current
setup:** after restarting ComfyUI on a clean `--directml` with the `RealVisXL_V5.0_fp16` checkpoint,
a fresh `_comfyui_txt2img` render produced a **clean, coherent noir frame** (verified by viewing the
PNG — see the backend smoke-test section). So:
- The image backend is **working on the current config** — ROCm migration is a **contingency, not a
  confirmed requirement**.
- The earlier noise may have been checkpoint-specific (the noir scene at one point resolved to the
  `animagine-xl-3.1` **anime** model) or a transient `torch-directml`/state issue.
- If noise recurs in a real build, the documented fix path remains ROCm (native Windows ROCm 7.2.1
  PyTorch in ComfyUI's env, or WSL2+ROCm fallback) + a photoreal checkpoint.

The **harness fixes above stand regardless** of the backend question — they're why the pipeline now
**completes** (cwd/RAM/guard) instead of failing on relative frame paths or passing grey placeholder
video. Test #4 is now gated only on a real end-to-end run, not a known harness bug.

---

## ✅ DONE 2026-06-16 (eighth session continued) — every backend smoke-tested green + runtime brought up for the film test

Pre-flight before factory-rehearsal test #4: each worker/media stack was exercised in isolation so a failure shows up here, not 20 minutes into a build. **All six green:**

| Stack | Test | Result |
|---|---|---|
| Image (ComfyUI / DirectML) | real `_comfyui_txt2img` noir frame | ✅ 541 KB PNG in ~32s |
| Audio (Piper) | real narration WAV | ✅ non-silent |
| Music (FluidSynth) | real jazz score WAV | ✅ non-silent |
| Video (ffmpeg) | render + ffprobe | ✅ valid streams |
| Code — local (Ollama) | qwen3:8b generation | ✅ + deepseek-coder-v2:16b loaded |
| Code — Codex OAuth (gpt-5.5) | **first-ever live `codex exec`** (direct, ASCII prompt) | ✅ valid JSON in ~9s |

**This direct call works live — but note it was only a partial validation.** It used a benign ASCII prompt and did NOT route through `execute_task`, so it missed a UTF-8 stdin bug that the *full* path then exposed and fixed (PR #84, see the top section). The media smoke tests (`tests/test_media_workers.py`) are 6/6.

**Runtime up:** Ollama (:11434, both rungs), ComfyUI (:8188, `--directml`), j-claw Telegram bot (`bot.bat`→`start_bot.py`, sole poller, `getWebhookInfo` clean). Dashboard (:8765) auto-starts on build.

**⚠️ OpenClaw shares the j-claw Telegram token** (`8853236488`, @JarvisClaw96bot) — Telegram allows only ONE `getUpdates` poller, so the two bots CANNOT run simultaneously (→ `telegram.error.Conflict`). To run the j-claw `/run` test cycle, OpenClaw was fully stopped. **Stopping it took three steps, not one:** disable the `OpenClaw Gateway` scheduled task, kill the persistent `C:\Users\Tyler\openclaw-watchdog.ps1` (it respawns the gateway the instant it dies — disabling the task alone was NOT enough), then kill the `openclaw.mjs gateway` node proc on :18789. **Restore OpenClaw after testing:** `Enable-ScheduledTask -TaskName "OpenClaw Gateway"` + re-launch the watchdog ps1. (Clean long-term fix: give the two bots SEPARATE tokens.)

**Status: environment is fully ready for test #4.** No stack will silently fall back to a placeholder.

---

## ✅ DONE 2026-06-16 (eighth session continued) — Codex OAuth rung hardening per second-opinion review (PR #81, MERGED `ac63575`)

Codex gave the merged PR #79 an independent second-opinion review: **verdict "the change looks solid"**, no correctness-breaking bug, three hardening items. All three applied + merged in PR #81:

- **Medium (latch/reservation atomicity)** — `worker.py`: the `_codex_disabled` latch check now lives INSIDE `_reserve_oauth_call`, under the same `_oauth_lock` as the capacity bump. Previously the gate read the latch and reserved capacity as two separate steps, so under parallel workers one worker could read the latch False, another flip it True after an auth/quota failure, and the first still launch `codex exec`. Now atomic. The gate keeps only the cheap `CODEX_CLI_ENABLED` short-circuit (config constant, no race).
- **Low (over-broad classifier)** — `_is_codex_unavailable` dropped the bare `"login"` substring (it could flag a genuine capability failure — e.g. a task writing a `LoginForm` echoed on a nonzero exit — as "unavailable" and wrongly skip the rung). Kept specific phrases: `"please run codex login"`, `"login required"`, `"run codex login"`, etc.
- **Low (failure telemetry)** — `_call_codex` now records `success=False` on any failed invocation. `calls` = attempted invocations; `success` = how many returned — so the auth/quota failures that trip the latch are now visible in `cost_summary()["oauth"]` / the dashboard.

**Also:** pinned `CODEX_CLI_ENABLED=True` in `TestCodexWorkerRung.setUp` — the suite previously depended on the operator's untracked `harness/.env` (a hermeticity gap the reviewer flagged in a separate, crashed pass: on a clean checkout/CI the flag defaults False, making the routing assertions vacuous). Added a bare-`"login"` false-positive test + a failed-call telemetry test. **Suite 40 green.**

> ⚠️ **Review-tooling note (worth remembering):** the Codex rescue review had an orphaned-process bug — a job whose process died ~3.5 min in still reported `status: running` for 30+ min because the companion computes `elapsed` as now-minus-start and never noticed the exit (same failure class as the bot-restart orphan). When watching a Codex job, watch the **log file's write-time**, not the `elapsed` counter. Also: the rescue subagent launched TWO parallel passes on one shared runtime, which serialized them — prefer a single pass.

**~~Still the one real gap: never run live~~ — RESOLVED 2026-06-16 (PR #84, see the top section).** Validated end-to-end through `execute_task` (not just a direct call): `CODEX_CLI_ENABLED=true` + `codex login` confirmed, `model_used=codex/gpt-5.5`, valid file output, `$0` telemetry. The full-path test found + fixed a UTF-8 stdin bug. The remaining unknown is only its behavior under a *real build's* escalation load (parallel workers, capacity counter, latch) — which test #4 will exercise.

---

## ✅ DONE 2026-06-16 (eighth session continued) — Codex CLI OAuth worker rung (PR #79, MERGED `18c228c`)

**What:** an optional flat-rate worker rung that sits BETWEEN the strongest local Ollama rung and the paid Anthropic rungs. It bills against the operator's ChatGPT Plus/Pro subscription (OAuth, flat-rate) rather than per token — so escalations that would otherwise spend Anthropic dollars are caught for free first, and Anthropic becomes the true last resort.

**Why this is the right shape:** the worker ladder already escalates capability failures local → cloud. The missing tier was a strong-but-free model. Codex (gpt-5.5) on a subscription is exactly that — stronger than the 16B local rung, $0 marginal cost.

### Files changed (PR #79 MERGED as squash `18c228c`; later hardened by PR #81 — see above):
- **`config.py`** — `CODEX_CLI_ENABLED` (default `false`), `CODEX_HOME`, `CODEX_MODEL=gpt-5.5`, `CODEX_EFFORT`, `CODEX_CLI_MAX_CALLS=20`, `CODEX_TIMEOUT=300`. Default `WORKER_LADDER` gains a `codex::gpt-5.5` rung (inert unless enabled). New declarative provider-class sets: `METERED_PROVIDERS={anthropic,openrouter,groq}`, `OAUTH_PROVIDERS={codex}`.
- **`worker.py`** —
  - `_call_codex(model, system, user) -> str` mirrors `_call_ollama`'s contract: shells `codex exec --skip-git-repo-check --ephemeral -s read-only -o <tmpfile> -m <model> -` with the combined prompt on stdin, reads the clean final message from the temp file, records $0 telemetry, bounded by `CODEX_TIMEOUT`.
  - `_is_codex_unavailable(exc)` — classifies "skip to next rung" failures (FileNotFoundError, TimeoutExpired, 401/403/429, "not logged in", "quota", "rate limit") vs genuine capability failures (a bad-output `ValueError` returns False → does NOT skip).
  - Budget gate rewritten by provider class: METERED → `_reserve_paid_call()` (dollar budget); OAUTH → cheap `_codex_disabled`/`CODEX_CLI_ENABLED` short-circuit then `_reserve_oauth_call(provider)` (separate `_oauth_calls_made` counter capped at `CODEX_CLI_MAX_CALLS`); ollama ungated.
  - `_codex_disabled` module latch — first auth/quota failure in a run disables the rung so subsequent tasks skip it without re-probing (no interactive-reauth hang). `reset_paid_budget()` now also clears the oauth counters + the latch.
  - `_call_provider` routes provider `"codex"` → `_call_codex`.
- **`cost.py`** — `_oauth_usage` accumulator + `record_oauth_usage(provider, *, success, latency_s, tokens)` ($0, never touches `_total_usd`); `cost_summary()` gains an `"oauth"` key; reset in `reset_costs()`.
- **`state_writer.py`** — `on_cost()` normalizes the `oauth` block (per-provider calls/success/tokens/latency_s) into `mission_control.json`.
- **`dashboard/index.html`** — cost panel renders a `<provider> (oauth)` row showing `N calls · $0 · M tok`; the table now also shows when there are oauth rows even with zero cloud spend.
- **`.env.example`** — documented Codex rung block + an example ladder WITH the rung.
- **`test_llm_layers.py`** — `TestCodexWorkerRung`, **7 new mocked tests** (routing, unavailability classification, oauth ≠ dollar-budget, capacity exhaustion → escalate, `_codex_disabled` short-circuit, parse path, cost telemetry). NO subprocess/API runs. **Full suite 39 green.**

### Operator setup to actually use the rung (it's OFF by default):
1. Install the Codex CLI and `codex login` (ChatGPT Plus/Pro session).
2. In `harness/.env`: `CODEX_CLI_ENABLED=true` and add `codex::gpt-5.5` to `WORKER_LADDER` between the last `ollama::` rung and the first `anthropic::` rung.
3. If the worker subprocess can't see the interactive login, set `CODEX_HOME` to the Codex auth dir.
4. Safe to leave off — when disabled or unavailable the rung is skipped and the existing local→Anthropic ladder is unchanged.

**Verify:** `cd harness && ./.venv/Scripts/python.exe -m pytest test_llm_layers.py -q` → 39 passed.

---

## ✅ DONE 2026-06-16 (eighth session) — test coverage + style-aware image checkpoints (PRs #74–#76)

### PR #75 — Test coverage for media workers + mission control
Two new test files, **14 tests green** under `harness/.venv`:
- `tests/test_mission_control.py` (8) — `state_writer` terminal transitions (DONE / NEEDS_FOLLOWUP / FAILED / CANCELED / no-continuation), deploy/cost/review/dynamic-check recording, atomic-write temp-file cleanup, and the `dashboard.py` HTTP control endpoints (static serving, control-status, restart/continue/retry/cancel, 400s on bad requests, held-open-client regression).
- `tests/test_media_workers.py` (6) — genre/duration detection plus real Piper-TTS and FluidSynth WAV smoke tests that assert non-silent output and skip cleanly when the binaries/soundfont are absent.

### PR #76 — Style-aware ComfyUI checkpoint selection
`asset_worker.py` now picks the checkpoint from the brief instead of using one fixed model:
- `_detect_image_style(task, spec)` scans objective/goal/creative brief for keywords — anime/cartoon cues → anime checkpoint, everything else → realistic (the default; the noir-film case resolves to realistic).
- `_style_modifiers(style)` injects per-style positive prefix + extra negative quality tags.
- `_comfyui_checkpoint(style)` selection priority: explicit `COMFYUI_CHECKPOINT` override → style-matched model when installed → other configured model when installed → first available → preferred name (trusts config when ComfyUI's list is unreachable).
- `config.py`: `COMFYUI_CHECKPOINT_REALISTIC` (RealVisXL), `COMFYUI_CHECKPOINT_ANIME` (Animagine), tunable `COMFYUI_STEPS=26` / `COMFYUI_SAMPLER=dpmpp_2m` / `COMFYUI_SCHEDULER=karras`.
- `tests/test_asset_worker.py` — **12 pure-function tests** (style detection incl. default/tie/noir, modifiers, checkpoint fallback ordering with the availability probe monkeypatched). No ComfyUI required.

### PR #74 — gitignore bot runtime logs
`*.log` ignored (bot daemon logs can contain the Telegram token in API request URLs). The #75 branch had to be rebased to keep this broader rule rather than the narrower per-file version it originally carried.

### Also landed earlier (PRs #70, #72)
- PR #72 — reconcile orphaned runs so a killed/restarted bot can't freeze `EXECUTING` (the long-standing restart-orphan trap).
- PR #70 — remove `'orchestrat'` from goal-text assembly detection.

**New tests this session: 26 green (14 media/mission-control + 12 asset worker); full suite 38 green.**

---

## ✅ DONE 2026-06-15 (seventh session continued) — local Piper TTS + FluidSynth music (PR #68)

### PR #68 — Replace Coqui TTS + MusicGen with Piper binary + algorithmic MIDI/FluidSynth

**Problem:** The film stack had two placeholders in media generation:
- `audio_worker.py` depended on a Coqui TTS HTTP server (`localhost:5002`) — not installed
- `music_worker.py` depended on MusicGen/audiocraft — GPU-bound Python ML stack, not practical locally

**`audio_worker.py`** — rewritten to use the pre-compiled Piper TTS binary (stdin → WAV, no GPU, ~0.26× realtime on CPU). `can_generate()` returns True when `PIPER_BINARY` and `PIPER_VOICE` both exist on disk. Narration text extracted from `creative_brief.narration/voiceover/dialogue`, falling back to a cleaned task objective. Smoke test: 195 KB WAV from "A detective walks through rain-slicked streets" in ~0.4s.

**`music_worker.py`** — rewritten using pure-Python `midiutil` MIDI composition rendered via FluidSynth binary + FluidR3_GM.sf2 soundfont. Genre detection reads the creative brief for keywords → `jazz`/`horror`/`epic`/`romance`/`ambient`. Five genre composers, each producing a full MIDI arrangement:
- `jazz` (120 BPM): walking bass (GM double_bass ch0) + sparse Cm7 piano comps (GM grand_piano ch1)
- `horror` (60 BPM): tremolo strings cluster C2/C#2/D2 + pad swells every 8 beats
- `epic` (140 BPM): brass C-major arpeggio + sustained strings + kick/snare/hihat drum kit
- `romance` (72 BPM): strings C–Am–F–G progression + stepwise piano melody
- `ambient` (80 BPM): long pad swells + sparse electric piano notes

FluidSynth CLI argument fix: `-F`/`-r`/`-q` must precede the soundfont path. Smoke test: 5.5 MB jazz WAV (30s). Genre detection for "noir detective in 1940s Chicago" → `jazz` ✓.

**`config.py`** — added `PIPER_BINARY`, `PIPER_VOICE`, `FLUIDSYNTH_BINARY`, `FLUIDSYNTH_SOUNDFONT` config entries.

**Film stack is now fully local — no placeholders:**
- ✅ Image frames: ComfyUI + DirectML + animagine-xl-3.1 (PR #67)
- ✅ Narration audio: Piper TTS binary (this PR)
- ✅ Background music: midiutil + FluidSynth + FluidR3_GM.sf2 (this PR)
- ✅ Video assembly: ffmpeg 8.1.1 (on PATH from PR #15 era)

**8/8 tests green.**

---

## ✅ DONE 2026-06-15 (seventh session continued) — CANCELED state on /cancel (PR #65)

### PR #65 — Write CANCELED state to mission_control.json on /cancel

**Root cause (factory rehearsal test #5):** When `/cancel` was sent mid-build, `telegram_bot.py` terminated/killed the pipeline subprocess but never wrote a terminal state to `mission_control.json`. The killed process can't flush state itself — so the dashboard was left indefinitely showing the last `EXECUTING` snapshot (stale task counts, running agent, etc.).

**Fix:** Added `_write_canceled_state()` to `telegram_bot.py`, called immediately after the subprocess is killed. It patches `mission_control.json` directly from the bot process, mirroring what `StateWriter._set_terminal_state("CANCELED")` does: sets `pipeline_state` to `"CANCELED"`, clears `active_agent`, writes the `terminal` block, marks any running `agent_nodes` as canceled, appends a cancel event, and bumps `sequence`.

**Also discovered:** The pipeline has no impossible-intent detection — it generated a full 33-task DAG for the BCI/hologram request and began executing before the user cancelled. Factory rehearsal test #5 will need a revised definition (honest FAIL at review, not early rejection).

---

## ✅ DONE 2026-06-15 (seventh session continued) — worker quality rules (PR #63)

### PR #63 — Tailwind CDN conditional, event binding rule, manifest icon check, contact form guard

**Root cause (NES portfolio build, factory rehearsal #3):** Three systematic gaps in worker guidance were exposed:
1. `orchestrator.txt` line 232 explicitly *mandated* Tailwind CDN for the vanilla stack — workers followed the rule, but the rule was wrong for pixel-art / retro / custom-aesthetic projects where Tailwind is unused overhead.
2. No rule existed preventing the `element.addEventListener('submit', this.handleSubmit)` pattern — passing an unbound method reference loses `this` inside the handler, causing `TypeError: this.validateEmail is not a function`.
3. `completeness.py` parsed HTML `src=`/`href=` and JS string literals for missing asset references but did NOT parse `manifest.json` icon paths (JSON format, not HTML/JS).

**`orchestrator.txt`:** Tailwind CDN changed from MANDATORY to CONDITIONAL — only add it when the brief explicitly calls for utility-first or modern UI styling; never for pixel-art, retro, hand-drawn, or custom-aesthetic projects. Two new rules added: (1) DOM event listener binding rule — class methods registered as listeners MUST use an arrow function wrapper or bind in the constructor to preserve `this` context; (2) contact form rule — static vanilla projects must use `action="mailto:..."` or a descriptive placeholder comment, never `action="your-form-id"` or `formspree.io/f/REPLACE_ME` which silently 404.

**`harness/completeness.py`:** Added `_missing_manifest_icons()` — parses `manifest.json`, enumerates `icons[].src` entries, flags any declared paths that don't exist on disk. Wired into `check_completeness()` alongside the existing HTML/JS asset checks.

**32/32 tests green.** Branch: `fix/pr63-worker-quality-rules`.

---

## ✅ DONE 2026-06-15 (seventh session continued) — Ollama token tracking + connection error guard

### PR #60 — Ollama token tracking in cost panel

`harness/cost.py`: added `_ollama_tokens` accumulator (`input`/`output`), `record_ollama_usage()`, and `ollama_tokens` key in `cost_summary()`. Reset in `reset_costs()`.

`harness/worker.py`: `_call_ollama()` now reads `response.prompt_eval_count` + `response.eval_count` and calls `record_ollama_usage()`.

`harness/state_writer.py`: `on_cost()` normalizes `ollama_tokens.input/output` from the summary dict.

`dashboard/index.html`: "local (ollama)" row added below the cloud token row in the cost panel. Table renders even with zero cloud spend (condition broadened to `modelRows.length || ollamaIn || ollamaOut`).

### PR #61 — Ollama connection errors fail the task immediately (no cloud escalation)

**Root cause (discovered live):** The first Tony Montana v8 build attempt burned $0.50 in ~23 minutes because Ollama was DOWN. Every task started on qwen3 → `ConnectionError` → caught by generic `except Exception` → silently walked up the worker ladder to `claude-sonnet-4-6`. All 23+ completed tasks used Anthropic.

**Fix:** Added `_is_ollama_unavailable(exc)` helper in `worker.py` that distinguishes infrastructure failures (server unreachable) from capability failures (bad output, wrong JSON). Checks `ConnectionError`, `ConnectionRefusedError`, `OSError`, `httpx.ConnectError`, and string patterns ("connection refused", "cannot connect", etc.). When an Ollama rung raises an infrastructure error, the task raises `RuntimeError` immediately — no ladder walk-up, no cloud spend.

**Rule encoded:** Anthropic escalation is for capability failures only. A down Ollama server fails the task loudly; it does not silently bill the API.

`harness/test_llm_layers.py`: **32/32 tests green.**
- Renamed `test_rung_walkup_on_infra_error` → `test_rung_walkup_on_capability_error` (uses `RuntimeError` not `ConnectionError`)
- Added `test_ollama_connection_error_raises_immediately_no_cloud_escalation`
- Updated `test_execute_task_logs_escalation_on_fallback_success` to use `RuntimeError` (simulates a capability failure)

### Factory rehearsal runs (2026-06-15, seventh session)

`deepseek-coder-v2:16b` pulled and confirmed (8.9 GB). Both local rungs live: `qwen3:8b` ✅ + `deepseek-coder-v2:16b` ✅. Codex plugin for Claude Code installed (`/plugin install codex@openai-codex`) — adds `/codex:adversarial-review` for independent second-opinion PR reviews.

Two builds ran with the full local ladder in place:

**NES-style portfolio** — `Build a static, personal portfolio website with a retro 80s NES game aesthetic`
- 31 tasks · 1 heal cycle · HANDOFF: ISSUES REMAIN (broken contact form JS, missing PWA icons, Tailwind CDN loaded on vanilla stack)
- Telegram: "Review result: pass / Project complete"
- Deployed: https://jclaw-build-a-personal-portfolio-website-styled-like-a-r.netlify.app

**Tony Montana v8** — `Tony Montana Miami Vice fan site v8` (distinct intent to bypass idempotency guard)
- 44 tasks (31 original + 13 heal tasks) · 2 heal cycles · Final state: NEEDS_FOLLOWUP
- Completeness kept failing: hero `<section id="hero">` without `class="hero"` (PR #53 rule not consistently applied by local workers); dark-mode toggle with no CSS rule for the toggled class
- Cost: $0.60 — Ollama handled bulk (96k local input tokens vs 82k cloud); 8 paid calls
- Deployed: https://jclaw-build-a-personal-portfolio-website-styled-like-a-t.netlify.app

**Key result:** PR #61 protection confirmed — no silent cloud escalation on infra failure. Cost profile is local-first. Healer gap identified: the `#hero`/`.hero` class mismatch pattern survives all heal cycles — the healer generates fix tasks but workers don't reliably patch structural HTML selector issues without targeted guidance.

---

## ✅ DONE 2026-06-15 (seventh session) — factory rehearsal #2 + dashboard smart controls

### PR #55 — Dashboard context-aware control button state

`dashboard/index.html`: replaced blanket `btn.disabled = !controlAllowed` with per-action
availability checks via `isControlActionAvailable(action)`:
- **Restart** — only when prior intent exists (`hasPriorIntent`)
- **Cancel** — only during active pipeline states (`isActiveState`)
- **Retry** — only when failed tasks exist AND output dir is present
- **Continue** — only when output dir is present

`updateControlButtons()` called from `render()` on every state tick and from
`setControlAvailability()` on token entry. Guard in `runControl()` rejects stale clicks
with a toast. Agent network suppresses `active` CSS pulse once pipeline is terminal.

### Factory rehearsal item #2 ✓ — /continue fix flow end-to-end

Existing portfolio build (`Build_a_personal_portfolio_website_styled_like_a_T`) had two
issues flagged by the Claude stamp: hero class mismatch + dark mode no-op.

- Sent `/run Build a personal portfolio website styled like Tony Montana retro 80s`
- Bot hit **idempotency guard** → returned the old completed build instead of a fresh run
- Bot offered "Want me to fix them?" → operator accepted
- Bot ran `/continue`: fixed `<section id="hero">` → `<section id="hero" class="hero">`;
  confirmed `html.light-mode {}` was already defined in `variables.css` (original stamp
  reviewer had incorrectly flagged dark mode as missing — it was already wired)
- Redeployed to Netlify; site confirmed working visually by operator ("The website looks great!!")

**Live URL:** https://jclaw-build-a-personal-portfolio-website-styled-like-a-t.netlify.app

**What this validated:**
- `/continue` fix flow (factory rehearsal #2) ✓
- Deploy + redeploy idempotency ✓
- Hero class in deployed HTML ✓ (PR #53 rule held through the fix)
- Dark mode toggle + `html.light-mode {}` CSS wired ✓
- Site fully styled: 8 split CSS files, 4 JS modules, sticky nav, scroll animations ✓

**What was NOT validated:**
- Clean v8 run — idempotency guard prevented a fresh build. True v8 (workers following
  PR #53 rules from scratch, no post-hoc fix) is still outstanding. Use a distinct intent
  to bypass the guard: `/run Tony Montana Miami Vice fan site v8`

---

## ✅ DONE 2026-06-14/15 (sixth session) — Tony Montana validation + .git rmtree fix

### PRs #51–#54 merged — Tony Montana build hardening

| PR | Description |
|----|-------------|
| **#51** | orchestrator.txt: unique file ownership per task (second task silently overwrites first — workers always write the complete file); ban on generic monolithic filenames (`css/style.css`, `js/app.js`, `js/main.js`); CSS must split across focused named files (one task per file); JS must split by feature. Also fixed: HTML example in orchestrator.txt referenced banned filenames; `worker.py` vanilla service worker template hardcoded `./app.js` |
| **#52** | `completeness.py` stripping order fix — `_strip_comments_strings` was stripping line comments (`//`) before strings, so `'// All fields required.'` had its `//` stripped first, corrupting the string boundary and leaving `var(--neon-yellow)` exposed as an apparent bare JS function call. Fixed: strings stripped before comments. Belt-and-suspenders: CSS function names (`var`, `calc`, `env`, `min`, `max`, `clamp`, etc.) added to the bare-call allowlist |
| **#53** | orchestrator.txt: ID/class coordination rule — every `<section>` must carry BOTH `id` (anchor nav) AND `class` matching the CSS selector; a section with only `id` silently breaks all `.hero { }` rules. JS toggle class rule — any task that toggles a CSS class must name the class in its objective and depend on a CSS task defining rules for that class (a toggle with no matching CSS rule is a no-op). HTML example updated to show `<section id="hero" class="hero">` pattern. Direct fixes applied to v6 Tony Montana project: `index.html` missing `class="hero"`, and `css/variables.css` missing `html.light-mode { }` rules |
| **#54** | `main.py`: `shutil.rmtree` fails with `PermissionError: [WinError 5]` on the second run of any project because `git_commit_project` leaves a `.git` folder with read-only object files (Windows git behavior). Added `onexc=_force_remove_readonly` handler that `chmod`s files to `S_IWRITE` before retrying — same pattern git-for-windows uses internally |

**Tony Montana build timeline:**
- v1–v4: Failed (vitest qa burning paid calls, CSS monolith truncation, .git PermissionError on project cleanup)
- v5: Cancelled — 9 tasks all writing `css/style.css`, 3 writing `js/app.js` → PR #51 added unique file ownership rule
- v6: Completed (REVIEW: PASS). Two post-hoc rendering issues found by code review: hero section had no `class="hero"` (all `.hero {}` CSS rules silently broken) and dark mode toggle was a no-op (no `html.light-mode {}` CSS defined). Fixed directly in v6 project files + encoded as permanent orchestrator rules in PR #53
- v7: Failed immediately with `PermissionError: [WinError 5]` on `.git/objects/` during `shutil.rmtree` — PR #54 fixes this
- v8: Pending restart — will confirm PR #53 rules make the agent network handle hero class + dark mode correctly on its own

---

## ✅ DONE 2026-06-13/14 (fifth session) — Dashboard PRs, factory rehearsal item #1, orchestrator hardening

### PRs #44–#48 merged

| PR | Description |
|----|-------------|
| **#44** | Dashboard mission-control telemetry — agent network, task drawer, cancel/continue/retry controls, cost panel, rung badges, health bar, live test results, healing timeline, model display, heal badge fix |
| **#45** | orchestrator.txt: `render_scene.py` must call `subprocess.run(cmd, check=True)`, never `print(cmd)`; Windows ffmpeg constraints (`drawtext` unavailable, `geq=` in filter_complex fails, use `color=` solid backgrounds) |
| **#46** | orchestrator.txt: HTML stub prevention for vanilla stack — task that writes `index.html` must name every `<link rel="stylesheet">`, CDN `<script>`, and every page section by its HTML `id` + visible content |
| **#47** | Dashboard state wiring — `on_cost()` normalization (`total_usd`/`by_model`/`tokens`/`paid_calls`), `on_review_failed()` event emission ("REVIEW_FAILED" text for heal badge counter), `on_openclaw_stamp()` method wired from `handoff.py`; deploy URL was already implemented (review agents caught it) |
| **#48** | Orchestrator timeout fix — `_OpenAICompatOrchestrator` (Gemini) now passes `timeout=ORCHESTRATOR_TIMEOUT` to `chat.completions.create()` and catches `APITimeoutError` as an availability failure (triggers model fallback chain → CompositeOrchestrator Sonnet fallback). Fixes the indefinite hang when Gemini stalls |

### Factory rehearsal — item #1 ✅

`/run Build a simple personal portfolio website` sent via Telegram:
- Build ran, 3/3 heal cycles used, status ISSUES REMAIN (CSS not linked, Tailwind CDN missing — worker quality gap, not pipeline gap)
- Netlify URL deployed: https://jclaw-build-a-simple-personal-portfolio-website.netlify.app
- Telegram notification received with URL ✅
- Pipeline end-to-end (orchestrator → workers → deploy → Telegram) confirmed working

PR #46 (HTML stub prevention) was added this session to reduce the CSS-orphan failure for future builds.

### PRs #49–#50 merged — CDN stack unit-test guard + task size limit

| PR | Description |
|----|-------------|
| **#49** | Two-part fix for vitest QA tasks burning paid call budget on CDN-only projects: (1) `orchestrator.txt` rule 8 extended — `vanilla`, `phaser`, `three-js` stacks may NOT plan any `qa` task with `verification: "unit_test"` or `"smoke"` (no `node_modules`, no install step); (2) `harness/verification.py` — `node` ecosystem unit_test auto-passes when `node_modules/` is absent (defensive guard for builds that slip through) |
| **#50** | `orchestrator.txt` principle 2 extended — one file per task, ≤150 lines per file (a 14B model's reliable output window); CSS must never be a single monolithic file (split: `variables.css`, `reset.css`, `layout.css`, `components.css`, `animations.css`, `responsive.css`, one task per file); JS must never be a single monolithic file (split by feature: `js/scroll.js`, `js/menu.js`, etc.) |

**Root cause of these fixes — Tony Montana build failures (v1–v4):**
- v1–v3: QA task planned `test/qa.test.js` + `package.json` → `detect_ecosystem()` returned `"node"` → `npm test` ran → vitest not found → 4 retries burned 3+ paid calls → paid budget exhausted → `index.html` and JS tasks degraded to Ollama-only and failed → build landed as ISSUES REMAIN
- All builds v1–v3: `css/style.css` was a single monolithic file — deepseek returned wrong output format, Sonnet and Opus both truncated mid-JSON; all 4 retries failed; heal cycles couldn't recover
- v4: Build cleared old project dir and restarted, but crashed at git commit: `PermissionError: [WinError 5] Access is denied: .git\objects\...` — Windows Defender locking the project git directory at write time
- **Fix applied:** `Add-MpPreference -ExclusionPath "C:\Users\Tyler\Desktop\Jarvis-Claw\harness\projects"` (must run as admin once)
- Also installed vitest globally: `npm install -g vitest` (vitest 4.1.8)
- **v5 pending validation** — DAG expected to drop from 49 tasks → ~20–24 tasks (no vitest qa planned); CSS split across 6 focused files; paid call budget preserved. User needs to resend from Telegram.

### Factory rehearsal — item #2 🔄 in progress / replaced by Tony Montana build

`/continue Add a dark mode toggle` sent via Telegram. First attempt hung for ~24 minutes (Gemini timeout — fixed in PR #48). Second attempt sent; outcome unclear — superseded by a new `/run` before HANDOFF was written.

### Dashboard audit

Full cross-file audit (dashboard.py ↔ dashboard/index.html ↔ state_writer.py) run via Explore agent. Findings:
- **12 panels confirmed working:** state badge, stage tracker, active agent, agent network, health bar, task list, events, test results, error cards, work log, output files, task drawer
- **4 orphaned panels fixed in PR #47:** cost breakdown table, token display, heal badge count, openclaw verdict card
- **1 rejection:** deploy URL fix was already implemented (`on_deploy()` exists in state_writer.py and is called in main.py) — review agent caught the false positive

### Gemini hang root cause

`_OpenAICompatOrchestrator.call()` (used by GeminiOrchestrator) had no `timeout=` on its `chat.completions.create()` call. When Gemini stalled, the process waited indefinitely — the `CompositeOrchestrator` Sonnet fallback never triggered because no exception was raised. Fixed in PR #48: 300s timeout + `APITimeoutError` treated as an availability failure (same fallback path as 429/503).

---

---

## ✅ DONE 2026-06-13 (fourth session) — v7 film validation + dashboard health panel + cost panel

### v7 film validation — ffmpeg render path confirmed for the first time

Build ran against `film_validation_v7/` (root dir, not `harness/projects/` — PROJECTS_DIR=./projects resolved relative to CWD).

**PRs #39 + #40 both validated in production:**
- PR #39 depth guard: scene sub-projects stayed FORMAT 2, never re-decomposed.
- PR #40 cross-provider fallback: Gemini hit REVIEW_FAILED schema error 3× → automatic switch to `claude-sonnet-4-6` orchestrator. Console warning confirmed.

**Critical findings (workers consistently generate broken render_scene.py):**
1. **Workers print instead of run ffmpeg.** deepseek, Sonnet, and Opus all generated `main()` as `print(shlex.join(cmd))` or `print(cmd)` — producing no output.mp4. Harness reads render_scene.py output; printing the command produces nothing. **Fix needed: add rule to orchestrator.txt — render_scene.py must call `subprocess.run(cmd, check=True)`, never print.**
2. **Fontconfig not on Windows.** `drawtext` ffmpeg filter requires fontconfig; this Windows host has none → exit 0xC0000005 (access violation). Use `fontfile='...'` without `:` in the path is impossible (`C:` breaks option parsing). **Workaround: skip drawtext entirely.**
3. **geq in filter_complex fails.** Complex `geq=r='...':g='...':b='...'` expressions in filter_complex context return "Invalid argument" on this ffmpeg 8.1.1 build. **Workaround: solid `color=` background instead.**
4. **Working minimal approach confirmed.** Simple `color=c=0xff6b35` + `aevalsrc=0.1*sin(...)` — no filter_complex, no geq, no drawtext — produces a valid 304 KB output.mp4.

**Working render_scene.py (save as reference):**
```python
import subprocess, sys
CMD = ["ffmpeg", "-y",
  "-f", "lavfi", "-i", "color=c=0xff6b35:size=1920x1080:rate=24:duration=20",
  "-f", "lavfi", "-i", "aevalsrc=exprs=0.1*sin(2*PI*440*t):c=mono:s=44100:d=20",
  "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
  "-pix_fmt", "yuv420p", "-r", "24", "-t", "20", "-c:a", "aac", "-b:a", "128k",
  "-ar", "44100", "-ac", "2", "-movflags", "+faststart", "output.mp4"]
def main():
    try: subprocess.run(CMD, check=True)
    except subprocess.CalledProcessError as exc: sys.exit(exc.returncode)
if __name__ == "__main__": main()
```

**Gemini REVIEW_FAILED bug persists:** `'review_result' is a required property` — Gemini repeatedly omits it. PR #40 fallback catches it automatically, but the root cause should be filed/tracked.

### Dashboard enhancements — local commits d419403 + acb8fd4 (need PR)

Two commits on `main` locally, not yet pushed:

1. **`d419403` — cost & escalation panel** (cherry-picked from worktree agent — only index.html, backend deletions discarded):
   - 4th column in bottom row. Per-model token table from work_log. Total USD spend. Escalation counter, paid-call budget indicator. Collapsed by default.

2. **`acb8fd4` — rung badges + build health bar**:
   - Rung badges on agent nodes: R0/R1 green (Ollama local), R2 amber (Sonnet), R3 red (Opus).
   - Build health bar above agent network: colour-segmented done/running/failed/pending.
   - Health stats row: heal cycle count (from events), escalation count, active model label.

**Worktree agent hazard confirmed:** the cost-panel worktree agent also deleted ~600 lines from state_writer.py, dashboard.py, scheduler.py (removed `agent_nodes`, `updated_at_epoch`, `sequence`, `_MAX_ERROR_LOG_CHARS`, `_MAX_AGENT_NODES`). Never merge a worktree branch without reviewing ALL changed files, not just index.html.

---

## ✅ DONE 2026-06-12 (third session) — Gemini-literalism hardening + fallback layers + test suite (PRs #34–#41)

Theme: the film validation rerun finally ran — four times (v3–v6) — and each run caught a real
defect, all of the same species: **Gemini follows the prompt/schema literally where Claude
inferred intent.** The pipeline has still never reached the ffmpeg render path; that's v7.

| Run | Defect | Fix |
|---|---|---|
| v3 | Gemini 503 raises `InternalServerError` — only `RateLimitError` was caught, so the flash→flash-lite fallback never engaged and every scene crashed | **PR #34**: catch `InternalServerError` + `APIConnectionError` in both orchestrator retry loops (Gemini model-switch path AND Anthropic backoff path) |
| v4 | `project_type: 'film'` rejected — validator enum was `[web, app, game]`; prompt's stack table lists film but the enum didn't | **PR #35**: add `film` to validator enum + both prompt lists |
| v5 | Worker threads crashed on `'charmap' codec can't encode '▶'` — non-UTF-8 launch shell | env-only: launch builds with `PYTHONUTF8=1 PYTHONIOENCODING=utf-8` |
| v6 | Gemini free-tier quota exhausted (20 req/min flash-lite); **root cause: scene sub-projects re-decompose at the DAG stage** — `SPEC_ACCEPTED` returned FORMAT 5 and `main.py:361` accepts it with no depth guard (INIT has one; Claude never did this at the DAG stage) | diagnosed; fix is next session's P2 |

Also this session:
- **PRs #30–#33 merged** (Gemini orchestrator, docs, .env.example, dashboard green theme).
- **PR #36** — Opus 4.8 added as 4th worker-ladder rung (last resort). $5/$25/MTok = only
  1.67× Sonnet now. Tasks never START on cloud; Opus fires only after deepseek AND Sonnet
  failed the same task, carrying full error context. Live `.env` updated too. Decision:
  Opus is **worker-only** — availability failures (provider down) get cross-provider
  same-tier fallback instead (P3.4 below); capability failures escalate up the ladder.
- **PR #37** — Mission Control outage root-caused: `_start_dashboard()` spawned a new
  dashboard.py per build; Windows SO_REUSEADDR stacked **15 instances** on port 8765 and
  the connection lottery wedged the UI. Fix: TCP-probe the port, skip the spawn. (Operator
  note: if the UI goes quiet again, `netstat -ano | findstr 8765` — kill extras directly.)
- **Final review model decision:** stays Haiku; deterministic gates backstop it. Audit its
  verdicts during v7; bump `FINAL_REVIEW_MODEL` to Sonnet if it misses a stub.
- Dashboard WIP (mission-control telemetry in scheduler.py / state_writer.py / dashboard.py /
  index.html) is the operator's uncommitted work — left untouched in the working tree.

### Also completed this session (PRs #38–#41)
1. **PR #38 (docs)** — README + SESSION_HANDOFF synced for PRs #34–#37.
2. **PR #39 — DAG-stage decomposition guard + retry pacing:**
   - `SPEC_ACCEPTED` payload now carries `decomposition_allowed:false` + `sub_project_depth` when `depth > 0`; corrective retry + `return False` if Gemini insists. Fixed discarded `_handle_oversize` return + missing args on depth-0 path.
   - `orchestrator.txt` rule 21: FORMAT 5 now explicitly scoped to top-level INIT only; `SPEC_ACCEPTED` must always return FORMAT 2.
   - `_parse_retry_delay()`: reads Google `RetryInfo.retryDelay` "Ns" → OpenRouter metadata → plain-text regex → blind default. Verified against v6 error payloads (3s → 5s, 54s → 56s).
   - Result: ~6–8 orchestrator calls/build (was 18–24).
3. **PR #40 — emergency cross-provider orchestrator fallback:**
   - `CompositeOrchestrator` + `make_orchestrator()` factory. Gemini exhausted → Anthropic Sonnet automatically. Loud console warning names the fallback model + primary failure.
   - `ORCHESTRATOR_EMERGENCY_PROVIDER` / `EMERGENCY_ORCHESTRATOR_MODEL` env knobs. Default on when `ANTHROPIC_API_KEY` present.
   - Design: availability failures go sideways (cross-provider, same tier); capability failures go up the worker ladder (Opus, PR #36).
4. **PR #41 — `harness/test_llm_layers.py`:** 25 mocked tests, all green. Zero API spend. Covers: both orchestrator providers (all retry/fallback/error shapes), `CompositeOrchestrator`, `_parse_retry_delay` (all 4 shapes), `routed_rung` (4-rung Opus ladder), `execute_task` attempt chain (rung walk-up, `ValueError` short-circuit, paid-budget clamp, all-exhausted), final review fail-closed regression guard.

### What's still remaining after third session
1. **v7 film validation** — ✅ done in fourth session (see above).
2. **Factory rehearsal** — 7-item Telegram checklist. All green → "factory" status.

---

## ✅ DONE 2026-06-12 (second session) — cost optimization + Gemini free-tier orchestrator (PRs #28–#30)

Theme: same pipeline, fraction of the API bill. Per-build cost drops from ~$0.50 to an
estimated **$0.05–0.15** once #30 is merged (orchestrator free, overpowered roles on Haiku,
40–70% fewer orchestrator input tokens).

1. **PR #28 (MERGED) — role-model right-sizing + cache fix:**
   - Creative Director: Opus → **Haiku** (`.env`); Technical Architect: Sonnet → **Haiku** (`.env`).
   - Final Review: was hardcoded to `ORCHESTRATOR_MODEL` (Sonnet) — new `FINAL_REVIEW_MODEL`
     config var, defaults Haiku. Runs 1–4×/build (per heal cycle) doing stub/dep/syntax checks.
   - `e2e_generator.py`: was the ONLY Anthropic call without `cache_control` — fixed.
   - Orchestrator + worker-escalation rungs stay Sonnet. ~30–50% cheaper per build.
2. **PR #29 (MERGED) — orchestrator context-bloat elimination:**
   - `REVIEW_FAILED` payload: full 50-task list → `tasks_slim_list()` (deprecated omitted; done
     tasks reduced to `{id, files, status}`; failed keep type+objective). Was ~30–60 KB/heal cycle.
   - `EXECUTION_ERROR` payload: full `active_dag` → `dag_summary` `{total_tasks,
     highest_task_seq, dependents_of_failed}` — the three things FORMAT 3 actually uses.
   - `orchestrator.txt` FORMAT 3 + REVIEW_FAILED sections document the new shapes.
3. **PR #30 (OPEN) — Gemini 2.5 Flash orchestrator via Google free tier:**
   - `ORCHESTRATOR_PROVIDER=gemini` calls Google's OpenAI-compatible endpoint DIRECTLY with
     `GOOGLE_API_KEY` → AI Studio free tier applies (1M tokens/day). The same model via
     OpenRouter would bill — that's why it's a native path.
   - `OpenRouterOrchestrator` refactored into `_OpenAICompatOrchestrator` base;
     `GeminiOrchestrator` is a thin subclass (chain: gemini-2.5-flash → flash-lite).
   - `orchestrator.txt` opener made provider-neutral ("You are Claude" removed).
   - **Live-validated:** real INIT call returned a schema-valid FORMAT 1 vanilla spec.
   - Found in passing: `openai>=1.0.0` was in requirements.txt but NOT installed in the venv —
     the existing OpenRouter path would have crashed identically. Installed.
   - Local `.env` already set: `GOOGLE_API_KEY` + `ORCHESTRATOR_PROVIDER=gemini`.
   - ⚠️ **Operator: the Google key was pasted into a chat session — regenerate it at
     aistudio.google.com and update `harness/.env`.**
4. **Worker-ladder rung-1 upgrade:** `deepseek-coder-v2:16b` (MoE, 8.9 GB Q4_0,
   ~90% HumanEval vs ~75% for qwen2.5-coder:14b at the same VRAM). Pull completed + ROCm
   smoke test **PASSED** (clean output, no crash). `harness/.env` updated:
   `WORKER_LADDER=ollama::qwen3:8b,ollama::deepseek-coder-v2:16b,anthropic::claude-sonnet-4-6`

### Blocker status after this session
- **Anthropic credits still exhausted** — but the bill they gate is now much smaller:
  with #30 merged, Anthropic is only the worker-escalation rung (≤15 calls, budget-capped)
  + 4 Haiku roles. Orchestrator (the former dominant cost) is free-tier Gemini.
- Everything else from the morning session unchanged: Netlify live ✓, duration gate ✓.

---

## ✅ DONE 2026-06-12 (morning) — duration honesty + Netlify LIVE (PRs #25–#26)

1. **PR #25 — film duration honesty:** ffprobe passes any clip >0.05s, so a 1-second render
   of a 20-second scene passed the probe (observed live). The film project-level gate now
   derives the expected duration (`verification.expected_film_duration`: shotlist.json shot
   sum, else "N-second" in the goal) and FAILS when the render is under half of it.
2. **PR #26 — Netlify deployment LIVE-VALIDATED.** Operator's token added to `harness/.env`
   (gitignored). Live testing exposed and fixed three wrapper defects:
   - Both pre-existing netlify CLI installs were broken (stale standalone shadowing PATH +
     npm-global incompatible with Node 25) — reinstalled; wrapper now probes candidates with
     `--version` instead of trusting PATH order.
   - JSON through the CLI's Windows cmd shim loses its quotes → createSite minted a
     RANDOMLY-NAMED site, breaking re-deploy idempotency. Site find/create now goes through
     the Netlify REST API directly (urllib).
   - Wrapper self-loads `harness/.env` so standalone runs work.
   **Proof:** two consecutive deploys both landed on https://jclaw-jclaw-deploy-test.netlify.app
   (HTTP 200, correct content); stray misnamed site deleted from the account.

### ⛔ ONE remaining blocker: Anthropic API credits *(cost picture superseded by the
second-session section above — orchestrator now free-tier Gemini, builds ~$0.05–0.15)*
Exhausted (probed repeatedly through 2026-06-12). Top up at console.anthropic.com → Plans &
Billing for the key in `harness/.env`.

### Then remaining (execution only, no new code expected):
1. Film validation rerun — recovery command in `harness/projects/film_validation_v2/HANDOFF.md`.
   Acceptance: real per-scene mp4s at honest durations, probe-clean `final.mp4`, zero silent
   skips, ONE aggregate Telegram push, honest exit code.
2. Factory rehearsal (the binding acceptance test) — from Telegram only: `/run` website →
   live URL; `/continue` feature → same URL redeployed; `/run` film → aggregate push;
   impossible intent → honest FAIL push; kill Ollama mid-build → crash push; two builds →
   strict FIFO; reboot + repeat → zero interactive auth. All green → "factory" status.

---

## ✅ DONE 2026-06-11 — "hands-off product factory" roadmap (PRs #10–#23)

Goal locked with the operator: Telegram is the only human interface; builds queue and run
unattended; web builds auto-deploy to a URL; operator contacted only on terminal outcome.

1. **PR #10** — completeness gate (static stub/asset checks gate per-task + project) +
   per-build cost & prompt-cache telemetry. Vanilla validation build **PASSED** post-merge.
2. **PR #11** — `notify.py`: Telegram push on terminal outcome (PASS/FAIL/crash + heal
   cycles + cost + HANDOFF path + deploy URL). Live round-trip confirmed.
3. **PR #12** — README stack tiers: verified vs generate-only, verification-depth legend.
4. **PR #13** — Telegram FIFO build queue (strictly sequential, one GPU) + `/continue`
   command; `/cancel queue|all`.
5. **PR #14** — experience.jsonl lessons aggregated per stack into orchestrator INIT/DAG
   payloads (deterministic, ≤500 tokens, no extra LLM call).
6. **PR #15** — film render EXECUTION + honest video gates: `_ensure_rendered` runs the
   render (ffmpeg edit-script lines / Python entry) inside verification; missing video FAILS
   ffprobe/frame/sync (was "auto-passed: no video files"); film stacks never get placeholder
   videos; `completeness._missing_python_imports` flags imports to never-written modules;
   mistyped all-video tasks route to video_worker; video tasks must pass their declared
   verification. ffmpeg/ffprobe 8.1.1 installed via winget, on persistent PATH.
7. **PR #16** — FORMAT 5 aggregation: `run_project` returns the verdict; one crashed scene
   no longer sinks the rest; parent HANDOFF aggregates per-scene ✓/✗; parent assembles scene
   clips → frame-checked `final.mp4` (`video_worker.assemble_film`); ONE aggregate Telegram
   push (sub-projects quiet); exit code honest; `handoff._MAX_HEAL` now reads config.
8. **PR #17** — unattended Netlify deploy: `deploy_netlify.py` (token auth, find-or-create
   site `jclaw-<slug>`, `--json`, prints one URL); deploys gated to static web stacks with
   honest ⊘ skip; `## Deployment` section in HANDOFF. `.env` has DEPLOY_HOOK + DEPLOY_TIMEOUT;
   **NETLIFY_AUTH_TOKEN still needed from operator.**
9. **PRs #18–#23 — seven live film-validation runs, each caught a real defect:**
   - **#18** FORMAT 5 recursion spiral (scene → scripts → …): sub-project INIT payloads carry
     `decomposition_allowed: false`; one corrective retry then honest fail; rule 21 exception.
   - **#19** assembly sub-projects detected by dependency shape (depends on all siblings).
   - **#20** render shell scripts EXECUTED via Git Bash (`_find_bash` rejects the WindowsApps
     WSL stub); workers wrap ffmpeg in preflight/variables so line-scraping wasn't enough.
   - **#21** task completion gated on declared files actually existing on disk (worker
     returned plausible JSON, task "done", render.sh never written — 3 heal cycles burned).
   - **#22** video tasks routed by OUTPUT not label (a type:video task declaring only
     render.sh went to video_worker, which renders but doesn't author — silently skipped).
   - **#23** final review fails CLOSED on API error (a crashed review call had green-lit a
     scene with zero video); `.sh`/`.sol`/`.gd` added to reviewable extensions (reviewer
     literally couldn't see render.sh); `config.spec_stack()` helper — film gates keyed on
     empty top-level `spec["stack"]` while FORMAT 1 nests it under `architecture.stack`.

### ⛔ Blockers as of 2026-06-11 *(superseded — see the 2026-06-12 section above: Netlify
resolved + duration gap closed; API credits remain the sole blocker)*
1. ~~Anthropic API credits exhausted~~ — still true 2026-06-12.
2. ~~NETLIFY_AUTH_TOKEN~~ — resolved 2026-06-12 (PR #26, live-validated).
3. ~~Duration honesty gap~~ — closed 2026-06-12 (PR #25).

---

## ✅ DONE 2026-06-04 (previous session, all merged to `main`)

1. **Sprint A** (`ac3bdce`) — worker ladder (`qwen3:8b → qwen2.5-coder:14b → sonnet`), paid-call
   budget (`MAX_PAID_WORKER_CALLS=15`), dispatch timeouts, bounded heal loop, mypy/ruff.
2. **Phase 1 — verification honesty** (`ac3bdce`, live-validated):
   - E2E + project-Playwright checks now **gate** the project and feed the heal loop (were
     computed then ignored); generated tests use relative `goto('/')` vs the `:18090` baseURL.
   - **SKIP ≠ PASS**: tool-missing auto-passes marked `⊘ SKIPPED` in HANDOFF via `SKIP_PREFIX`.
   - Game check fails on zero-size canvas + 1.5s runtime-error window.
3. **Escalation-tax fix** (`b479e57`) — binary/image tasks route to `asset_worker` (+ valid PNG
   placeholder, no 404); single-file script output salvaged before escalating.
4. **Heal-loop convergence + Movies Phase 2** (`056ad67`, salvaged from 2 parallel agents):
   - `heal_metrics.py` + main.py: detect non-convergence; escalate once, then stop early.
   - Movies: `generate_video` reads `task.files`; real film/video-editor ffmpeg-director prompts;
     `music_worker` gates on a real backend; real frame_integrity + sync_check (honest SKIP when
     ffmpeg/ffprobe absent).
5. **Pre-merge review fixes** (`7c7656e`) — from a high-effort review of the PR #5 diff:
   - **Phase-tracking made functional:** `main.py` read `exc._pipeline_phase` but it was set
     nowhere → every crash reported the generic `"pipeline"`. Now a mutable `phase` holder is
     threaded through `_run_project_inner`, so failure handoffs report the real stage.
   - **Worker-timeout liveness limitation documented** (see Known ceilings): the `_dispatch_batch`
     timeout bounds the *wait*, not a running thread.
6. **PR #5 MERGED** → `main` (`a807cf1`). Then **PR #6 MERGED** (`dc0f854`) — docs sync (below).
7. **README + GitHub repo description synced** (PR #6, `bbcf57d`) — README brought from pre-merge
   state to current (bot fixed, movies Phase 2, escalation/heal/bot marked done, roadmap updated);
   the empty GitHub "About" description was filled in.
8. **OpenClaw bot FIXED + CONFIRMED LIVE** (see next section).
9. **GPU VRAM freed** — unloaded a pinned `Pixtral-12B` (7.7 GB, was loaded "Forever") so the
   worker models load cleanly for the next supervised build. `ollama ps` is now empty.

### Validation (2026-06-04, pre-fix baseline)
A supervised vanilla-website build ran end-to-end, **no hang**, correctly exited **"ISSUES FOUND"**
instead of false-greening — all Phase 1 changes fired. It surfaced: 14B worker reliably escalates
on script/binary tasks (now fixed by #3); heal loop bounded but didn't converge (now fixed by #4);
the broken bot (now fixed, #8). Hardware confirmed **AMD RX 9070 XT 16 GB** (→ qwen3 bot crashes are
ROCm/runner instability, not context size). **A fresh supervised run against merged `main` is the
top remaining item** — to confirm the Sonnet-escalation count drops vs this baseline.

---

## OpenClaw bot — ✅ FIXED & CONFIRMED LIVE

The bot now replies correctly on **@JarvisClaw96bot** with the proper model. Confirmed two ways:
a direct `openclaw agent` turn and a real Telegram message both return a coherent **Haiku** reply.

**Config (in `~/.openclaw/openclaw.json`):**
- `agents.defaults.model.primary` → `anthropic/claude-haiku-4-5-20251001` (reliable router).
- `tools.profile` → `minimal` (router-light).

**Root cause was subtler than the config — a STALE ORPHANED GATEWAY.** The config on disk was
already correct, but the *running* gateway was an orphaned process started before the edit, still
serving the old `qwen3:8b` router in memory → inbound Telegram messages were received but produced
no reply.
- **The trap:** `openclaw daemon restart` / `gateway stop` only manage the Windows **Scheduled Task
  "OpenClaw Gateway"** — they do NOT touch a gateway launched independently.
- **The fix:** find the PID on `:18789` (`Get-NetTCPConnection -LocalPort 18789`), `Stop-Process`
  it directly, then `openclaw daemon start` (fresh process re-reads the Haiku config).
- **Verify the live router:** `openclaw agent --agent main --message "PONG and your model"`
  → expect `anthropic/claude-haiku-4-5-20251001`.

**Optional hardening (not blocking):** `OLLAMA_MAX_LOADED_MODELS=1` (bot/worker VRAM contention),
`ollama signin` (fixes web_search), prune ~12 dangling Ollama manifests, and fix the bot's
self-description (it says it routes to `qwen2.5-coder:14b` — actually the 3-rung ladder).

### OpenClaw config invariants (hard-won — do not undo)
- `agents.defaults.model` must be `{"primary": "..."}` ONLY. A `fallback` array is INVALID —
  `openclaw doctor --fix` reverts the whole model block. Failover is via the `agents.defaults.models`
  registry, not a fallback key.
- OpenClaw reads its API key from `C:\Users\Tyler\.openclaw\.env`, NOT Windows env vars.
- Config edits hot-reload for *most* settings, but a **model/router change is only guaranteed after
  a full gateway restart** (the orphaned-process lesson above). `sessions.json` edits need the
  gateway stopped first.
- `tools.profile` allowed values: `minimal`, `coding`, `messaging`, `full`.

---

## Prompt caching — audited 2026-06-04

**Verdict: the high-value path is correct; two easy gaps + no hit telemetry.**
- ✅ **Orchestrator** (`orchestrator.py:83`) caches its system prompt. This is the dominant Claude
  cost (called many times per project: INIT, SPEC_ACCEPTED, every EXECUTION_ERROR retry,
  PROJECT_REVIEW, every REVIEW_FAILED heal cycle) — correct priority, correct placement.
- ⚠️ **Gaps (uncached, but called repeatedly):** `final_review.py:80` (runs every heal cycle) and
  `worker.py:816` `_call_anthropic` (Sonnet escalation rung, up to 15×/run). Add `cache_control`.
- ℹ️ Creative Director (`:31`) + Technical Architect (`:50`) cache their system prompts but run
  **once** per project → no read benefit (harmless, small prompts).
- ℹ️ **No telemetry** — nothing logs `cache_read_input_tokens`/`cache_creation_input_tokens`, so
  cache hits can't be confirmed. Adding one log line would let the next build prove caching works.
- ℹ️ **5-min TTL** can expire across a long DAG (SPEC_ACCEPTED → PROJECT_REVIEW). A 1-hour TTL
  (`ttl:"1h"` + extended-cache beta header) would keep the orchestrator cache warm all run.

---

## 📋 WHAT'S LEFT TO FINALIZE (priority order, updated 2026-06-15 seventh session end)

1. **Factory rehearsal test #4 — film run (~$0.10)** — film stack is now fully local (ComfyUI + Piper TTS + FluidSynth, PR #67 + #68). Run from Telegram:
   ```
   /run Make a 30 second noir film about a detective in 1940s Chicago
   ```
   Expected: genre detected as `jazz`, walking-bass MIDI score rendered, Piper narration generated, ComfyUI noir frames, ffmpeg assembly → `final.mp4`, aggregate Telegram push.
2. **Factory rehearsal test #5 — impossible intent (revised)** — run the BCI/hologram prompt and let it complete rather than canceling. Expect honest FAIL verdict at PROJECT_REVIEW (not garbage PASS). If it passes, the Creative Director needs impossible-intent detection.
3. **Factory rehearsal tests #6–#8** — kill Ollama mid-build, FIFO queue, reboot.
4. **(Optional)** Better noir visual model — swap animagine-xl-3.1 for Realistic Vision V5.1 SDXL in ComfyUI for more cinematic frames.
5. **(Optional)** Impossible-intent detection — Creative Director should detect physically-unbuildable requests and fail at INIT rather than generating a full DAG.
3. **Carry-overs (not blocking):** native mobile CI runner; Playwright runner task type in the DAG; IPFS/on-chain CI deploy hook; LemonSqueezy / Stripe Connect prompts.
4. **Optional hardening / polish:**
   - Worker-timeout hard bound: `shutdown(wait=False, cancel_futures=True)` (3.9+) + audit inner timeouts.
   - OpenClaw: bot self-description, `OLLAMA_MAX_LOADED_MODELS=1`, prune dangling manifests.
   - Prune the 6 stale `worktree-agent-*` branches (dead — work was salvaged into `056ad67`).
   - Gemini REVIEW_FAILED bug — `review_result` consistently omitted; PR #40 workaround works but root cause unresolved.
   - **`experience.jsonl` recency cap** (PR #57 follow-up) — `get_worker_hints()` currently does a full file scan with no TTL; stale escalation patterns from old model versions carry equal weight to recent ones. Fix: only read entries from the last N days (e.g. 30) or cap at the most recent M entries. Prevents misleading hints as models and orchestrator rules improve over time.
   - **`experience.jsonl` file size guard** (PR #57 follow-up) — file grows unboundedly; `get_worker_hints()` is called once per worker task (20× per build), so a large file creates O(tasks × entries) I/O per build. Fix: if file exceeds ~500 entries, truncate oldest on write (rolling window). Keeps scan time bounded regardless of build history length.

### Known structural ceilings
- Worker quality is bounded by 14B-class local models (Ollama-only worker constraint is locked).
- Verification honesty depends on installed tooling — checks SKIP (now honestly marked) when a tool
  is absent.
- Worker-task timeout is liveness-bounded by the *wait*, not by interrupting a running thread:
  `_dispatch_batch` relies on each worker I/O path (Ollama HTTP, subprocesses) carrying its own
  internal timeout — currently true (`ollama.Client(timeout=WORKER_TASK_TIMEOUT)`). Don't remove
  those inner timeouts. A truly uninterruptible worker would still block at the pool's shutdown.
  (Follow-up: `shutdown(wait=False, cancel_futures=True)` on 3.9+ for a harder bound.)

---

## Key paths
- J-Claw harness: `C:\Users\Tyler\Desktop\Jarvis-Claw\harness\`
- GitHub: https://github.com/Matt28296/j-claw (PR #5 + #6 merged; `main` @ `dc0f854`)
- OpenClaw config: `C:\Users\Tyler\.openclaw\openclaw.json`; key in `.openclaw\.env`
- Gateway: port 18789 (Scheduled Task "OpenClaw Gateway"). Ollama: 127.0.0.1:11434
  (qwen3:8b, qwen2.5-coder:14b, llava:7b). Verify router: `openclaw agent --agent main --message …`
- Dashboard "Mission Control": http://localhost:8765 (auto-starts during builds).
- Plan/assessment doc: `C:\Users\Tyler\.claude\plans\please-explain-how-close-bubbly-coral.md`
- Last validation output: `harness\projects\Build_a_small_static_personal_portfolio_website_us\`
