"""A/B evaluate a candidate j-claw worker model vs the base (and a previously-promoted) model.

Runs on the **9070 XT** (the always-on evaluator). Generation is local Ollama HTTP only; everything
else goes through the versioned ``evaluation_contract`` so the numbers reflect how j-claw *actually*
prompts, parses, and verifies a worker — without reaching into harness privates.

Two tiers (you can NEVER promote from fast alone):
  * **fast** (default): parse with the real tolerant contract + path-safety + a static AST/stub check.
    Executes NOTHING the model produced, so it is safe to run anywhere. Verdict caps at ``smoke_passed``.
  * **deep** (``--deep``): additionally runs the REAL harness verifier with red/green DISCRIMINATION
    controls per row, so a "pass" actually means the candidate solved that task:
        before        = project gold MINUS the task's declared files
        verify(before)              must FAIL  (else the task isn't needed -> non_discriminating)
        verify(before + gold)       must PASS  (else the verifier is unreliable here -> control_fail)
        verify(before + candidate)  == the score
    SKIP_PREFIX outcomes are 'skipped', never 'pass'. **WARNING: deep mode executes candidate-produced
    code via the real build/test toolchain. Run it only in a hardened/hermetic environment (network
    off, no secrets in env, resource limits). Container isolation is documented as before-trust work.**

Promotion gates on EFFECTIVE evidence: enough *attempted* (non-skipped) rows, a bounded skip rate,
paired per-row wins over base on attempted rows, no parse regression, no per-stack collapse, and no
regression past budget vs a previous adapter. Tiny held-out sets -> ``insufficient_evidence``.

It never calls a paid provider (local-only host, checked structurally) and does not promote anything.

Run from the harness dir:
    python -m training.eval_worker --deep --candidate-model jclaw-worker:cand --base-model qwen2.5-coder:14b
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import secret_scrub

HARNESS = Path(__file__).resolve().parent.parent
REPO = HARNESS.parent
DEFAULT_DATASET = REPO / "jclaw-training" / "datasets" / "curated_v001.jsonl"
DEFAULT_EVAL_DIR = REPO / "jclaw-training" / "evals"
DEFAULT_OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EVAL_CODE_VERSION = 2   # bump on scoring/verdict-logic changes (recorded in artifacts)

# Promotion needs EFFECTIVE evidence, not raw held-out count: enough verifier-ATTEMPTED rows and a
# bounded skip rate. One verify flip must never decide promotion.
MIN_ATTEMPTED_N = int(os.getenv("EVAL_MIN_ATTEMPTED_N", "20"))
MAX_SKIP_RATE = float(os.getenv("EVAL_MAX_SKIP_RATE", "0.5"))

_STUB_MARKERS = (
    "notimplementederror", "raise notimplemented", "# todo: implement", "your code here",
    "lorem ipsum", "coming soon", "placeholder implementation", "<placeholder>",
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
# verify outcomes that count as a genuine attempt (the model was actually tested):
_ATTEMPTED = {"pass", "fail"}
# outcomes that mean the verifier could not discriminate (don't count for or against the model):
_NON_DISCRIMINATING = {"skipped", "non_discriminating", "control_fail", "not_attempted", "unsafe_path", "error"}


def _utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _sha256_file(p: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _is_local_host(host: str) -> bool:
    try:
        netloc = urlparse(host).hostname or ""
    except ValueError:
        return False
    if netloc in _LOCAL_HOSTS:
        return True
    return netloc.startswith("192.168.") or netloc.startswith("10.") or netloc.startswith("172.")


def _safe_rel(path) -> str | None:
    """Normalize a model-supplied path and reject anything that could escape the workspace."""
    if not isinstance(path, str) or not path.strip():
        return None
    p = path.strip().replace("\\", "/")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        return None
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        return None
    return "/".join(parts) if parts else None


# ---------------------------------------------------------------------------- dataset

def load_rows(dataset: Path) -> list[dict]:
    rows: list[dict] = []
    if not dataset.exists():
        return rows
    for line in dataset.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def heldout_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if (r.get("metadata") or {}).get("split") == "heldout"]


def gold_project_files(rows: list[dict]) -> dict[str, dict[str, str]]:
    """{project: {rel_path: gold_content}} from ALL rows — seeds the verification workspace so a single
    task is verified IN CONTEXT against the project's known-good output."""
    proj: dict[str, dict[str, str]] = {}
    for r in rows:
        name = (r.get("metadata") or {}).get("project", "")
        bucket = proj.setdefault(name, {})
        for f in ((r.get("output") or {}).get("files") or []):
            if isinstance(f.get("path"), str):
                bucket[f["path"]] = f.get("content", "")
    return proj


# ---------------------------------------------------------------------------- ollama (only network)

def ollama_generate(host: str, model: str, system: str, prompt: str, timeout: float, seed: int) -> dict:
    body = json.dumps({
        "model": model, "system": system, "prompt": prompt,
        "format": "json", "stream": False,
        "options": {"temperature": 0, "seed": seed, "num_predict": 8192},
    }).encode("utf-8")
    req = urllib.request.Request(
        host.rstrip("/") + "/api/generate", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"text": data.get("response", ""), "tokens": int(data.get("eval_count") or 0),
                "latency_s": round(time.monotonic() - t0, 3), "error": None}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        return {"text": "", "tokens": 0, "latency_s": round(time.monotonic() - t0, 3),
                "error": f"{type(exc).__name__}: {exc}"}
    except json.JSONDecodeError as exc:
        return {"text": "", "tokens": 0, "latency_s": round(time.monotonic() - t0, 3),
                "error": f"bad ollama envelope: {exc}"}


# ---------------------------------------------------------------------------- static (fast tier, no exec)

def static_ok(files: list[dict]) -> bool:
    if not files:
        return False
    seen: set[str] = set()
    for f in files:
        rel = _safe_rel(f.get("path"))
        content = f.get("content")
        if rel is None or rel in seen or not isinstance(content, str) or not content.strip():
            return False
        seen.add(rel)
        if any(m in content.lower() for m in _STUB_MARKERS):
            return False
        if rel.endswith(".py"):
            try:
                tree = ast.parse(content)
            except SyntaxError:
                return False
            meaningful = [n for n in tree.body
                          if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant))
                          and not isinstance(n, ast.Pass)]
            if not meaningful:
                return False
    return True


# ---------------------------------------------------------------------------- deep verify (red/green)

def _write_files(root: Path, files: dict[str, str]) -> str | None:
    for rel, content in files.items():
        safe = _safe_rel(rel)
        if safe is None:
            return rel
        dest = root / safe
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content if isinstance(content, str) else "", encoding="utf-8")
        except OSError as exc:
            return f"{rel}: {exc}"
    return None


def verify_candidate(contract, task, declared: list[str], gold: dict[str, str],
                     produced: dict[str, str]) -> str:
    """Red/green discrimination. Returns one of:
      pass | fail | non_discriminating | control_fail | skipped | unsafe_path | error."""
    for rel in produced:
        if _safe_rel(rel) is None:
            return "unsafe_path"
    before = {p: c for p, c in gold.items() if p not in declared}
    gold_declared = {p: gold[p] for p in declared if p in gold}

    def _run(extra: dict[str, str]) -> tuple[str, str]:
        with tempfile.TemporaryDirectory(prefix="jclaw_eval_") as tmp:
            root = Path(tmp)
            bad = _write_files(root, {**before, **extra})
            if bad is not None:
                return "error", f"write failed: {bad}"
            return contract.verify_task(task, root)

    # Control 1: the project WITHOUT this task's files must FAIL (else the task is not discriminating).
    o_before, _ = _run({})
    if o_before == "pass":
        return "non_discriminating"
    if o_before in ("skipped", "error"):
        return "skipped"
    # Control 2: gold output for this task must PASS (else the verifier is unreliable for this row).
    o_gold, _ = _run(gold_declared)
    if o_gold == "skipped":
        return "skipped"
    if o_gold != "pass":
        return "control_fail"
    # Score: the candidate's output for this task, in the same context.
    o_cand, _ = _run(produced)
    return o_cand if o_cand in ("pass", "fail") else ("skipped" if o_cand == "skipped" else "error")


# ---------------------------------------------------------------------------- per-row scoring

def score_row(contract, row, task, stack, raw, mode, gold_files) -> dict:
    declared = [f for f in (task.files or []) if isinstance(f, str)]
    files, perr = contract.parse_worker_output(raw, task)
    parse_ok = files is not None
    files = files or []
    produced_paths = {f.get("path") for f in files if isinstance(f.get("path"), str)}
    files_complete = bool(declared) and all(d in produced_paths for d in declared)
    try:
        no_secret = not any(secret_scrub.contains_secret(str(f.get("content", ""))) for f in files)
    except Exception:  # noqa: BLE001
        no_secret = True
    stat_ok = static_ok(files) and no_secret

    outcome = "not_attempted"
    if mode == "deep" and parse_ok and files_complete:
        produced = {f["path"]: f.get("content", "") for f in files if isinstance(f.get("path"), str)}
        outcome = verify_candidate(contract, task, declared, gold_files, produced)
    return {
        "stack": stack,
        "parse_ok": parse_ok,
        "files_complete": files_complete,
        "static_ok": stat_ok,
        "no_secret": no_secret,
        "verify_outcome": outcome,
        "attempted": outcome in _ATTEMPTED,
        "verify_pass": outcome == "pass",
        "parse_error": perr,
    }


def aggregate(cards: list[dict], lats: list[float], toks: list[int], gen_errors: int) -> dict:
    n = max(1, len(cards))
    rate = lambda k: round(sum(1 for c in cards if c.get(k)) / n, 4)  # noqa: E731
    attempted = [c for c in cards if c.get("attempted")]
    att_n = len(attempted)
    pass_n = sum(1 for c in attempted if c.get("verify_pass"))
    outcomes: dict[str, int] = {}
    per_stack: dict[str, dict] = {}
    for c in cards:
        outcomes[c["verify_outcome"]] = outcomes.get(c["verify_outcome"], 0) + 1
        st = per_stack.setdefault(c.get("stack", "?"), {"attempted": 0, "pass": 0, "rows": 0})
        st["rows"] += 1
        if c.get("attempted"):
            st["attempted"] += 1
            if c.get("verify_pass"):
                st["pass"] += 1
    return {
        "rows": len(cards),
        "gen_errors": gen_errors,
        "parse_rate_all": rate("parse_ok"),
        "files_complete_rate": rate("files_complete"),
        "static_rate": rate("static_ok"),
        "no_secret_rate": rate("no_secret"),
        "verify_attempted_n": att_n,
        "verify_pass_n": pass_n,
        "verify_pass_rate_attempted": round(pass_n / att_n, 4) if att_n else 0.0,
        "skip_rate": round((len(cards) - att_n) / n, 4),
        "verify_outcomes": outcomes,
        "per_stack": per_stack,
        "avg_latency_s": round(sum(lats) / n, 3) if lats else 0.0,
        "avg_tokens": round(sum(toks) / n, 1) if toks else 0.0,
    }


def evaluate_model(contract, host, model, rows, gold, mode, gen_timeout, seed) -> tuple[dict, list[dict]]:
    cards, lats, toks, gen_errors = [], [], [], 0
    for row in rows:
        try:
            system, user, task, stack = contract.build_worker_prompt(row)
        except contract.ContractError as exc:
            gen_errors += 1
            cards.append({"stack": "?", "parse_ok": False, "files_complete": False, "static_ok": False,
                          "no_secret": True, "verify_outcome": "row_error", "attempted": False,
                          "verify_pass": False, "parse_error": f"contract: {exc}"})
            continue
        res = ollama_generate(host, model, system, user, gen_timeout, seed)
        if res["error"]:
            gen_errors += 1
            cards.append({"stack": stack, "parse_ok": False, "files_complete": False, "static_ok": False,
                          "no_secret": True, "verify_outcome": "gen_error", "attempted": False,
                          "verify_pass": False, "parse_error": res["error"]})
            continue
        proj = (row.get("metadata") or {}).get("project", "")
        cards.append(score_row(contract, row, task, stack, res["text"], mode, gold.get(proj, {})))
        lats.append(res["latency_s"]); toks.append(res["tokens"])
    agg = aggregate(cards, lats, toks, gen_errors)
    agg["model"] = model
    return agg, cards


# ---------------------------------------------------------------------------- comparative gates

def paired_wins_attempted(cand: list[dict], base: list[dict]) -> dict:
    """Per-row wins/losses vs base on rows BOTH models attempted (the statistically valid comparison)."""
    wins = losses = ties = comparable = 0
    for c, b in zip(cand, base):
        if not (c.get("attempted") and b.get("attempted")):
            continue
        comparable += 1
        if c.get("verify_pass") and not b.get("verify_pass"):
            wins += 1
        elif b.get("verify_pass") and not c.get("verify_pass"):
            losses += 1
        else:
            ties += 1
    return {"comparable": comparable, "candidate_only_pass": wins, "base_only_pass": losses, "ties": ties}


def _stack_collapse(cand: dict, base: dict) -> list[str]:
    """A stack where base has real signal (>=3 attempted, some passing) but candidate passes none."""
    bad = []
    for st, b in (base.get("per_stack") or {}).items():
        c = (cand.get("per_stack") or {}).get(st, {})
        if b.get("attempted", 0) >= 3 and b.get("pass", 0) > 0 and c.get("pass", 0) == 0:
            bad.append(st)
    return bad


def comparative_verdict(mode, cand, base, prev, cand_cards, base_cards, budget) -> dict:
    if mode != "deep":
        produced = cand.get("parse_rate_all", 0.0) > 0
        return {"kind": "smoke_passed" if produced else "insufficient_evidence", "promotable": False,
                "reasons": ["fast mode: pipeline exercised, promotion needs --deep" if produced
                            else "fast mode and no parseable output"]}
    att = cand.get("verify_attempted_n", 0)
    if att < MIN_ATTEMPTED_N:
        return {"kind": "insufficient_evidence", "promotable": False,
                "reasons": [f"verify_attempted_n={att} < MIN_ATTEMPTED_N={MIN_ATTEMPTED_N}"]}
    if cand.get("skip_rate", 1.0) > MAX_SKIP_RATE:
        return {"kind": "insufficient_evidence", "promotable": False,
                "reasons": [f"skip_rate={cand.get('skip_rate')} > MAX_SKIP_RATE={MAX_SKIP_RATE}"]}
    reasons: list[str] = []
    paired = paired_wins_attempted(cand_cards, base_cards)
    ok = True
    if paired["candidate_only_pass"] <= paired["base_only_pass"]:
        ok = False
        reasons.append(f"not a paired win vs base: {paired}")
    if cand.get("verify_pass_rate_attempted", 0.0) <= base.get("verify_pass_rate_attempted", 0.0):
        ok = False
        reasons.append("verify_pass_rate_attempted does not beat base")
    if cand.get("parse_rate_all", 0.0) < base.get("parse_rate_all", 0.0):
        ok = False
        reasons.append("parse_rate regressed vs base")
    collapsed = _stack_collapse(cand, base)
    if collapsed:
        ok = False
        reasons.append(f"per-stack collapse vs base on: {collapsed}")
    if prev is not None:
        if prev.get("verify_pass_rate_attempted", 0.0) - cand.get("verify_pass_rate_attempted", 0.0) > budget:
            ok = False
            reasons.append("regresses vs previous beyond budget")
        if (1.0 - cand.get("parse_rate_all", 0.0)) > (1.0 - prev.get("parse_rate_all", 0.0)) + 1e-9:
            ok = False
            reasons.append("schema-failure rate up vs previous")
    return {"kind": "compared", "promotable": ok, "reasons": reasons or ["all comparative gates passed"]}


# ---------------------------------------------------------------------------- main

def run(args: argparse.Namespace) -> int:
    if not _is_local_host(args.ollama_host):
        print(f"REFUSED: --ollama-host {args.ollama_host!r} is not local (no-paid invariant).")
        return 2
    import evaluation_contract as contract   # lazy: keeps --help free of harness/ollama imports

    dataset = Path(args.dataset)
    all_rows = load_rows(dataset)
    rows = heldout_rows(all_rows)
    if not rows:
        print(f"ERROR: no held-out rows in {dataset}. Run export_dataset.py first.")
        return 2
    if args.limit:
        rows = rows[: args.limit]
    gold = gold_project_files(all_rows)
    mode = "deep" if args.deep else "fast"
    print(f"eval: {len(rows)} held-out row(s) | host {args.ollama_host} | mode={mode}")
    if mode == "deep":
        print("  WARNING: deep mode runs the real verifier on candidate-produced code. Ensure a "
              "hardened/hermetic environment (network off, no secrets, resource limits).")

    models = {"base": args.base_model, "candidate": args.candidate_model}
    if args.prev_model:
        models["previous"] = args.prev_model
    aggs, cards = {}, {}
    for label, model in models.items():
        if not model:
            continue
        print(f"  running {label}: {model} ...")
        aggs[label], cards[label] = evaluate_model(
            contract, args.ollama_host, model, rows, gold, mode, args.timeout, args.seed)
        a = aggs[label]
        print(f"    parse_rate={a['parse_rate_all']} attempted={a['verify_attempted_n']} "
              f"pass_rate_attempted={a['verify_pass_rate_attempted']} skip_rate={a['skip_rate']} "
              f"outcomes={a['verify_outcomes']}")

    if "candidate" not in aggs:
        verdict = {"kind": "no_candidate", "promotable": False, "reasons": ["no --candidate-model"]}
        pairing = {}
        print("NOTE: no --candidate-model; ran base only.")
    else:
        verdict = comparative_verdict(
            mode, aggs["candidate"], aggs.get("base", {}), aggs.get("previous"),
            cards["candidate"], cards.get("base", cards["candidate"]), args.regression_budget)
        pairing = paired_wins_attempted(cards["candidate"], cards.get("base", cards["candidate"]))
        print(f"  verdict[{verdict['kind']}]: promotable={verdict['promotable']} "
              f":: {'; '.join(verdict['reasons'])}")
        print(f"  paired vs base (attempted): {pairing}")

    artifact = {
        "schema_version": 2,
        "kind": "jclaw-worker-eval",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": mode,
        "dataset": str(dataset),
        "dataset_manifest_sha256": _sha256_file(dataset.parent / "manifest_v001.json"),
        "heldout_rows": len(rows),
        "gates": {"min_attempted_n": MIN_ATTEMPTED_N, "max_skip_rate": MAX_SKIP_RATE,
                  "regression_budget": args.regression_budget},
        "ollama_host": args.ollama_host,
        "candidate_model": args.candidate_model or None,
        "candidate_hash": args.candidate_hash or None,
        "models": aggs,
        "paired_vs_base": pairing,
        "verdict": verdict,
        "provenance": {
            "eval_code_version": EVAL_CODE_VERSION,
            "contract_versions": contract.versions(),
            "ollama_version": contract.ollama_version(),
            "generation": {"temperature": 0, "seed": args.seed, "num_predict": 8192},
        },
        "invariants": {
            "no_paid_provider_called": True,
            "no_paid_enforced_by": "local-only ollama host + no provider SDK in this path",
            "privacy_check": "enforced(secret_scrub)",
            "verification": "harness run_verification with red/green controls; SKIP=skipped not pass",
        },
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"eval_{_utc_stamp()}.json"
    out_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_path}\neval is informational only -- run promote_worker.py to (gated) promote.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="A/B evaluate a candidate j-claw worker model vs base (local Ollama only).")
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET), help="curated dataset jsonl (held-out rows filtered from it)")
    ap.add_argument("--base-model", default=os.getenv("WORKER_MODEL", "qwen2.5-coder:14b"), help="base Ollama model tag")
    ap.add_argument("--prev-model", default="", help="previously-promoted Ollama tag (optional; enables regression gate)")
    ap.add_argument("--candidate-model", default="", help="candidate Ollama model tag to evaluate")
    ap.add_argument("--candidate-hash", default="", help="canonical adapter/model hash (promote_worker requires a match)")
    ap.add_argument("--ollama-host", default=DEFAULT_OLLAMA_HOST, help="local Ollama base URL")
    ap.add_argument("--deep", action="store_true", help="run the REAL verifier with red/green controls (executes codegen)")
    ap.add_argument("--timeout", type=float, default=float(os.getenv("EVAL_TIMEOUT_S", "300")), help="per-generation timeout (s)")
    ap.add_argument("--seed", type=int, default=int(os.getenv("EVAL_SEED", "0")), help="deterministic generation seed")
    ap.add_argument("--regression-budget", type=float, default=0.02, help="allowed pass-rate drop vs previous adapter")
    ap.add_argument("--limit", type=int, default=0, help="cap held-out rows (0 = all)")
    ap.add_argument("--out-dir", default=str(DEFAULT_EVAL_DIR), help="where to write eval_<ts>.json")
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
