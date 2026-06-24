"""Phase-2 (9070 XT) tests — dataset export quality gates + secret scrubbing.

Deterministic, no network, no torch. Builds synthetic project dirs in a temp folder. Run from harness:
    python test_export_dataset.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from training import export_dataset as ed
from training import secret_scrub as ss


def _mk_project(root: Path, name: str, *, verdict: str, tasks: list, files: dict):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "REVIEW.md").write_text(f"# Final Code Review\n\nVERDICT: {verdict}\n", encoding="utf-8")
    (d / "tasks_done.json").write_text(json.dumps(tasks), encoding="utf-8")
    (d / "spec.json").write_text(json.dumps(
        {"goal": "demo goal", "project_type": "web",
         "constraints": ["vanilla only"], "architecture": {"stack": "vanilla"}}), encoding="utf-8")
    for rel, content in files.items():
        fp = d / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    return d


class SecretScrubTests(unittest.TestCase):
    def test_masks_keys_email_paths(self):
        self.assertNotIn("sk-ant", ss.scrub_text("k=sk-ant-abcdef0123456789ABCDEF"))
        self.assertIn("[EMAIL]", ss.scrub_text("ping me@example.com"))
        self.assertIn("[USER]", ss.scrub_text(r"C:\Users\Tyler\x.py"))

    def test_scrub_obj_recurses_and_tolerates_nonstrings(self):
        out = ss.scrub_obj({"x": ["bearer abcdef0123456789xyz", 1, None]})
        self.assertEqual(out["x"][0], ss.REDACTED)
        self.assertEqual(out["x"][1], 1)
        self.assertIsNone(out["x"][2])

    def test_contains_secret(self):
        self.assertTrue(ss.contains_secret("AIza0123456789012345678901234567890"))
        self.assertFalse(ss.contains_secret("plain code line"))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _done(self, tid, files, **over):
        t = {"id": tid, "type": "frontend", "objective": "do it", "files": files,
             "dependencies": [], "acceptance_criteria": [], "verification": "none", "status": "done"}
        t.update(over)
        return t

    def test_only_pass_projects_with_real_files_export(self):
        # PASS project with two done tasks (task-002 depends on task-001); an email in an objective.
        _mk_project(
            self.tmp, "pass_proj", verdict="PASS",
            tasks=[
                self._done("task-001", ["app.py"], objective="contact dev@example.com for app.py"),
                self._done("task-002", ["util.py"], dependencies=["task-001"]),
            ],
            files={"app.py": "print('hi')\n", "util.py": "def u():\n    return 1\n"},
        )
        # excluded: failed review
        _mk_project(self.tmp, "fail_proj", verdict="ISSUES FOUND",
                    tasks=[self._done("task-001", ["a.py"])], files={"a.py": "print(1)\n"})
        # excluded: a task not done
        _mk_project(self.tmp, "incomplete_proj", verdict="PASS",
                    tasks=[self._done("task-001", ["a.py"]),
                           {"id": "task-002", "files": ["b.py"], "status": "pending"}],
                    files={"a.py": "print(1)\n", "b.py": "print(2)\n"})
        # task skipped: secret embedded in output
        _mk_project(self.tmp, "secret_proj", verdict="PASS",
                    tasks=[self._done("task-001", ["cfg.py"])],
                    files={"cfg.py": "API_KEY = 'sk-ant-abcdef0123456789ABCDEF'\n"})
        # task skipped: declared file missing on disk
        _mk_project(self.tmp, "missing_proj", verdict="PASS",
                    tasks=[self._done("task-001", ["gone.py"])], files={})

        rows, manifest = ed.build_rows(projects_dir=self.tmp)

        # Only pass_proj's two tasks survive.
        self.assertEqual(len(rows), 2, manifest["exclusions"])
        projects = {r["metadata"]["project"] for r in rows}
        self.assertEqual(projects, {"pass_proj"})

        # output content is read from DISK
        app_row = next(r for r in rows if r["metadata"]["task_id"] == "task-001")
        self.assertEqual(app_row["output"]["files"][0]["content"], "print('hi')\n")
        # the email in the objective was scrubbed before write
        self.assertNotIn("dev@example.com", app_row["input"]["task"]["objective"])
        self.assertIn("[EMAIL]", app_row["input"]["task"]["objective"])

        # dependency context is populated for task-002
        util_row = next(r for r in rows if r["metadata"]["task_id"] == "task-002")
        self.assertIn("task-001", util_row["input"]["dependency_files"])

        # exclusions recorded the right reasons
        ex = manifest["exclusions"]
        self.assertGreaterEqual(ex.get("no_pass_review", 0), 1)
        self.assertGreaterEqual(ex.get("tasks_incomplete", 0), 1)
        self.assertGreaterEqual(ex.get("secret_in_output", 0), 1)
        self.assertGreaterEqual(ex.get("missing_output_file", 0), 1)
        self.assertEqual(manifest["scrubber_version"], ss.SCRUBBER_VERSION)
        self.assertEqual(manifest["row_count"], 2)

    def test_export_writes_files(self):
        _mk_project(self.tmp, "p", verdict="PASS",
                    tasks=[self._done("task-001", ["a.py"])], files={"a.py": "x = 1\n"})
        out = self.tmp / "out" / "curated.jsonl"
        rows, manifest, out_path, manifest_path = ed.export(out=out, projects_dir=self.tmp)
        self.assertTrue(out_path.exists() and manifest_path.exists())
        lines = out_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["metadata"]["project"], "p")


if __name__ == "__main__":
    unittest.main(verbosity=2)
