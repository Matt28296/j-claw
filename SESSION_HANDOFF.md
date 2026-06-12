# Session Handoff — J-Claw + OpenClaw

Date: **2026-06-12, second session** (previous: 2026-06-12 morning, 2026-06-04). Operator: Matthew (Windows acct "Tyler"/GitHub TylerBeats).
Two systems:
- **OpenClaw** = Telegram bot front-end (routing only). Config: `C:\Users\Tyler\.openclaw\`
- **J-Claw** = the build pipeline. Code: `C:\Users\Tyler\Desktop\Jarvis-Claw\harness\`

**PRs #10–#29 are MERGED to `main`. PR #30 (Gemini orchestrator) is OPEN — operator review + merge.**
Direct push to `main` is intentionally blocked — land changes via PR.

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

## 📋 WHAT'S LEFT TO FINALIZE (priority order, updated 2026-06-12 second session)

1. **Operator: merge PR #30** (Gemini orchestrator — live-validated, review + merge).
2. **Operator: regenerate the Google API key** (was pasted into a chat session) at
   aistudio.google.com → update `GOOGLE_API_KEY` in `harness/.env`.
3. **Operator: top up Anthropic credits** — still needed for worker escalation + the four
   Haiku roles, but the per-build bill is now ~$0.05–0.15 (orchestrator is free-tier Gemini).
4. **Worker ladder rung-1 switch:** if the `deepseek-coder-v2:16b` ROCm smoke test passed
   (see section above), set in `harness/.env`:
   `WORKER_LADDER=ollama::qwen3:8b,ollama::deepseek-coder-v2:16b,anthropic::claude-sonnet-4-6`
   If it crashed under ROCm, stay on qwen2.5-coder:14b — no change needed.
5. **Film validation rerun** — recovery command in `harness/projects/film_validation_v2/HANDOFF.md`.
   Also the first real exercise of Gemini on SPEC_ACCEPTED / EXECUTION_ERROR / REVIEW_FAILED
   and of the slim payloads (PR #29) under fire.
6. **Factory rehearsal** (binding acceptance test) — from Telegram only; see README roadmap.
7. **Carry-overs:** native mobile CI runner; Playwright runner task type in the DAG;
   IPFS/on-chain CI deploy hook; LemonSqueezy / Stripe Connect prompts.
8. **Optional hardening / polish:**
   - ~~Prompt caching gaps (final_review, e2e)~~ — closed (PR #28). Worker-escalation rung
     already had cache_control. Optional remainder: 1-hour TTL on the orchestrator cache
     (only relevant when ORCHESTRATOR_PROVIDER=anthropic).
   - Worker-timeout hard bound: `shutdown(wait=False, cancel_futures=True)` (3.9+) + audit inner
     timeouts.
   - OpenClaw: bot self-description, `OLLAMA_MAX_LOADED_MODELS=1`, prune dangling manifests.
   - Prune the 6 stale `worktree-agent-*` branches (dead — work was salvaged into `056ad67`).

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
