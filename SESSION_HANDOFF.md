# Session Handoff — J-Claw + OpenClaw

Date: 2026-06-03. Operator: Matthew (Windows acct "Tyler"/GitHub TylerBeats).
Two systems in play:
- **OpenClaw** = Telegram bot front-end (routing only). Config: `C:\Users\Tyler\.openclaw\`
- **J-Claw** = the build pipeline. Code: `C:\Users\Tyler\Desktop\Jarvis-Claw\harness\`

---

## 🔴 CRITICAL OPEN ISSUE — OpenClaw Telegram bot is broken (IN PROGRESS)

**Symptom:** Bot gives no response to Telegram messages ("completely broke").

**Root cause (confirmed via gateway + ollama logs):**
- The bot model `ollama/qwen3:8b` **crashes** on OpenClaw's full-context requests:
  `500 {"error":"model runner has unexpectedly stopped, this may be due to resource
  limitations or an internal error"}` → provider goes into cooldown → no reply.
- It works on **tiny** prompts (direct `ollama generate` test returned "OK") but crashes
  under the real system prompt + tool schemas + session context. The session is declared at
  **200k ctx** for an 8B model — suspected oversized KV-cache allocation / resource crash.
- **It was ALWAYS crashing.** The Haiku fallback silently caught every crash — which is also
  why Haiku quota bled to **259% (129K/50K input)**. The "local bot" was effectively Haiku-powered.

**What I changed this session re: the bot (in order):**
1. To stop the Haiku bleed: removed Haiku from the model registry, deleted the 74k-token
   Haiku-pinned `agent:main:main` session, slimmed `AGENTS.md` 213→~15 lines, cleared
   `auth-state.json` cooldowns. → This REMOVED the safety net → bot went fully dead.
2. To restore function: **re-added** `anthropic/claude-haiku-4-5-20251001` to
   `agents.defaults.models` in `openclaw.json` and restarted/hot-reloaded the gateway.

**STATUS: NOT YET VERIFIED.** Gateway is up (PID 6972, port 18789, Telegram channel OK).
The Haiku fallback is back in the registry (hot reload applied at 19:17). **Need the user to
send a Telegram message to @JarvisClaw96bot to confirm it now responds** (it should fall to
Haiku when qwen3 crashes). Re-adding Haiku means crashes route to Haiku each message → will
consume some quota again (smaller now: lean AGENTS.md + no 74k session) until qwen3 is fixed.

**DECISION STILL NEEDED — how to get a reliable LOCAL bot (qwen3 keeps crashing):**
- **A. Cap Ollama context** — `OLLAMA_CONTEXT_LENGTH` env (e.g. 16384) or a Modelfile
  `PARAMETER num_ctx` for qwen3. Tradeoff: same Ollama server feeds J-Claw workers
  (qwen2.5-coder:14b) — pick a value that doesn't starve code-gen. NOTE: if OpenClaw sends
  num_ctx in the request, a Modelfile default may be overridden — may need an OpenClaw-side
  per-model context-window setting (couldn't find the exact key; docs URL 404'd — investigate
  `openclaw.json` model entry schema or OpenClaw docs).
- **B. Trim the tools profile** — `tools.profile` is `"coding"` (injects write/edit/exec +
  schemas). A router only needs `exec` (+read). Smaller prompt → less likely to crash AND
  better routing integrity (can't write code). Find a minimal valid profile or explicit allowlist.
- **C. Accept Haiku fallback** — works now, uses some subscription quota.
- **D. Switch bot to a smaller stable local model** — note `ollama` log shows MANY models with
  missing blobs (half-deleted: llama3.2:3b, qwen2.5:3b, etc.) — would need a clean re-pull.

**OpenClaw config invariants learned the hard way:**
- `agents.defaults.model` must be `{"primary": "..."}` ONLY. A `"fallback": [...]` array is
  INVALID in this version — `openclaw doctor --fix` reverts the whole model block to Haiku-primary.
  Fallback is achieved via the `agents.defaults.models` registry (auto-failover), not a fallback key.
- OpenClaw reads its API key from `C:\Users\Tyler\.openclaw\.env`, NOT Windows env vars.
- Editing `openclaw.json` hot-reloads (no restart needed); editing `sessions.json` needs the
  gateway stopped first.

---

## ✅ COMPLETED THIS SESSION

### OpenClaw bleed mitigation (done, but see open issue above)
- Deleted Haiku-pinned `agent:main:main` session (`sessions.json` + its `.jsonl`); backup at
  `sessions.json.bak`. Remaining session: the qwen3:8b Telegram one.
- `AGENTS.md` rewritten lean (router-only, no "commit your own code"/proactive/heartbeat burn).
- `auth-state.json` cooldowns/errorCounts cleared.

### J-Claw Sprint A — code fixes (COMPLETE + smoke-tested)
All in `C:\Users\Tyler\Desktop\Jarvis-Claw\harness\`:

- **`config.py`**: added `WORKER_LADDER` = `ollama::qwen3:8b,ollama::qwen2.5-coder:14b,anthropic::claude-sonnet-4-6`;
  added `MAX_PAID_WORKER_CALLS=15`; fixed `WORKER_FALLBACKS` (was uninstalled `qwen2.5-coder:7b` → `qwen3:8b`).
  (Earlier: `WORKER_MODEL`→`qwen2.5-coder:14b`, added `TECHNICAL_ARCHITECT_MODEL`.)
- **`worker.py`**: `route_task()` (base routing is ALWAYS local — never starts on paid Sonnet;
  trivial single-file scaffold/style/data/config → rung 0, else strongest-local rung);
  `routed_rung()` = base + retry_count (escalation on retry, capped); paid-budget gate
  (`reset_paid_budget`/`_reserve_paid_call`) so cloud calls are capped at `MAX_PAID_WORKER_CALLS`
  then degrade to local; last-ditch local append only when chain is all-cloud; ollama client
  `timeout=WORKER_TASK_TIMEOUT`.
- **`scheduler.py`**: unified `_dispatch_batch()` (single/serial tasks now run under
  `WORKER_TASK_TIMEOUT` — previously no timeout → a hung worker hung the whole pipeline);
  bounded review loop in `run()` (max 2 rounds) so `_project_review` follow-up fix tasks
  **actually execute** (were silently dropped); `_project_review()` now returns bool; rung logging.
- **`main.py`**: `reset_paid_budget()` at project start.
- **`.env.example`**: documented `WORKER_LADDER` + `MAX_PAID_WORKER_CALLS`; removed `7b` refs.
- **`verification.py`** (earlier): added `_run_mypy_check` + `_run_ruff_check`, wired into
  Python/fastapi verification paths.
- **`technical_architect.py`** (earlier): uses `TECHNICAL_ARCHITECT_MODEL`.

Verified: router never base-routes to paid Sonnet; escalation climbs local→Sonnet on retry;
budget cap + degrade-to-local proven end-to-end; all modules compile.

---

## 📋 NOT STARTED — Sprint B (make verification HONEST)

From a 5-agent review (all returned "needs-fixes"). These are why true one-shot success < reported:
- **Verification false-passes** (`verification.py`): missing-tool auto-pass (ffprobe/godot/bandit/
  pytest/mypy/ruff all return pass when tool absent); **video = theater** (passes any file ≥100 bytes,
  `sync_check` does nothing); **godot check unreachable** (only runs when `method=="none"`);
  **E2E broken** — generated tests hit `localhost:3000` but server serves `:18090`, AND the result
  is never gated (only logged in `main.py:282`).
- **Video pipeline produces ZERO output** (`scheduler.py` routes video tasks to `generate_video`
  which reads `task.output_files` — empty for video tasks; data-flow bug). `film`/`video-editor`
  have NO `_STACK_PROMPT` → fall back to vanilla HTML. `music_worker.can_generate()` hardcoded False.
- Electron = install-only; games gate on canvas-renders only (no gameplay).
- 12 code stacks (web/app/game/dapp) ARE genuinely strong.

**Minor:** `worker.py:~433` prints a `⚠` that crashes on Windows cp1252 console IF `main.py` is
run directly — `run.bat` sets `chcp 65001`+`PYTHONUTF8=1` so real builds are fine. 30-sec ASCII swap.

---

## ▶️ RECOMMENDED NEXT STEPS (priority order)
1. **Resolve the OpenClaw bot (current fire):** confirm it responds after the Haiku re-add, then
   pick a permanent local fix (Option A context-cap or B tools-trim above) so it stops leaning on Haiku.
2. **Live supervised test build of J-Claw** — Sprint A is verified only in isolation; a real run
   validates routing/escalation/budget/timeout/heal end-to-end AND ranks Sprint B by real severity.
   Suggest a small target (simple site or one-mechanic game). Command: `cd C:\Users\Tyler\Desktop\Jarvis-Claw && .\run.bat --yes "<description>"`. Watch the dashboard at http://localhost:8765.
3. **Sprint B** — verification honesty + video pipeline, prioritized by what the test build reveals.

## Key paths
- OpenClaw config: `C:\Users\Tyler\.openclaw\openclaw.json`, workspace `SOUL.md`/`AGENTS.md`/`SKILL.md`
- OpenClaw sessions/auth: `C:\Users\Tyler\.openclaw\agents\main\sessions\`, `...\agent\auth-state.json`
- J-Claw harness: `C:\Users\Tyler\Desktop\Jarvis-Claw\harness\`
- Plan file (architecture + roadmap): `C:\Users\Tyler\.claude\plans\does-this-fit-the-playful-rainbow.md`
- Gateway: PID 6972, port 18789. Ollama: 127.0.0.1:11434 (models: qwen3:8b, qwen2.5-coder:14b, llava:7b).
- Dashboard "Mission Control": http://localhost:8765 (auto-starts during builds).
