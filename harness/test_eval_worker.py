"""Unit tests for the eval scoring/verdict logic (training/eval_worker.py).

Pure logic — no Ollama, no real verifier (a fake contract scripts verify outcomes). Run from the
harness dir:  python test_eval_worker.py
"""
from __future__ import annotations

from training import eval_worker as E


# ---------------------------------------------------------------------------- path safety + static

def test_safe_rel():
    assert E._safe_rel("src/app.py") == "src/app.py"
    assert E._safe_rel("a/./b.py") == "a/b.py"
    for bad in ["/etc/passwd", "C:/x", "..\\..\\x", "a/../../b", "", "   ", None, 5]:
        assert E._safe_rel(bad) is None, bad
    print("ok: _safe_rel accepts clean rel paths, rejects absolute/.. /drive/empty")


def test_static_ok():
    assert E.static_ok([{"path": "a.py", "content": "def f():\n    return 1\n"}])
    assert not E.static_ok([])                                                   # nothing
    assert not E.static_ok([{"path": "a.py", "content": "# TODO: implement\n"}])  # stub marker
    assert not E.static_ok([{"path": "a.py", "content": "pass\n"}])              # trivial module
    assert not E.static_ok([{"path": "a.py", "content": "def ("}])              # syntax error
    assert not E.static_ok([{"path": "/abs.py", "content": "x=1"}])             # unsafe path
    assert E.static_ok([{"path": "x.txt", "content": "hello"}])                  # non-py non-empty ok
    print("ok: static_ok passes real code, fails stub/trivial/syntax/unsafe")


# ---------------------------------------------------------------------------- red/green discrimination

class _FakeContract:
    """Scripts verify_task purely from the declared file's content present in the workspace."""
    def __init__(self, declared):
        self.declared = declared

    def verify_task(self, task, root):
        from pathlib import Path
        p = Path(root) / self.declared
        if not p.exists():
            return "fail", "before: task file absent"      # 'before' must fail
        txt = p.read_text(encoding="utf-8")
        if "GOLD" in txt or "GOOD" in txt:
            return "pass", "ok"
        return "fail", "bad output"


def _vc(produced_content):
    fc = _FakeContract("a.py")
    gold = {"a.py": "GOLD\n", "b.py": "sibling\n"}
    return E.verify_candidate(fc, task=None, declared=["a.py"], gold=gold,
                              produced={"a.py": produced_content})


def test_red_green_pass_and_fail():
    assert _vc("GOOD\n") == "pass"     # before fails, gold passes, candidate good -> pass
    assert _vc("BAD\n") == "fail"      # before fails, gold passes, candidate bad  -> fail
    print("ok: red/green scores candidate pass/fail when controls hold")


def test_red_green_non_discriminating():
    class AlwaysPass:
        def verify_task(self, task, root):
            return "pass", "always"
    out = E.verify_candidate(AlwaysPass(), None, ["a.py"], {"a.py": "G", "b.py": "s"}, {"a.py": "X"})
    assert out == "non_discriminating"   # 'before' already passes -> task not needed
    print("ok: red/green marks non_discriminating when 'before' passes")


def test_red_green_control_fail():
    class GoldFails:
        # before fails (file absent), gold also fails -> verifier unreliable here
        def verify_task(self, task, root):
            return "fail", "never passes"
    out = E.verify_candidate(GoldFails(), None, ["a.py"], {"a.py": "G", "b.py": "s"}, {"a.py": "X"})
    assert out == "control_fail"
    print("ok: red/green marks control_fail when gold doesn't pass")


def test_red_green_unsafe_path():
    fc = _FakeContract("a.py")
    out = E.verify_candidate(fc, None, ["a.py"], {"a.py": "GOLD"}, {"../escape.py": "x"})
    assert out == "unsafe_path"
    print("ok: red/green rejects unsafe produced path")


# ---------------------------------------------------------------------------- aggregate + verdict

def _card(stack, parse=True, complete=True, outcome="pass"):
    return {"stack": stack, "parse_ok": parse, "files_complete": complete, "static_ok": True,
            "no_secret": True, "verify_outcome": outcome, "attempted": outcome in E._ATTEMPTED,
            "verify_pass": outcome == "pass", "parse_error": None}


def test_aggregate_attempted_metrics():
    cards = [_card("py", outcome="pass"), _card("py", outcome="fail"),
             _card("py", outcome="skipped"), _card("web", outcome="pass")]
    a = E.aggregate(cards, [1.0], [10], 0)
    assert a["verify_attempted_n"] == 3      # pass,fail,pass  (skipped excluded)
    assert a["verify_pass_n"] == 2
    assert a["skip_rate"] == round(1 / 4, 4)
    assert a["per_stack"]["py"]["attempted"] == 2 and a["per_stack"]["py"]["pass"] == 1
    print("ok: aggregate separates attempted vs skipped + per-stack")


def test_paired_wins_attempted():
    cand = [_card("py", outcome="pass"), _card("py", outcome="pass"), _card("py", outcome="skipped")]
    base = [_card("py", outcome="fail"), _card("py", outcome="pass"), _card("py", outcome="pass")]
    p = E.paired_wins_attempted(cand, base)
    assert p["comparable"] == 2 and p["candidate_only_pass"] == 1 and p["base_only_pass"] == 0
    print("ok: paired wins counted only on rows both attempted")


def test_verdict_fast_is_smoke_only():
    cand = E.aggregate([_card("py", outcome="not_attempted")], [], [], 0)
    v = E.comparative_verdict("fast", cand, cand, None, [], [], 0.02)
    assert v["kind"] == "smoke_passed" and not v["promotable"]
    print("ok: fast mode caps at smoke_passed (never promotable)")


def test_verdict_deep_insufficient_then_promotable():
    # too few attempted -> insufficient
    few = E.aggregate([_card("py", outcome="pass")], [], [], 0)
    v1 = E.comparative_verdict("deep", few, few, None, [_card("py")], [_card("py")], 0.02)
    assert v1["kind"] == "insufficient_evidence" and not v1["promotable"]

    # enough attempted, candidate beats base on paired attempted -> promotable
    E_MIN = E.MIN_ATTEMPTED_N
    cand_cards = [_card("py", outcome="pass") for _ in range(E_MIN)]
    base_cards = [_card("py", outcome="fail") for _ in range(E_MIN)]
    cand = E.aggregate(cand_cards, [], [], 0)
    base = E.aggregate(base_cards, [], [], 0)
    v2 = E.comparative_verdict("deep", cand, base, None, cand_cards, base_cards, 0.02)
    assert v2["kind"] == "compared" and v2["promotable"], v2

    # regression vs previous beyond budget -> blocked
    prev = E.aggregate([_card("py", outcome="pass") for _ in range(E_MIN)], [], [], 0)
    v3 = E.comparative_verdict("deep", base, base, prev, base_cards, base_cards, 0.02)
    assert not v3["promotable"]
    print("ok: deep verdict insufficient<N, promotable on paired win, blocks regression")


def main() -> int:
    tests = [
        test_safe_rel, test_static_ok,
        test_red_green_pass_and_fail, test_red_green_non_discriminating,
        test_red_green_control_fail, test_red_green_unsafe_path,
        test_aggregate_attempted_metrics, test_paired_wins_attempted,
        test_verdict_fast_is_smoke_only, test_verdict_deep_insufficient_then_promotable,
    ]
    for t in tests:
        t()
    print(f"\nALL {len(tests)} EVAL-LOGIC TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
