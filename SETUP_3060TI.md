# 3060 Ti setup — second-machine local-LLM (sidecar serve **or** LoRA trainer)

> Carry-on guide for setting up the **3060 Ti** box. The **9070 XT side is complete** (routing + dataset
> export + eval + promote, on branch `feat/two-machine-llm`). This doc is the machine/environment prep
> plus the two scripts still to build. Full design rationale: `DEV_NOTES_two_machine_llm.md`.

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
