# 3060 Ti setup — second-machine local-LLM (sidecar serve **or** LoRA trainer)

> Carry-on guide for setting up the **3060 Ti** box. The **9070 XT side is complete** (routing + dataset
> export + eval + promote, on branch `feat/two-machine-llm`). This doc is the machine/environment prep
> plus the two scripts still to build. Full design rationale: `DEV_NOTES_two_machine_llm.md`.

## Status as of 2026-06-24 (twelfth session)

| Step | Status | Notes |
|------|--------|-------|
| Scripts (§F) | ✅ Done | `node_agent.py`, `train_worker.py`, `requirements-wsl.txt`, `sample_config.json` committed on `feat/two-machine-llm` |
| GPU / driver (§A) | ✅ Done | NVIDIA 3060 Ti 8 GB, driver 595.95, CUDA 13.2 |
| Ollama (§B) | ✅ Done | Installed, running on `0.0.0.0:11434`, `OLLAMA_HOST` persisted via `setx` |
| qwen3:8b | ✅ Done | Pulled and ready (5.2 GB, Q4_K_M) |
| Firewall port 11434 | ✅ Done | Inbound TCP 11434 from `100.64.0.0/10` (Tailscale CGNAT). Tighten to `100.77.200.46/32` (9070xt exact IP) when admin available |
| Tailscale (§E connectivity) | ✅ Done | IP `100.76.236.124`, hostname `nvidia3060ti`, tailnet `Matt28296@` |
| Syncthing (§D) | ✅ Done | Device ID `2H2Y7RC-JYJMNVS-TEUJGBG-SCRTWGB-NF3C5OU-IQ5FU4Y-BQBD5I4-XGNANQV`, paired with 9070xt (`ZOWZTAD-6TZUVQ2-VSKVPT6-W3VMJBL-CQV7RLX-HP7LGUE-4RCHNIJ-R2Z5WAR`), both folders syncing |
| Routing wired (§E) | ✅ Done | 9070xt curl-verified our Ollama; `LOCAL_LLM_NODES=amd_9070xt=http://localhost:11434,nvidia_3060ti=http://100.76.236.124:11434` + `TRAINER_NODE=nvidia_3060ti` in `harness/.env` on 9070xt |
| WSL2 + CUDA (§C) | ✅ Done | AMD-V/SVM enabled in BIOS; Ubuntu-22.04 WSL2; nvidia-smi inside WSL shows 3060 Ti, CUDA 13.2 |
| Unsloth pip install | ✅ Done | training/.venv (Python 3.11.15); torch 2.5.1+cu121; unsloth[cu121-torch250] from git; full HF stack |
| `nvidia-smi` inside WSL | ✅ Done | NVIDIA GeForce RTX 3060 Ti, 8192 MiB, CUDA 13.2 confirmed |

The 3060 Ti has **two roles, never both at once**: a **serving sidecar** (extra Ollama capacity for
low-risk tasks while j-claw runs) **or** a **LoRA trainer** (WSL2). The serving lease + `node_agent.py`
enforce the exclusion. Hard invariant carried from the 9070 XT: a local-Ollama failure must **never**
escalate to a paid cloud provider.

---

## 0. Get the repo on this machine
```powershell
git clone https://github.com/Matt28296/j-claw.git
cd j-claw
git checkout feat/two-machine-llm
git pull
```

## A. GPU base
1. Install the latest NVIDIA driver. Verify the card is seen:
   ```powershell
   nvidia-smi          # must list the 3060 Ti (8 GB)
   ```

## B. Serving role — Ollama (Windows)
2. Install Ollama for Windows (https://ollama.com/download).
3. Pull a base worker model. **Prefer `qwen3:8b`** — it fits 8 GB VRAM; `qwen2.5-coder:14b` spills to CPU
   and is slow. The sidecar only takes low-risk task types anyway.
   ```powershell
   ollama pull qwen3:8b
   ```
4. Expose Ollama on the LAN so the 9070 XT can reach it, then restart Ollama:
   - Set a **system** env var `OLLAMA_HOST=0.0.0.0:11434` (binds all interfaces).
   - Allow inbound **TCP 11434** in Windows Firewall — ideally restricted to the 9070 XT's IP.
5. Note this box's LAN IP (`ipconfig` → IPv4). Call it `<3060ti-ip>`.

## C. Trainer role — WSL2 + CUDA
6. Enable WSL2 and install Ubuntu 22.04:
   ```powershell
   wsl --install -d Ubuntu-22.04
   ```
7. Inside WSL: install the CUDA toolkit (WSL CUDA), Python 3.11+, and a venv/conda. Verify the GPU is
   visible **inside WSL**:
   ```bash
   nvidia-smi          # run INSIDE the WSL shell — must show the 3060 Ti
   ```
   (The heavy training deps — unsloth/torch/transformers/trl/peft/datasets/accelerate/bitsandbytes/
   sentencepiece/protobuf — arrive with `training/requirements-wsl.txt`, created when we build
   `train_worker.py`. They install **only** inside WSL, never into `harness/requirements.txt`.)

## D. Shared storage — Syncthing (install on BOTH machines)
8. Install Syncthing on the 3060 Ti and the 9070 XT. Bidirectionally share:
   - **`jclaw-training/`** — datasets / adapters / evals (so the trained adapter reaches the 9070 XT).
   - **`node_state/`** — so the 9070 XT router reads this box's serving lease and lifecycle state.
9. Set **explicit roots** on each box. Beware Windows (`C:\…\jclaw-training`) ↔ WSL
   (`/mnt/c/…/jclaw-training`) **path drift** — it is the #1 silent-failure source. Decide the canonical
   path and reference it in each box's `.env`.

## E. Wire routing — on the **9070 XT** (`harness/.env`)
10. Point the node pool at this box and name it the trainer:
    ```ini
    LOCAL_LLM_NODES=amd_9070xt=http://localhost:11434,nvidia_3060ti=http://<3060ti-ip>:11434
    TRAINER_NODE=nvidia_3060ti
    # SIDECAR_ALLOWED_TASK_TYPES=documentation   # widen later once the sidecar behaves
    ```
11. Confirm reachability from the 9070 XT:
    ```powershell
    curl http://<3060ti-ip>:11434/api/tags        # should return this box's models
    ```
    The sidecar stays **OFFLINE in routing** until `node_agent.py` writes a RUNNING state file with a
    valid serving lease — that is the next build (step F). So routing keeps using the primary until then.

## F. Build the two deferred scripts (next coding session)
These are intentionally **not yet built** — author them with the 3060 Ti available so they can be tested:
- **`harness/node_agent.py`** — CLI `running | offline | force-offline | train | status | heartbeat`.
  Writes `node_state/<TRAINER_NODE>.json`. Hardened train sequence:
  `DRAINING` (serving_allowed=false) → wait inflight→0 (bounded) → **stop Ollama serving** (not just
  `keep_alive:0`) → `TRAINING` → run `TRAINING_COMMAND` → `EXPORTING` → `RETURNING` → live generation
  probe → `RUNNING`. `TRAINING_FAILED` keeps the node out of routing until an explicit `running`.
- **`training/train_worker.py`** (+ `requirements-wsl.txt` + a sample config) — Unsloth QLoRA in WSL2,
  conservative 8 GB defaults (4-bit, LoRA rank 8, batch 1 + grad accum + grad checkpointing), `--help`
  works without importing unsloth. **Must emit `ARTIFACT_MANIFEST.json` + a `.sync_complete` marker
  LAST**, so the 9070 XT's `promote_worker.py` can hash a stable identity and refuse a mid-sync adapter.

## G. Verification checklist (once set up)
- [ ] `nvidia-smi` shows the 3060 Ti on Windows **and** inside WSL.
- [ ] From the 9070 XT, `curl http://<3060ti-ip>:11434/api/tags` returns models.
- [ ] Syncthing shows `jclaw-training/` and `node_state/` in sync on both boxes.
- [ ] *(after `node_agent.py` exists)* `node_agent running` → the dashboard "Local LLM Nodes" panel shows
      `nvidia_3060ti` RUNNING/green; a `documentation` task routes to it.
- [ ] *(after `node_agent.py` exists)* `node_agent train` → it drops out of routing the instant
      `serving_allowed` flips false, inflight drains to 0, and dispatch fails **closed to the primary**
      (never a paid cloud call).

## Full pipeline once both machines are up
```powershell
# 9070 XT: build the dataset from current verified builds
cd harness; python -m training.export_dataset
# 3060 Ti (WSL2): train the candidate adapter            (train_worker.py — step F)
python train_worker.py --config sample_config.json
# 9070 XT: stage the candidate GGUF into Ollama as a temp tag, then evaluate (deep, gated)
cd harness; python -m training.eval_worker --deep --candidate-model jclaw-worker:cand `
    --candidate-hash <canonical hash> --prev-model jclaw-worker:v000
# 9070 XT: promote ONLY if promotable (prints the command; --apply to run it)
python -m training.promote_worker --eval ../jclaw-training/evals/eval_<ts>.json --new-tag jclaw-worker:v001
```
