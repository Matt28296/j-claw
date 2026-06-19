"""Wave 4 / A4 — config env-parse hardening regression tests.

Locks the fixes for the ENV finding:
  * bare ``float()/int()`` env parses crashed the harness AT IMPORT on
    empty / non-numeric values (``MAX_BUILD_COST_USD=`` or ``=abc`` -> ValueError);
  * ``MAX_FORMAT5_DEPTH=0`` silently disabled ALL decomposition because
    ``_subproject_decomposition_allowed(0)`` computes ``0 < 0 == False``.

These tests do NOT touch test_llm_layers.py (shared, edited by no one this wave)
and run standalone:  python test_wave4_env.py
"""
from __future__ import annotations

import importlib
import os
import sys


# --- tiny test harness (mirrors the repo's stdlib-only assert style) ----------
_PASSED = 0
_FAILED = 0


def _check(cond: bool, msg: str) -> None:
    global _PASSED, _FAILED
    if cond:
        _PASSED += 1
    else:
        _FAILED += 1
        print(f"  FAIL: {msg}")


def _reload_config_with(env: dict[str, str | None]):
    """Reimport config.py with the given env overrides applied, then restore.

    Returns the freshly-imported module so callers can read the resolved
    module-level constants exactly as the harness would at startup."""
    saved: dict[str, str | None] = {}
    for k, v in env.items():
        saved[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        sys.modules.pop("config", None)
        import config  # noqa: WPS433  (intentional re-import under patched env)
        return importlib.reload(config)
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


# --- the helpers themselves ---------------------------------------------------
def test_helpers_directly() -> None:
    cfg = _reload_config_with({})
    # _float_env: empty / abc fall back to default; negative clamps to floor
    _check(cfg._float_env("__NOPE__", 5.0, lo=0) == 5.0, "_float_env unset -> default")
    os.environ["__T_FLOAT__"] = ""
    _check(cfg._float_env("__T_FLOAT__", 5.0, lo=0) == 5.0, "_float_env empty -> default")
    os.environ["__T_FLOAT__"] = "abc"
    _check(cfg._float_env("__T_FLOAT__", 5.0, lo=0) == 5.0, "_float_env non-numeric -> default")
    os.environ["__T_FLOAT__"] = "-3.5"
    _check(cfg._float_env("__T_FLOAT__", 5.0, lo=0) == 0.0, "_float_env negative -> floor 0")
    os.environ["__T_FLOAT__"] = "2.5"
    _check(cfg._float_env("__T_FLOAT__", 5.0, lo=0) == 2.5, "_float_env valid passthrough")
    os.environ.pop("__T_FLOAT__", None)

    os.environ["__T_INT__"] = ""
    _check(cfg._int_env("__T_INT__", 7, lo=1) == 7, "_int_env empty -> default")
    os.environ["__T_INT__"] = "abc"
    _check(cfg._int_env("__T_INT__", 7, lo=1) == 7, "_int_env non-numeric -> default")
    os.environ["__T_INT__"] = "0"
    _check(cfg._int_env("__T_INT__", 7, lo=1) == 1, "_int_env 0 below floor -> floor 1")
    os.environ["__T_INT__"] = "-4"
    _check(cfg._int_env("__T_INT__", 7, lo=1) == 1, "_int_env negative -> floor 1")
    os.environ["__T_INT__"] = "9"
    _check(cfg._int_env("__T_INT__", 7, lo=1) == 9, "_int_env valid passthrough")
    os.environ.pop("__T_INT__", None)


# --- import-crash + clamp matrix on the real constants ------------------------
def test_cost_vars_never_crash_import() -> None:
    matrix = {
        "MAX_BUILD_COST_USD": ("MAX_BUILD_COST_USD", 5.0, 0.0),
        "BUILD_COST_WARN_FRAC": ("BUILD_COST_WARN_FRAC", 0.75, 0.0),
        "MAX_BUILD_TOKENS": ("MAX_BUILD_TOKENS", 0, 0),
    }
    for env_name, (attr, default, floor) in matrix.items():
        for bad in ("", "abc"):
            cfg = _reload_config_with({env_name: bad})
            _check(
                getattr(cfg, attr) == default,
                f"{env_name}={bad!r} -> default {default} (got {getattr(cfg, attr)})",
            )
        # '0' is valid for these (floor 0) -> 0
        cfg = _reload_config_with({env_name: "0"})
        _check(getattr(cfg, attr) == floor, f"{env_name}='0' -> {floor}")
        # negative clamps up to floor 0
        cfg = _reload_config_with({env_name: "-5"})
        _check(getattr(cfg, attr) == floor, f"{env_name}='-5' -> floor {floor}")


def test_format5_depth_floor() -> None:
    # empty / non-numeric -> default 3, no crash
    for bad in ("", "abc"):
        cfg = _reload_config_with({"MAX_FORMAT5_DEPTH": bad})
        _check(cfg.MAX_FORMAT5_DEPTH == 3, f"MAX_FORMAT5_DEPTH={bad!r} -> default 3")
    # THE KEY ASSERTION: '0' must resolve to >= 1 so top-level FORMAT-5 still works.
    cfg = _reload_config_with({"MAX_FORMAT5_DEPTH": "0"})
    _check(
        cfg.MAX_FORMAT5_DEPTH >= 1,
        f"MAX_FORMAT5_DEPTH='0' must clamp to >=1 (got {cfg.MAX_FORMAT5_DEPTH})",
    )
    _check(cfg.MAX_FORMAT5_DEPTH == 1, "MAX_FORMAT5_DEPTH='0' -> floor 1 exactly")
    # negative also floored to 1
    cfg = _reload_config_with({"MAX_FORMAT5_DEPTH": "-2"})
    _check(cfg.MAX_FORMAT5_DEPTH == 1, "MAX_FORMAT5_DEPTH='-2' -> floor 1")
    # valid >1 passthrough
    cfg = _reload_config_with({"MAX_FORMAT5_DEPTH": "5"})
    _check(cfg.MAX_FORMAT5_DEPTH == 5, "MAX_FORMAT5_DEPTH='5' passthrough")


def test_top_level_decomposition_still_allowed_with_depth_env_zero() -> None:
    """Concrete proof the =0 footgun is gone: with MAX_FORMAT5_DEPTH=0 in the
    env, top-level decomposition (depth 0 < clamped MAX) must still be allowed."""
    cfg = _reload_config_with({"MAX_FORMAT5_DEPTH": "0"})
    top_level_depth = 0
    allowed = top_level_depth < cfg.MAX_FORMAT5_DEPTH  # the live guard's predicate
    _check(allowed, "top-level FORMAT-5 must be allowed even when MAX_FORMAT5_DEPTH=0 in env")


def test_other_int_vars_survive_garbage() -> None:
    # spot-check a representative spread of the other hardened parses
    cfg = _reload_config_with(
        {
            "MAX_PARALLEL_WORKERS": "",
            "MAX_PAID_WORKER_CALLS": "abc",
            "DASHBOARD_PORT": "",
            "ORCHESTRATOR_TIMEOUT": "xyz",
            "MAX_TASKS": "0",
        }
    )
    _check(cfg.MAX_PARALLEL_WORKERS == 4, "MAX_PARALLEL_WORKERS empty -> default 4")
    _check(cfg.MAX_PAID_WORKER_CALLS == 15, "MAX_PAID_WORKER_CALLS abc -> default 15")
    _check(cfg.DASHBOARD_PORT == 8765, "DASHBOARD_PORT empty -> default 8765")
    _check(cfg.ORCHESTRATOR_TIMEOUT == 300, "ORCHESTRATOR_TIMEOUT xyz -> default 300")
    _check(cfg.MAX_TASKS == 1, "MAX_TASKS='0' -> floor 1")


def main() -> int:
    test_helpers_directly()
    test_cost_vars_never_crash_import()
    test_format5_depth_floor()
    test_top_level_decomposition_still_allowed_with_depth_env_zero()
    test_other_int_vars_survive_garbage()
    # leave config in a clean default state for any later import
    _reload_config_with({})
    print(f"\ntest_wave4_env: {_PASSED} passed, {_FAILED} failed")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
