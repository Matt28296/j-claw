# 3060 Ti — Setup & Operations Reference

The 3060 Ti acts as a **serving sidecar** (extra Ollama capacity for low-risk tasks) **or** a
**LoRA trainer** (fine-tuning in WSL2) — **never both at once**. The `node_agent.py` lifecycle
manager enforces the exclusion. The full two-machine pipeline has been proven end-to-end as of
2026-06-25.

For design rationale and the 9070 XT side, see `DEV_NOTES_two_machine_llm.md`.

---

## Setup status (as of 2026-06-25)

All steps complete. This is an operational reference, not a to-do list.

| Step | Status |
|---|---|
| GPU / driver | ✅ NVIDIA RTX 3060 Ti 8 GB, driver 595.95, CUDA 13.2 |
| Ollama (Windows) | ✅ Running on `0.0.0.0:11434`; `OLLAMA_HOST` persisted via `setx` |
| qwen3:8b | ✅ Pulled (5.2 GB, Q4_K_M) |
| qwen2.5-coder:7b-instruct | ✅ Pulled (4.7 GB) — base model for A/B eval |
| jclaw-worker:cand | ✅ Staged in Ollama (4.7 GB) — current LoRA candidate |
| Firewall TCP 11434 | ✅ Inbound from `100.64.0.0/10` (Tailscale CGNAT) |
| Tailscale | ✅ IP `100.76.236.124`, hostname `nvidia3060ti`, tailnet `Matt28296@` |
| Syncthing | ✅ Paired; both folders syncing direct via tailnet; auto-starts on Windows login |
| Routing wired (9070 XT) | ✅ `LOCAL_LLM_NODES` + `TRAINER_NODE` set in 9070 XT's `harness/.env` |
| WSL2 + Ubuntu 22.04 | ✅ AMD-V/SVM enabled; Ubuntu-22.04; `nvidia-smi` shows 3060 Ti inside WSL |
| Training venv | ✅ `training/.venv` (Python 3.11.15); full stack installed (see version pinning) |
| node_agent.py | ✅ Committed on `feat/two-machine-llm` |
| train_worker.py | ✅ Committed on `feat/two-machine-llm` |
| QLoRA training (end-to-end) | ✅ Ran — adapter at `jclaw-training/adapters/jclaw-worker-v001/` |
| GGUF export (q4_k_m) | ✅ `jclaw-training/adapters/jclaw-worker-v001-gguf/jclaw-worker-q4_k_m.gguf` |
| eval_worker --deep | ✅ Ran — verdict: `insufficient_evidence` (dataset too small; gate working correctly) |

---

## Machine identity

| Item | Value |
|---|---|
| Tailscale IP | `100.76.236.124` |
| Hostname | `nvidia3060ti` |
| Syncthing Device ID | `2H2Y7RC-JYJMNVS-TEUJGBG-SCRTWGB-NF3C5OU-IQ5FU4Y-BQBD5I4-XGNANQV` |
| OLLAMA_HOST (from WSL) | `http://172.23.240.1:11434` (Windows host gateway) |
| Syncthing `jclaw-training` folder | `C:\Users\Matthew\j-claw\jclaw-training` |
| Syncthing `node_state` folder | `C:\Users\Matthew\j-claw\node_state` |

**9070 XT peer:**

| Item | Value |
|---|---|
| Tailscale IP | `100.77.200.46` |
| Syncthing Device ID | `ZOWZTAD-6TZUVQ2-VSKVPT6-W3VMJBL-CQV7RLX-HP7LGUE-4RCHNIJ-R2Z5WAR` |

---

## Day-to-day operations

### Start / verify the sidecar (serving mode)

After a reboot or session reset, bring the node up:

**1. Ensure Ollama is running** (usually auto-starts):
```powershell
# Check
Invoke-WebRequest http://localhost:11434/api/tags -UseBasicParsing | Select-Object -Expand Content
# If not running, launch it from the Start menu or:
Start-Process "C:\Users\Matthew\AppData\Local\Programs\Ollama\ollama.exe"
```

**2. Ensure Syncthing is running** (auto-starts via Windows registry):
```powershell
Get-Process syncthing -ErrorAction SilentlyContinue
# If not running:
Start-Process "C:\Users\Matthew\AppData\Local\Syncthing\syncthing.exe" -WindowStyle Hidden
```

**3. Start the heartbeat loop** (does NOT survive WSL session resets — must be re-run each session):
```bash
# Run from Git Bash or WSL:
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- bash -c "
cat > /tmp/hb_loop.sh << 'EOF'
#!/bin/bash
while true; do
  cd /mnt/c/Users/Matthew/j-claw
  python harness/node_agent.py heartbeat 2>/dev/null
  sleep 4
done
EOF
chmod +x /tmp/hb_loop.sh
nohup /tmp/hb_loop.sh > /tmp/hb_loop.log 2>&1 &
echo Heartbeat PID \$!
"
```

**4. Verify the node is live** — check `node_state/nvidia_3060ti.json`:
- `mode` should be `RUNNING`
- `serving_allowed` should be `true`
- `updated_at` should be within the last 10 seconds

The 9070 XT reads this file via Syncthing; if `updated_at` goes stale (>45s), the router marks this node OFFLINE and falls back to the primary.

### Check node status

```bash
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- python /mnt/c/Users/Matthew/j-claw/harness/node_agent.py status
```

Or just read the state file:
```powershell
Get-Content C:\Users\Matthew\j-claw\node_state\nvidia_3060ti.json | ConvertFrom-Json
```

---

## Training pipeline

### Prerequisites
- Syncthing must have synced the dataset from the 9070 XT:
  `C:\Users\Matthew\j-claw\jclaw-training\datasets\curated_v001.jsonl`
- At least 20 held-out rows needed for a promotable eval verdict (current dataset: 8 rows total, 1 held-out)

### Run training

```bash
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- bash -c "
cd /mnt/c/Users/Matthew/j-claw
source training/.venv/bin/activate
python training/train_worker.py --config training/sample_config.json 2>&1
"
```

`node_agent` automatically transitions the node: `RUNNING` → `DRAINING` → `TRAINING` → `RETURNING` → `RUNNING`.
The trained adapter lands in `jclaw-training/adapters/<name>/` with `ARTIFACT_MANIFEST.json` + `.sync_complete`.
Syncthing pushes it to the 9070 XT automatically.

### Export GGUF and stage as Ollama candidate

After training completes, export the adapter to GGUF and stage it:

```bash
# Step 1: merge LoRA + quantize to q4_k_m (saves to WSL /tmp/ first, then copies to Windows)
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- bash /mnt/c/Users/Matthew/j-claw/convert_gguf.sh

# Step 2: stage in Ollama (computes SHA256, creates Modelfile, runs ollama create)
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- bash -c "
cd /mnt/c/Users/Matthew/j-claw
source training/.venv/bin/activate
python run_stage.py
"
```

`run_stage.py` prints the `candidate_hash` (SHA256 of the GGUF) — keep this for the eval command.

### Run eval_worker --deep

**Must run from the `harness/` directory** (the module lives at `harness/training/eval_worker.py`):

```bash
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- bash -c "
cd /mnt/c/Users/Matthew/j-claw/harness && \
OLLAMA_HOST=http://172.23.240.1:11434 \
/mnt/c/Users/Matthew/j-claw/training/.venv/bin/python -m training.eval_worker --deep \
  --candidate-model jclaw-worker:cand \
  --candidate-hash <HASH_FROM_RUN_STAGE> \
  --base-model qwen2.5-coder:7b-instruct \
  --dataset /mnt/c/Users/Matthew/j-claw/jclaw-training/datasets/curated_v001.jsonl \
  --out-dir /mnt/c/Users/Matthew/j-claw/jclaw-training/evals 2>&1
"
```

The eval JSON is written to `jclaw-training/evals/eval_<ts>.json` and syncs to the 9070 XT via Syncthing.
The 9070 XT's `promote_worker.py` reads the eval result and decides whether to promote.

---

## Version pinning (WSL2 training venv)

These exact versions are required. Do not upgrade without testing — several combinations break silently.

```
torch 2.5.0+cu121
unsloth 2024.12.12          # installed with --no-deps
unsloth-zoo 2024.12.7       # installed with --no-deps
transformers 4.47.1
trl 0.14.0
accelerate 1.2.0
triton 3.1.0                # 3.2.0 breaks WSL2 CUDA driver detection
xformers 0.0.28.post2
python-dotenv 1.2.2         # required by harness/config.py → eval_worker
ollama 0.6.2                # required by harness/worker.py → eval_worker
rich, httpx                 # already present; required by harness
```

To reinstall the pinned unsloth stack (if the venv gets corrupted):
```bash
# From WSL inside the training venv:
pip install --no-deps --force-reinstall "unsloth==2024.12.12" "unsloth-zoo==2024.12.7"
pip install transformers==4.47.1 trl==0.14.0 accelerate==1.2.0 triton==3.1.0
pip install xformers==0.0.28.post2 python-dotenv rich ollama httpx
```

---

## Critical gotchas

**DrvFS atomic rename fails:** `safetensors` and other libraries use atomic rename, which fails on
`/mnt/c/` (NTFS via WSL2 DrvFS). Always save intermediate files to WSL-native `/tmp/`, then `cp`
the final `.gguf` to the Windows path afterward. `convert_gguf.sh` already handles this.

**Git Bash path translation:** Git Bash translates `/mnt/c/...` to `C:/Program Files/Git/mnt/c/...`
before passing arguments. Fix: prefix every WSL/path-containing command with `MSYS_NO_PATHCONV=1`.

**ollama.exe needs Windows-format paths:** `ollama.exe` is a Windows binary; it cannot see WSL's
filesystem. Write the Modelfile to `/mnt/c/Users/Matthew/j-claw/Modelfile.cand` (WSL path) and pass
`C:/Users/Matthew/j-claw/Modelfile.cand` (Windows path) to `ollama.exe`. `run_stage.py` handles this.

**Syncthing Folder ID must match exactly:** `node-state` (hyphen) and `node_state` (underscore) are
different folders in Syncthing's model. The Folder IDs configured on both machines must be identical.
Use `node_state` (underscore) to match the 9070 XT.

**Heartbeat loop doesn't survive WSL restarts:** `/tmp/hb_loop.sh` is gone after a WSL session reset.
Re-run the heartbeat loop command above after every WSL restart. The 9070 XT's TTL is 45s; without a
heartbeat the node appears OFFLINE within a minute.

**triton 3.2.0 breaks WSL2:** triton 3.2 changed the CUDA driver detection API in a way incompatible
with WSL2. Pin `triton==3.1.0`. Upgrading triton will cause training to fail with
`RuntimeError: 0 active drivers ([])`.

**unsloth/unsloth-zoo must be installed with --no-deps:** newer unsloth versions pull
`torchao>=0.13.0`, which requires `torch.int1` (only in PyTorch 2.6+). torch 2.5 has no cu124
wheel — cu121 is the only option on this box. The `--no-deps` flag prevents pip from pulling the
incompatible torchao chain.

**eval_worker module path:** the eval module is at `harness/training/eval_worker.py`. Run it as
`python -m training.eval_worker` from the `harness/` directory, NOT from the repo root. Running from
the repo root gives `No module named training.eval_worker`.

---

## Current adapter / eval state

| Artifact | Location |
|---|---|
| Adapter (safetensors) | `jclaw-training/adapters/jclaw-worker-v001/` |
| GGUF (q4_k_m, 4.4 GB) | `jclaw-training/adapters/jclaw-worker-v001-gguf/jclaw-worker-q4_k_m.gguf` |
| candidate_hash | `f790383cd58fe0d329e6f655e5851787cf44ce8cd2b93bbea2bde843f224fae6` |
| Staged Ollama tag | `jclaw-worker:cand` |
| Eval result | `jclaw-training/evals/eval_20260625T002552Z.json` |
| Eval verdict | `insufficient_evidence` — `verify_attempted_n=0 < MIN_ATTEMPTED_N=20` |
| llama-quantize binary | `llama.cpp/build/bin/llama-quantize` (5.3 MB, pre-built) |

The gate is working correctly. To get a promotable result, the 9070 XT needs to export a larger
dataset (≥20 discriminating held-out rows) via `python -m training.export_dataset`, push it via
Syncthing, and then a new training run + eval cycle is needed on this machine.

---

## Coord repo (inter-machine messaging)

Both machines read and write `Matt28296/jclaw-coord` (master branch):
- `msg/3060ti.md` — messages FROM this machine TO the 9070 XT (append at bottom)
- `msg/9070xt.md` — messages FROM the 9070 XT TO this machine (read-only from here)

Protocol: `git pull --rebase`, append entry, `git add msg/3060ti.md`, `git commit`, `git push`.
Always pull before pushing to avoid conflicts.
