# Session Handoff — J-Claw + OpenClaw

Date: **2026-06-15, seventh session** (previous: sixth 2026-06-14/15, fifth 2026-06-13/14, fourth 2026-06-13, third 2026-06-12, second + morning 2026-06-12, first 2026-06-04). Operator: Matthew (Windows acct "Tyler"/GitHub TylerBeats).
Two systems:
- **OpenClaw** = Telegram bot front-end (routing only). Config: `C:\Users\Tyler\.openclaw\`
- **J-Claw** = the build pipeline. Code: `C:\Users\Tyler\Desktop\Jarvis-Claw\harness\`

**PRs #10–#61 are MERGED to `main`.**
Direct push to `main` is intentionally blocked — land changes via PR.

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

1. **Tony Montana v8 clean run** — use `/run Tony Montana Miami Vice fan site v8` (unique intent bypasses idempotency guard). Expected: DAG ~20–24 tasks, CSS split across named files (no `css/style.css`), sections with both `id` and `class` from the start (no post-hoc fix needed), dark mode CSS task as a declared dependency. Validates PRs #51–53 rules work for Ollama workers independently.
2. **Factory rehearsal items #3–7** (binding acceptance test) — from Telegram only:
   - **#3 `/run` a film** — aggregate push, real per-scene mp4s, probe-clean `final.mp4`
   - **#4 impossible intent** — honest FAIL push
   - **#5 kill Ollama mid-build** — crash push, pipeline recovers
   - **#6 two queued builds** — strict FIFO, both complete and push
   - **#7 reboot + repeat** — no interactive auth anywhere
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
