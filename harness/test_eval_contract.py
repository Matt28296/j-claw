"""Golden tests for the evaluation contract (harness/evaluation_contract.py).

These PIN the surface eval depends on, so a harness refactor that silently changes what j-claw
"sends a worker" / "accepts as output" / "treats as verified" fails here instead of corrupting an
eval. Run from the harness dir:  python test_eval_contract.py
"""
from __future__ import annotations

import sys as _sys

# Reconfigure BEFORE importing evaluation_contract -> worker (which creates a rich Console at import).
# On a cp1252 console the worker's truncation/salvage warnings would otherwise raise UnicodeEncodeError
# (Brain Finding 1). Production eval does this in main() before the lazy worker import.
try:
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import json

import evaluation_contract as C
import verification
import worker


def _row(stack="vanilla", files=("index.html",), ttype="scaffold", verification_method="html_auto"):
    return {
        "instruction": "impl",
        "input": {
            "task": {"id": "task-001", "type": ttype, "objective": "build the thing",
                     "files": list(files), "acceptance_criteria": ["works"], "dependencies": []},
            "spec": {"goal": "a thing", "architecture": {"stack": stack}},
            "context": {"project": "p", "stack": stack},
            "dependency_files": {},
        },
        "metadata": {"verification": verification_method, "project": "p", "split": "heldout"},
    }


def test_task_from_row_ok():
    t = C.task_from_dataset_row(_row())
    assert t.id == "task-001" and t.type == "scaffold"
    assert t.files == ["index.html"]
    assert t.verification == "html_auto"   # recovered from metadata.verification
    print("ok: task_from_dataset_row maps fields + verification method")


def test_task_from_row_fail_closed():
    for mutate, label in [
        (lambda r: r["input"]["task"].pop("type"), "missing type"),
        (lambda r: r["input"]["task"].pop("objective"), "missing objective"),
        (lambda r: r["input"]["task"].update(files=[]), "empty files"),
        (lambda r: r["input"]["task"].update(files="x"), "files not list"),
    ]:
        r = _row()
        mutate(r)
        try:
            C.task_from_dataset_row(r)
        except C.ContractError:
            continue
        raise AssertionError(f"expected ContractError on {label}")
    print("ok: task_from_dataset_row fails closed on missing critical fields")


def test_build_prompt_is_production_composition():
    row = _row(stack="react-vite", files=("src/App.jsx",))
    system, user, task, stack = C.build_worker_prompt(row)
    assert stack == "react-vite"
    # The contract must compose EXACTLY system = _SYSTEM_PROMPT + "\n" + _STACK_PROMPTS[stack].
    assert system == worker._SYSTEM_PROMPT + "\n" + worker._STACK_PROMPTS["react-vite"]
    payload = json.loads(user)
    assert payload["task"]["id"] == "task-001"
    assert payload["project_context"]["stack"] == "react-vite"
    print("ok: build_worker_prompt == production system+user composition")


def test_build_prompt_unknown_stack_falls_back_vanilla():
    system, _, _, stack = C.build_worker_prompt(_row(stack="totally-unknown"))
    assert stack == "totally-unknown"
    assert system.endswith(worker._STACK_PROMPTS["vanilla"])
    print("ok: unknown stack falls back to the vanilla stack prompt")


def test_parse_strict_and_salvage():
    task = C.task_from_dataset_row(_row(files=("a.py",)))
    code = "def add(a, b):\n    return a + b\n"   # >20 chars so the salvage length guard is satisfied
    strict = json.dumps({"files": [{"path": "a.py", "content": code}]})
    files, err = C.parse_worker_output(strict, task)
    assert err is None and files and files[0]["path"] == "a.py"
    # Single-file salvage from a chat wrapper (the real grok failure shape).
    wrapped = json.dumps({"response": f"```python\n{code}```"})
    files2, err2 = C.parse_worker_output(wrapped, task)
    assert err2 is None and files2 and "return a + b" in files2[0]["content"]
    # Unrecoverable garbage -> (None, error).
    files3, err3 = C.parse_worker_output("not json and no code", task)
    assert files3 is None and err3
    print("ok: parse_worker_output strict + single-file salvage + hard-miss")


def test_verify_task_skip_semantics():
    orig = verification.run_verification
    task = C.task_from_dataset_row(_row())
    try:
        verification.run_verification = lambda t, d: (True, "auto-passed: no tests present")
        assert C.verify_task(task, ".")[0] == "skipped"
        verification.run_verification = lambda t, d: (True, "all good")
        assert C.verify_task(task, ".")[0] == "pass"
        verification.run_verification = lambda t, d: (False, "boom")
        assert C.verify_task(task, ".")[0] == "fail"
        def _raise(t, d):
            raise RuntimeError("verifier exploded")
        verification.run_verification = _raise
        assert C.verify_task(task, ".")[0] == "error"
    finally:
        verification.run_verification = orig
    # sanity: SKIP_PREFIX is the real constant
    assert C.SKIP_PREFIX == verification.SKIP_PREFIX
    print("ok: verify_task maps pass/fail/skipped(auto-passed)/error")


def test_versions_shape():
    v = C.versions()
    assert set(v) == {"contract", "prompt", "parser", "verifier"}
    assert all(isinstance(x, int) for x in v.values())
    print("ok: versions() shape")


def main() -> int:
    tests = [
        test_task_from_row_ok, test_task_from_row_fail_closed,
        test_build_prompt_is_production_composition, test_build_prompt_unknown_stack_falls_back_vanilla,
        test_parse_strict_and_salvage, test_verify_task_skip_semantics, test_versions_shape,
    ]
    for t in tests:
        t()
    print(f"\nALL {len(tests)} CONTRACT TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
