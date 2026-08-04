"""#396 — a lint that times out must not read as a lint that passed.
#397 — module-level scratch must not survive across tests.

Both are the same bug in two places: an absence of output standing in for a
result that was never produced.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# #396 — silence means clean, and only clean
# ---------------------------------------------------------------------------

def _timeout_run(*a, **k):
    raise subprocess.TimeoutExpired(cmd=a[0] if a else "x", timeout=k.get("timeout", 5))


def test_lint_timeout_is_reported_not_swallowed(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    monkeypatch.setattr(subprocess, "run", _timeout_run)
    out = supertool._vim_render_lint(str(f))
    assert "TIMED OUT" in out
    assert out != ""


def test_lint_timeout_is_not_reported_as_a_lint_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """A slow runner is not a syntax error — the two must not share a marker."""
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    monkeypatch.setattr(subprocess, "run", _timeout_run)
    assert "POST-EDIT LINT FAILED" not in supertool._vim_render_lint(str(f))


def test_lint_timeout_is_configurable(tmp_path: Path, monkeypatch) -> None:
    seen = {}

    def _capture(*a, **k):
        seen["timeout"] = k.get("timeout")
        raise subprocess.TimeoutExpired(cmd="x", timeout=k.get("timeout", 5))

    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    monkeypatch.setenv("SUPERTOOL_LINT_TIMEOUT", "30")
    monkeypatch.setattr(subprocess, "run", _capture)
    out = supertool._vim_render_lint(str(f))
    assert seen["timeout"] == 30
    assert "30s" in out


def test_bad_lint_timeout_env_falls_back_to_the_default(
    tmp_path: Path, monkeypatch
) -> None:
    seen = {}

    def _capture(*a, **k):
        seen["timeout"] = k.get("timeout")
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    monkeypatch.setenv("SUPERTOOL_LINT_TIMEOUT", "not-a-number")
    monkeypatch.setattr(subprocess, "run", _capture)
    supertool._vim_render_lint(str(f))
    assert seen["timeout"] == supertool._LINT_TIMEOUT_DEFAULT


def test_the_suite_budget_does_not_move_the_product_default(monkeypatch) -> None:
    """#553 — conftest raises SUPERTOOL_LINT_TIMEOUT for this suite because a
    CI runner occasionally needs the room. That is a fact about the runner. The
    budget supertool ships with is unchanged, and reading the suite's value as
    the product's would be reading a workaround as a decision.
    """
    monkeypatch.delenv("SUPERTOOL_LINT_TIMEOUT", raising=False)
    assert supertool._LINT_TIMEOUT_DEFAULT == 5
    assert supertool._lint_timeout() == 5


def test_a_missing_binary_still_lints_silently(tmp_path: Path, monkeypatch) -> None:
    """No lint ran because none applies — that is the one silence we keep.

    Rewritten for #559. It used to reach this state by making the *spawn*
    fail, which is a different fact: there the binary was found and the file
    still went unchecked, and that now declines rather than falling silent
    (pinned below). The silence being defended here is the real one — nothing
    on this machine was ever going to check this file.
    """
    real_which = shutil.which
    monkeypatch.setattr(
        supertool.shutil, "which",
        lambda name, *a, **k: None if name == "php" else real_which(name, *a, **k),
    )
    f = tmp_path / "x.php"
    f.write_text("<?php\necho 'hi';\n")
    assert supertool._vim_render_lint(str(f)) == ""


def test_a_checker_that_cannot_be_started_declines(tmp_path: Path, monkeypatch) -> None:
    """#559 — the binary resolved, the file was not checked, and silence there
    would say "clean". Names the tool so the reader knows what did not run.
    """
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    out = supertool._vim_render_lint(str(f))
    assert "POST-EDIT LINT DECLINED" in out
    assert "py_compile" in out
    assert "NOT checked" in out
    assert "POST-EDIT LINT FAILED" not in out


def test_the_python_lint_never_bets_on_a_path_lookup(tmp_path: Path, monkeypatch) -> None:
    """#559/#529 — the literal "python3" is the name Windows resolves to the
    App Execution Alias stub (blocks instead of erroring) or to nothing.
    """
    seen: dict = {}

    def capture(*a, **k):
        seen["cmd"] = list(a[0] if a else k.get("args"))
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(subprocess, "run", capture)
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    supertool._vim_render_lint(str(f))
    assert seen["cmd"][0] == sys.executable


def test_a_clean_lint_still_says_so(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    out = supertool._vim_render_lint(str(f))
    assert "TIMED OUT" not in out and "FAILED" not in out


# ---------------------------------------------------------------------------
# #397 — the autouse reset must cover every module-level cache
# ---------------------------------------------------------------------------

_SENTINEL_DIR = "/__st397_sentinel__"


def test_a_first_test_pollutes_module_state() -> None:
    """Runs before its pair by file order — the pollution is the setup."""
    supertool._REPO_ROOT_WALK_CACHE[_SENTINEL_DIR] = ["junk"]
    supertool._MUTATION_ATTEMPTS[0] = 999
    supertool._FORMATTER_SKIPS.append("junk")
    supertool._WRITE_WARNINGS.append(("junk", "junk"))
    supertool._VALIDATOR_FINGERPRINT_CACHE[_SENTINEL_DIR] = "junk"


def test_b_the_next_test_sees_clean_module_state() -> None:
    assert _SENTINEL_DIR not in supertool._REPO_ROOT_WALK_CACHE
    assert supertool._MUTATION_ATTEMPTS[0] == 0
    assert supertool._FORMATTER_SKIPS == []
    assert supertool._WRITE_WARNINGS == []
    assert _SENTINEL_DIR not in supertool._VALIDATOR_FINGERPRINT_CACHE


def test_every_mutable_global_in_supertool_py_is_reset_or_deliberately_exempt() -> None:
    """The reset list is hand-maintained; this makes forgetting loud.

    A new module-level dict/list/set is per-run scratch until someone says
    otherwise. Adding one without a decision fails here.

    **This check covers `supertool.py` and nothing else**, and the name now says
    so (#686). Both the snapshot and the restore go through
    `getattr(supertool, name)`, so the reach stops at one module — while the old
    name, `test_every_mutable_global_...`, read as a statement about the
    codebase. `presets/` is held separately, and on a different contract, by
    `tests/test_preset_global_lifetimes_686.py`.
    """
    import conftest

    live = {
        name for name, val in vars(supertool).items()
        if name.startswith("_")
        and name == name.upper()
        and any(c.isalpha() for c in name)
        and isinstance(val, (dict, list, set))
    }
    accounted = set(conftest.RESET_GLOBALS) | set(conftest.RESET_EXEMPT_GLOBALS)
    assert live - accounted == set(), (
        "new module-level mutable global(s) in supertool.py not accounted for "
        "in conftest.RESET_GLOBALS / RESET_EXEMPT_GLOBALS — decide which, "
        "see issue #397. This check reads supertool.py only; globals under "
        "presets/ are covered by test_preset_global_lifetimes_686.py (#686)."
    )
