# Session Handoff — J-Claw + OpenClaw

Date: **2026-06-04**. Operator: Matthew (Windows acct "Tyler"/GitHub TylerBeats).
Two systems:
- **OpenClaw** = Telegram bot front-end (routing only). Config: `C:\Users\Tyler\.openclaw\`
- **J-Claw** = the build pipeline. Code: `C:\Users\Tyler\Desktop\Jarvis-Claw\harness\`

All J-Claw work this session is on branch **`harness/verification-honesty`** (PR **#5** → `main`,
open). Latest commit `056ad67`. `main` is still at `04f9b74` until the PR is merged.

---

## ✅ DONE THIS SESSION (all committed on harness/verification-honesty)

1. **Sprint A** (`ac3bdce`) — worker ladder (`qwen3:8b → qwen2.5-coder:14b → sonnet`), paid-call
   budget (`MAX_PAID_WORKER_CALLS=15`), dispatch timeouts, bounded heal loop, mypy/ruff.
2. **Phase 1 — verification honesty** (`ac3bdce`, live-validated):
   - E2E + project-Playwright checks now **gate** the project and feed the heal loop (were
     computed then ignored); generated tests use relative `goto('/')` vs the `:18090` baseURL.
   - **SKIP ≠ PASS**: tool-missing auto-passes marked `⊘ SKIPPED` in HANDOFF via `SKIP_PREFIX`.
   - Game check fails on zero-size canvas + 1.5s runtime-error window.
3. **README + finalize roadmap** (`8ef226f`) — honest capability scorecard, corrected OpenClaw
   section, fixed stale config table.
4. **Escalation-tax fix** (`b479e57`) — binary/image tasks route to `asset_worker` (+ valid PNG
   placeholder, no 404); single-file script output salvaged before escalating. Cuts the Sonnet
   escalations seen in the validation run (qa_check ×3, PNG icons ×5).
5. **Heal-loop convergence + Movies Phase 2** (`056ad67`, salvaged from 2 parallel agents):
   - `heal_metrics.py` + main.py: detect non-convergence; escalate once, then stop early.
   - Movies: `generate_video` reads `task.files` (was zero-output); real film/video-editor
     ffmpeg-director prompts; `music_worker` gates on a real backend; real frame_integrity +
     sync_check video verification (honest SKIP when ffmpeg/ffprobe absent).

### Validation (2026-06-04)
A supervised vanilla-website build ran end-to-end, **no hang**, correctly exited **"ISSUES FOUND"**
instead of false-greening — all Phase 1 changes fired. Confirmed findings: 14B worker reliably
escalates on script/binary tasks (now mitigated by #4); heal loop bounded but didn't converge
(now mitigated by #5). Hardware confirmed **AMD RX 9070 XT 16GB** (→ qwen3 bot crashes are
ROCm/runner instability, not context size).

---

## OpenClaw bot — FIXED (config), NEEDS A TELEGRAM TEST

The bot was broken: `ollama/qwen3:8b` crashed on the AMD runner with no working failover
(`next=none`) → no replies. **Fix applied this session** in `~/.openclaw/openclaw.json` (hot-reloads):
- `agents.defaults.model.primary` → `anthropic/claude-haiku-4-5-20251001` (reliable router; this is
  also what `openclaw doctor --fix` reverts to).
- `tools.profile` → `minimal` (was `coding` — lighter prompt for a router).

**STILL TO DO:** send a message to **@JarvisClaw96bot** to confirm Haiku now replies. Optional
hardening: `OLLAMA_MAX_LOADED_MODELS=1` (avoid bot/worker VRAM contention), `ollama signin` (fix
web_search), and `ollama rm` the ~12 dangling/half-deleted manifests.

### OpenClaw config invariants (hard-won — do not undo)
- `agents.defaults.model` must be `{"primary": "..."}` ONLY. A `fallback` array is INVALID —
  `openclaw doctor --fix` reverts the whole model block. Failover is via the `agents.defaults.models`
  registry, not a fallback key.
- OpenClaw reads its API key from `C:\Users\Tyler\.openclaw\.env`, NOT Windows env vars.
- Editing `openclaw.json` hot-reloads; editing `sessions.json` needs the gateway stopped first.
- `tools.profile` allowed values: `minimal`, `coding`, `messaging`, `full`.

---

## 📋 WHAT'S LEFT TO FINALIZE (priority order)

1. **Merge PR #5** → `main` (the direct push to main is intentionally blocked; merge on GitHub).
2. **Confirm the OpenClaw bot** replies on Telegram (Haiku router) + optional hardening above.
3. **Re-run a supervised build** to validate the escalation-tax + convergence fixes end-to-end and
   confirm the Sonnet-escalation count drops vs the 2026-06-04 baseline.
4. **Movies: live-validate** — run a "10-second video" build; needs `ffmpeg`/`ffprobe` installed to
   actually render + verify (otherwise honest SKIP).
5. **Carry-overs** (not yet done): Playwright runner task type in the orchestrator DAG;
   IPFS/on-chain CI deploy hook; LemonSqueezy / Stripe Connect multi-vendor prompts.
6. **Native mobile verification** (Phase 3): stand up a macOS/Android CI runner, or explicitly mark
   Swift/Kotlin "generate-only" — cannot build/verify on this Windows box.

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
- GitHub: https://github.com/Matt28296/j-claw (PR #5)
- OpenClaw config: `C:\Users\Tyler\.openclaw\openclaw.json`; key in `.openclaw\.env`
- Gateway: port 18789. Ollama: 127.0.0.1:11434 (qwen3:8b, qwen2.5-coder:14b, llava:7b).
- Dashboard "Mission Control": http://localhost:8765 (auto-starts during builds).
- Plan/assessment doc: `C:\Users\Tyler\.claude\plans\please-explain-how-close-bubbly-coral.md`
- Last validation output: `harness\projects\Build_a_small_static_personal_portfolio_website_us\`
