"""memory_lint.py — staleness pre-flight for a project's project_memory/.

WARN-ONLY by design: it detects memory that has drifted from the code on disk and
reports it, but NEVER mutates memory and NEVER blocks the build. (Auto-prune and
fail-closed are deliberately deferred until we have observed real staleness
patterns — see the settled design debate.) To make sure advisory findings are not
lost in scrollback of an unattended run, every pass writes a structured
``project_memory/lint_report.json`` artifact and returns a count the caller can
surface in the build summary.

Why this matters: context_builder.py feeds workers a ~4K-token subset of
project_memory/ per task. If a memory file cites a source file that was deleted,
or api_contracts.md describes an endpoint that no longer exists in the source
tree, the worker is implementing against a ghost interface. On the FORMAT-5
decomposing path that is worse — sub-projects SHARE api_contracts.md across
worktrees, so one stale contract corrupts multiple sub-projects at once.

Checks (all advisory):
  1. missing_file_citation — a memory file cites a source path that is not on disk.
  2. contract_no_source    — a non-deprecated `METHOD /route` in api_contracts.md
                             whose route string appears nowhere in the source tree.
  3. orphan_meta           — a `<name>.meta.json` with no matching memory file
                             (or a memory file whose .meta.json is missing).

Run standalone:   python memory_lint.py <project_dir>
As a pre-flight:  lint_project_memory(project_dir)  → returns a report dict
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

# Memory text files worth scanning for source-file citations.
_MEMORY_TEXT_FILES = (
    "api_contracts.md", "architecture.md", "known_issues.md",
    "project_summary.md", "coding_standards.md",
)
# Source-tree dirs that are never project source.
_SKIP_DIRS = {
    "project_memory", "runtime_memory", ".git", "node_modules", ".venv",
    "__pycache__", "dist", "build", ".pytest_cache", ".playwright",
    ".jclaw_worktrees", "architecture_decisions",
}
# Extensions that look like real source files (used to keep citation matching
# from firing on every ".md"/".json" mention in prose).
_SOURCE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".php",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".html", ".css", ".scss", ".vue", ".svelte",
    ".sh", ".sol", ".gd", ".sql", ".toml", ".yaml", ".yml", ".kt", ".swift",
}
# Path-like tokens: a/b/c.ext or bare file.ext. Allow ./ and word/dash/dot segments.
_CITATION_RE = re.compile(r"(?<![\w/])\.?/?(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,6}")
# `METHOD /route` inside backticks, optionally struck through (~~ = deprecated).
_ENDPOINT_RE = re.compile(r"(~~)?`(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+?)`")


def _iter_source_files(project_dir: Path):
    """Yield (relative_posix_path, basename) for every source file in the project,
    skipping memory dirs, VCS, deps and build output."""
    for p in project_dir.rglob("*"):
        if not p.is_file():
            continue
        parts = set(p.relative_to(project_dir).parts)
        if parts & _SKIP_DIRS:
            continue
        yield p.relative_to(project_dir).as_posix(), p.name


def _source_index(project_dir: Path) -> tuple[set[str], set[str]]:
    """Return (set of relative posix paths, set of basenames) for the source tree."""
    rels: set[str] = set()
    names: set[str] = set()
    for rel, name in _iter_source_files(project_dir):
        rels.add(rel)
        names.add(name)
    return rels, names


def _source_blob(project_dir: Path, limit: int = 3_000_000) -> str:
    """Concatenate source-file text (best-effort) so contract routes can be searched.
    Bounded so a huge tree can't blow memory; binary/oversize files are skipped."""
    chunks: list[str] = []
    total = 0
    for rel, _name in _iter_source_files(project_dir):
        fp = project_dir / rel
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        total += len(text)
        if total > limit:
            break
        chunks.append(text)
    return "\n".join(chunks)


def _extract_citations(text: str) -> set[str]:
    """Pull source-file-looking path tokens out of memory text."""
    out: set[str] = set()
    for m in _CITATION_RE.findall(text):
        token = m.strip().lstrip("./")
        if not token:
            continue
        ext = "." + token.rsplit(".", 1)[-1].lower()
        if ext not in _SOURCE_EXTS:
            continue
        out.add(token)
    return out


def _citation_exists(token: str, rels: set[str], names: set[str]) -> bool:
    """A citation is satisfied if its full relative path OR its basename is on disk
    (basename fallback avoids false positives when memory cites a bare filename)."""
    if token in rels:
        return True
    base = token.rsplit("/", 1)[-1]
    return base in names


def lint_project_memory(project_dir: Path, write_report: bool = True) -> dict:
    """Scan project_dir/project_memory/ for staleness vs the source tree.

    Returns a report dict: {"project", "memory_present", "findings": [...],
    "counts": {kind: n}, "total"}. Each finding is
    {"severity": "warn", "kind", "source", "detail"}. Never raises on stale memory;
    only advisory. Writes project_memory/lint_report.json when write_report=True."""
    project_dir = Path(project_dir)
    mem = project_dir / "project_memory"
    report: dict = {
        "project": project_dir.name,
        "memory_present": mem.is_dir(),
        "findings": [],
        "counts": {},
        "total": 0,
    }
    if not mem.is_dir():
        return report

    findings: list[dict] = report["findings"]
    rels, names = _source_index(project_dir)

    # ── Check 1: file citations that no longer exist on disk ──────────────────
    for fname in _MEMORY_TEXT_FILES:
        fpath = mem / fname
        if not fpath.exists():
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for token in sorted(_extract_citations(text)):
            if not _citation_exists(token, rels, names):
                findings.append({
                    "severity": "warn", "kind": "missing_file_citation",
                    "source": fname,
                    "detail": f"cites '{token}' but no such file exists in the project",
                })

    # ── Check 2: api_contracts endpoints with no trace in source ──────────────
    contracts = mem / "api_contracts.md"
    if contracts.exists():
        try:
            ctext = contracts.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            ctext = ""
        if ctext:
            blob = _source_blob(project_dir)
            for struck, method, route in _ENDPOINT_RE.findall(ctext):
                if struck:  # ~~...~~ = deprecated; not expected in source
                    continue
                needle = route.split("?", 1)[0].rstrip("/") or route
                if needle and needle not in blob:
                    findings.append({
                        "severity": "warn", "kind": "contract_no_source",
                        "source": "api_contracts.md",
                        "detail": f"contract '{method} {route}' — route not found in source tree",
                    })

    # ── Check 3: orphaned .meta.json (cheap consistency) ──────────────────────
    for meta in sorted(mem.glob("*.meta.json")):
        owner = meta.name[: -len(".meta.json")]
        if not (mem / owner).exists():
            findings.append({
                "severity": "warn", "kind": "orphan_meta",
                "source": meta.name,
                "detail": f"metadata for '{owner}' but that memory file is gone",
            })
    for fname in _MEMORY_TEXT_FILES:
        if (mem / fname).exists() and not (mem / f"{fname}.meta.json").exists():
            findings.append({
                "severity": "warn", "kind": "orphan_meta",
                "source": fname,
                "detail": f"'{fname}' has no .meta.json (version tracking missing)",
            })

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    report["counts"] = counts
    report["total"] = len(findings)

    if write_report:
        try:
            (mem / "lint_report.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8")
        except OSError:
            pass
    return report


def format_report(report: dict) -> str:
    """One-line-per-finding human summary."""
    if not report.get("memory_present"):
        return "memory-lint: no project_memory/ — skipped."
    total = report.get("total", 0)
    if total == 0:
        return "memory-lint: ✓ no staleness detected."
    lines = [f"memory-lint: ⚠ {total} staleness warning(s) (advisory):"]
    for f in report.get("findings", []):
        lines.append(f"  [{f['kind']}] {f['source']}: {f['detail']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python memory_lint.py <project_dir>", file=sys.stderr)
        return 2
    project_dir = Path(argv[0])
    if not project_dir.is_dir():
        print(f"not a directory: {project_dir}", file=sys.stderr)
        return 2
    report = lint_project_memory(project_dir)
    print(format_report(report))
    # WARN-ONLY: always exit 0 — staleness never blocks.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
