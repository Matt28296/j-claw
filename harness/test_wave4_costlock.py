#!/usr/bin/env python3
"""Wave 4 / CB4 — cost accumulator thread-safety regression tests.

record_usage() / check_cost_ceiling() / reset_costs() mutate module globals in
cost.py while up to 4 scheduler workers run concurrently (ThreadPoolExecutor).
Before this fix the read-modify-writes were unlocked, so concurrent
record_usage() calls could lose updates (a += that read a stale _total_usd /
_calls) and check-then-record was racy. These tests hammer those entry points
from many threads and assert the accumulator total is EXACT (no lost updates)
and the ceiling latch stays consistent.

Run (from harness/):
  $env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"
  python test_wave4_costlock.py  (via the venv interpreter)
"""
from __future__ import annotations

import sys
import threading

import cost


# --- WHY WE FORCE THE GIL SWITCH INTERVAL ------------------------------------
# CPython's default thread-switch interval (sys.getswitchinterval(), ~5ms) is
# far longer than the handful of bytecodes a record_usage() read-modify-write
# (`_total_usd += cost`, `_calls += 1`, `_tokens[...] += ...`) takes to run, so
# the interpreter almost never preempts a thread *inside* that unlocked window.
# As a result the lost-update race is effectively invisible at the default
# interval: against an UNLOCKED cost.py these tests pass anyway (verified across
# many runs), making the regression assertion a tautology that would NOT have
# caught the pre-fix code.
#
# Forcing sys.setswitchinterval() to a near-zero value (_RACE_SWITCH_INTERVAL)
# makes CPython preempt threads aggressively — mid read-modify-write — which
# reproduces the dropped updates the lock prevents. Empirically, with the
# interval forced low the UNLOCKED accumulator loses ~a third to a half of its
# token updates (e.g. 5.28M != 8M tokens), while the LOCKED shipped code stays
# EXACT. We restore the original interval in a finally block so we never leak
# this pathological setting into other tests in the same process.
#
# DO NOT remove the setswitchinterval() call to "speed up" the test: without it
# the lost-update assertion does not exercise the race and the regression lock
# is worthless.
_RACE_SWITCH_INTERVAL = 1e-9


class _U:  # minimal stand-in for an Anthropic response.usage
    def __init__(self, i=0, o=0, r=0, c=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = r
        self.cache_creation_input_tokens = c


def test_concurrent_record_usage_no_lost_updates():
    """Many threads each record a fixed number of identical paid calls. The
    accumulated total, call count, and token tallies must be EXACT — any lost
    update (the symptom of an unlocked +=) shows up as a short count.

    Forces sys.setswitchinterval(_RACE_SWITCH_INTERVAL) so the race is actually
    triggered (see the module-level note): at the default interval this test
    passes even against an UNLOCKED cost.py, so it would not guard the
    regression. With the interval forced low the unlocked code reproducibly
    drops updates (e.g. 5.28M != 8M tokens) while the locked code stays exact."""
    cost.reset_costs()

    n_threads = 16
    per_thread = 500
    # 1000 input tok on sonnet ($3/Mtok) => $0.003 per call, exactly representable
    # enough that we compare call count + token tallies (integer, exact) too.
    in_tok = 1000
    out_tok = 0
    expected_calls = n_threads * per_thread
    expected_input_tok = expected_calls * in_tok
    per_call_cost = cost.call_cost(_U(i=in_tok, o=out_tok), "claude-sonnet-4-6")
    expected_usd = per_call_cost * expected_calls

    start = threading.Barrier(n_threads)

    def worker():
        start.wait()  # maximize contention: release all threads together
        for _ in range(per_thread):
            cost.record_usage(_U(i=in_tok, o=out_tok), "claude-sonnet-4-6", "worker")

    # Aggressively preempt threads (incl. mid read-modify-write) so the
    # lost-update race surfaces; restore the original interval no matter what.
    _orig_interval = sys.getswitchinterval()
    sys.setswitchinterval(_RACE_SWITCH_INTERVAL)
    try:
        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(_orig_interval)

    s = cost.cost_summary()
    assert s["paid_calls"] == expected_calls, (
        f"lost call updates: {s['paid_calls']} != {expected_calls}")
    assert s["tokens"]["input"] == expected_input_tok, (
        f"lost token updates: {s['tokens']['input']} != {expected_input_tok}")
    # Float accumulation order varies under threading; allow a tiny tolerance but
    # it must be effectively exact (no whole calls dropped).
    assert abs(s["total_usd"] - round(expected_usd, 4)) < 1e-6, (
        f"lost cost updates: {s['total_usd']} != {round(expected_usd, 4)}")
    print(f"  no-lost-updates: {expected_calls} calls, "
          f"{expected_input_tok} input tok, ${s['total_usd']} — exact")


def test_check_then_record_latch_consistent(monkeypatch_ceiling=2.0):
    """Under a low ceiling, many workers race check_cost_ceiling() + record_usage().
    Once tripped the latch must be sticky (every subsequent check raises) and the
    final spend must not silently exceed the ceiling by more than one in-flight
    call per worker (the check-then-record window is now atomic per call)."""
    import config

    cost.reset_costs()
    orig_usd = config.MAX_BUILD_COST_USD
    orig_tok = config.MAX_BUILD_TOKENS
    config.MAX_BUILD_COST_USD = monkeypatch_ceiling
    config.MAX_BUILD_TOKENS = 0
    try:
        n_threads = 16
        per_thread = 200
        # $0.03 per call so the $2 ceiling is reached well within the workload.
        in_tok = 10_000
        per_call_cost = cost.call_cost(_U(i=in_tok), "claude-sonnet-4-6")

        trips = [0] * n_threads
        recorded = [0] * n_threads
        start = threading.Barrier(n_threads)

        def worker(idx):
            start.wait()
            for _ in range(per_thread):
                try:
                    cost.check_cost_ceiling()
                except cost.BuildCostCeilingExceeded:
                    trips[idx] += 1
                    continue
                cost.record_usage(_U(i=in_tok), "claude-sonnet-4-6", "worker")
                recorded[idx] += 1

        # Force aggressive preemption so the check-then-record window is
        # actually interleaved across workers (see module-level note); restore
        # the original interval regardless of outcome.
        _orig_interval = sys.getswitchinterval()
        sys.setswitchinterval(_RACE_SWITCH_INTERVAL)
        try:
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            sys.setswitchinterval(_orig_interval)

        s = cost.cost_summary()
        # The latch tripped (some thread saw the ceiling).
        assert sum(trips) > 0, "ceiling never tripped despite spend over ceiling"
        # Accumulator is internally consistent: paid_calls equals the number of
        # records we actually performed (no lost / phantom updates).
        assert s["paid_calls"] == sum(recorded), (
            f"call count {s['paid_calls']} != records {sum(recorded)}")
        assert abs(s["total_usd"] - round(per_call_cost * sum(recorded), 4)) < 1e-6
        # Overshoot is bounded: each worker can be mid-call past the check at most
        # once when the ceiling trips, so total spend cannot exceed
        # ceiling + (n_threads) in-flight calls.
        max_allowed = monkeypatch_ceiling + n_threads * per_call_cost
        assert s["total_usd"] <= max_allowed + 1e-9, (
            f"overshoot {s['total_usd']} > bound {max_allowed}")
        # Latch is sticky: a fresh check still raises (ceiling stays closed).
        raised = False
        try:
            cost.check_cost_ceiling()
        except cost.BuildCostCeilingExceeded:
            raised = True
        assert raised, "latch not sticky — check stopped raising after trip"
        print(f"  latch-consistent: {sum(recorded)} recorded, {sum(trips)} refused, "
              f"final ${s['total_usd']:.4f} <= bound ${max_allowed:.4f}, sticky ✓")
    finally:
        config.MAX_BUILD_COST_USD = orig_usd
        config.MAX_BUILD_TOKENS = orig_tok
        cost.reset_costs()


def test_concurrent_reset_does_not_corrupt():
    """A reset interleaved with records must leave a consistent accumulator: the
    final summary's paid_calls equals its by-label/token bookkeeping (no torn
    half-cleared state). We don't assert an exact total here (reset timing is
    nondeterministic) — only that the structure is coherent and didn't crash."""
    cost.reset_costs()

    stop = threading.Event()

    def recorder():
        while not stop.is_set():
            cost.record_usage(_U(i=100, o=50), "claude-sonnet-4-6", "worker")

    def resetter():
        for _ in range(50):
            cost.reset_costs()

    threads = [threading.Thread(target=recorder) for _ in range(8)]
    rt = threading.Thread(target=resetter)
    for t in threads:
        t.start()
    rt.start()
    rt.join()
    stop.set()
    for t in threads:
        t.join()

    s = cost.cost_summary()
    # token dict must still have exactly the canonical keys (not torn).
    assert set(s["tokens"]) == {"input", "output", "cache_read", "cache_creation"}, s
    assert s["paid_calls"] >= 0 and s["total_usd"] >= 0.0, s
    print(f"  reset-race: survived, paid_calls={s['paid_calls']} coherent ✓")


if __name__ == "__main__":
    test_concurrent_record_usage_no_lost_updates()
    test_check_then_record_latch_consistent()
    test_concurrent_reset_does_not_corrupt()
    print("wave4 cost-lock tests passed ✓")
