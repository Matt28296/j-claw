"""Export supervised-fine-tuning (SFT) rows from VERIFIED j-claw build artifacts (runs on the 9070 XT).

Quality gate (the "high-confidence" rule): a project contributes rows ONLY if its final honest review
passed — `REVIEW.md` contains `VERDICT: PASS` — AND every task is `done`. This is the same completion
signal The-Brain uses to "graduate" a build, so the dataset inherits that bar. Per task we additionally
require real, non-empty output files on disk, no stub/placeholder markers, and no embedded secret.

IMPORTANT (recon gotcha): a task's output file CONTENT is NOT stored in tasks_done.json — it lives on
disk in the project dir. We map each task's declared `files` to `project_dir/<rel>` and read them.

Every row AND the manifest are passed through secret_scrub.scrub_obj before writing. No torch/LLM imports.

CLI:  python -m training.export_dataset [--out ../jclaw-training/datasets/curated_v001.jsonl]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

from . import secret_scrub

HARNESS = Path(__file__).resolve().parent.parent          # .../harness
REPO = HARNESS.parent                                      # repo root
PROJECTS_DIR = HARNESS / "projects"
DEFAULT_OUT = REPO / "jclaw-training" / "datasets" / "curated_v001.jsonl"
EXPORT_VERSION = "v001"
HELDOUT_EVERY = 5  # ~20% of rows go to the deterministic held-out split

_INSTRUCTION = ("Implement this j-claw worker task. Return the complete file contents as valid JSON only.")
_VERDICT_RE = re.compile(r"VERDICT:\s*([A-Za-z ]+)")
# Conservative stub/placeholder markers — presence means the output isn't real, so we skip the row.
_STUB_RE = re.compile(
    r"(NotImplementedError|raise\s+NotImplemented|#\s*TODO:\s*implement|your code here|lorem ipsum"
    r"|coming soon|placeholder implementation|<placeholder>)", re.IGNORECASE)


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _verdict(project_dir: Path) -> str | None:
    review = project_dir / "REVIEW.md"
    txt = _read_text(review) if review.exists() else None
    if not txt:
        return None
    m = _VERDICT_RE.search(txt)
    return m.group(1).strip().upper() if m else None


def _task_files_on_disk(project_dir: Path, files: list) -> tuple[dict | None, str | None]:
    """Read declared output files from disk. Returns (path->content, None) or (None, skip_reason)."""
    if not files:
        return None, "no_declared_files"
    out: dict[str, str] = {}
    for rel in files:
        if not isinstance(rel, str) or not rel.strip():
            return None, "bad_file_entry"
        fp = project_dir / rel
        if not fp.exists() or not fp.is_file():
            return None, "missing_output_file"
        content = _read_text(fp)
        if content is None:
            return None, "unreadable_output_file"
        if not content.strip():
            return None, "empty_output"
        if _STUB_RE.search(content):
            return None, "stub_marker"
        if secret_scrub.contains_secret(content):
            return None, "secret_in_output"
        out[rel] = content
    return out, None


def _split_for(key: str) -> str:
    h = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)
    return "heldout" if h % HELDOUT_EVERY == 0 else "train"


def _iter_project_dirs(projects_dir: Path):
    """Every dir containing a tasks_done.json (covers nested FORMAT-5 sub-projects)."""
    for td in sorted(projects_dir.rglob("tasks_done.json")):
        yield td.parent


def build_rows(projects_dir: Path = PROJECTS_DIR):
    """Returns (rows, manifest_dict). Pure: reads disk, no writes, no network."""
    rows: list[dict] = []
    exclusions: dict[str, int] = {}
    hashes: list[str] = []
    splits = {"train": 0, "heldout": 0}
    projects_scanned = 0
    source_files = 0

    def excl(reason: str):
        exclusions[reason] = exclusions.get(reason, 0) + 1

    for pdir in _iter_project_dirs(projects_dir):
        projects_scanned += 1
        rel_name = pdir.relative_to(projects_dir).as_posix()
        tasks = _read_json(pdir / "tasks_done.json")
        source_files += 1
        if not isinstance(tasks, list) or not tasks:
            excl("malformed_tasks")
            continue
        # Project-level high-confidence gate: passed final review + all tasks done.
        if _verdict(pdir) != "PASS":
            excl("no_pass_review")
            continue
        if not all(isinstance(t, dict) and t.get("status") == "done" for t in tasks):
            excl("tasks_incomplete")
            continue
        spec = _read_json(pdir / "spec.json") or {}
        if (pdir / "spec.json").exists():
            source_files += 1
        by_id = {t.get("id"): t for t in tasks if isinstance(t, dict)}

        for task in tasks:
            files, reason = _task_files_on_disk(pdir, task.get("files") or [])
            if reason:
                excl(reason)
                continue
            # Dependency context: the output files of this task's dependencies (what it could "see").
            dep_files: dict[str, dict] = {}
            for dep_id in (task.get("dependencies") or []):
                dep = by_id.get(dep_id)
                if not dep:
                    continue
                dep_out, dep_reason = _task_files_on_disk(pdir, dep.get("files") or [])
                if dep_out:
                    dep_files[dep_id] = dep_out

            task_id = task.get("id", "")
            split = _split_for(f"{rel_name}::{task_id}")
            splits[split] = splits.get(split, 0) + 1
            for content in files.values():
                hashes.append(hashlib.sha256(content.encode("utf-8")).hexdigest())

            row = {
                "instruction": _INSTRUCTION,
                "input": {
                    "task": {
                        "id": task_id,
                        "type": task.get("type", ""),
                        "objective": task.get("objective", ""),
                        "files": task.get("files", []),
                        "acceptance_criteria": task.get("acceptance_criteria", []),
                        "dependencies": task.get("dependencies", []),
                    },
                    "spec": {
                        "goal": spec.get("goal", ""),
                        "project_type": spec.get("project_type", ""),
                        "constraints": spec.get("constraints", []),
                        "architecture": spec.get("architecture", {}),
                    },
                    "context": {"project": rel_name, "stack": (spec.get("architecture") or {}).get("stack", "")},
                    "dependency_files": dep_files,
                    "failure_history": [],
                },
                "output": {"files": [{"path": p, "content": c} for p, c in files.items()]},
                "metadata": {
                    "source": "j-claw-session",
                    "project": rel_name,
                    "task_id": task_id,
                    "verification": task.get("verification", ""),
                    "model_used": task.get("model_used", ""),
                    "split": split,
                    "created_at": "",
                },
            }
            rows.append(secret_scrub.scrub_obj(row))  # scrub BEFORE the row leaves this function

    manifest = {
        "export_version": EXPORT_VERSION,
        "scrubber_version": secret_scrub.SCRUBBER_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "row_count": len(rows),
        "skipped_count": sum(exclusions.values()),
        "exclusions": dict(sorted(exclusions.items())),
        "projects_scanned": projects_scanned,
        "source_files_read": source_files,
        "splits": splits,
        "content_sha256": hashes,
    }
    return rows, secret_scrub.scrub_obj(manifest)


def export(out: Path = DEFAULT_OUT, projects_dir: Path = PROJECTS_DIR):
    rows, manifest = build_rows(projects_dir)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest_path = out.parent / f"manifest_{EXPORT_VERSION}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows, manifest, out, manifest_path


def main() -> int:
    ap = argparse.ArgumentParser(description="Export curated SFT rows from verified j-claw builds.")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output .jsonl path")
    ap.add_argument("--projects-dir", default=str(PROJECTS_DIR), help="harness/projects dir to scan")
    args = ap.parse_args()
    rows, manifest, out, manifest_path = export(Path(args.out), Path(args.projects_dir))
    print(f"wrote {len(rows)} row(s) -> {out}")
    print(f"manifest -> {manifest_path}")
    print(f"  projects scanned: {manifest['projects_scanned']}  "
          f"skipped: {manifest['skipped_count']}  splits: {manifest['splits']}")
    if manifest["exclusions"]:
        print(f"  exclusions: {manifest['exclusions']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
