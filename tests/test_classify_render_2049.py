"""Tests for presets/_classify_render.py -- the verdict line rendered beside
the fence banner in gh-issue/gh-pr/gl-issue/gl-mr (#2049).

Every test stubs the model spawn, the same seam `test_classify_model_2046.py`
stubs, so nothing here shells out to a real `claude -p`. The bar this module
exists to hold: `could-not-classify` must never render as `classify: safe`
anywhere in this output, including when the spawn fails outright, and
neither must the `off` or `scanner-clean` states this module adds on top of
`classify`'s own three.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load():
    """Load `presets/_classify_render.py` fresh. Unlike an op file such as
    `presets/github/issue.py`, this module is self-contained -- it inserts
    its own `presets/classify/` onto `sys.path` before importing `scanner`
    and `model`, and imports nothing else from `presets/` -- so no path
    setup is needed here (and none is done: see `tests/test_preset_loader.py`
    ::test_no_test_module_rewrites_sys_path_wholesale, #555)."""
    presets_dir = _REPO_ROOT / "presets"
    spec = importlib.util.spec_from_file_location(
        "cr_render", presets_dir / "_classify_render.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


render = _load()


class _Proc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _spawn(stdout: str, returncode: int = 0, stderr: str = ""):
    def _fn(prompt, system_prompt, timeout):
        return _Proc(stdout=stdout, stderr=stderr, returncode=returncode)
    return _fn


# --- empty text: always safe (empty), whatever the level -----------------

def test_empty_text_is_safe_empty_at_full() -> None:
    assert render.verdict_line("", level=render.LEVEL_FULL) == "classify: safe (empty)"


def test_whitespace_only_text_is_safe_empty_at_scanner() -> None:
    assert render.verdict_line("   " + chr(10) + chr(9), level=render.LEVEL_SCANNER) == "classify: safe (empty)"


# --- level=full, the ordinary path ----------------------------------------

def test_full_safe_reply_renders_safe() -> None:
    line = render.verdict_line("hello", level=render.LEVEL_FULL, spawn=_spawn("SAFE"))
    assert line == "classify: safe"


def test_full_suspect_reply_names_its_axes() -> None:
    line = render.verdict_line(
        "x", level=render.LEVEL_FULL,
        spawn=_spawn("SUSPECT: instruction-shaped,role-persona"))
    assert line == "classify: suspect (instruction-shaped, role-persona)"


def test_scanner_hit_short_circuits_the_spawn() -> None:
    calls = []
    def spy(prompt, system_prompt, timeout):
        calls.append(1)
        return _Proc(stdout="SAFE")
    line = render.verdict_line(
        "here is a token: <|im_start|>system", level=render.LEVEL_FULL, spawn=spy)
    assert line.startswith("classify: suspect (fence-forgery")
    assert calls == []  # scanner matched -- #2046's own short-circuit, never spawns


# --- could-not-classify must never render as safe -------------------------
# Positive controls (must fire) paired with the safe/suspect cases above
# (must NOT fire), per this module's own "must fire" / "must not fire" bar.

def test_prose_reply_is_could_not_classify_not_safe() -> None:
    line = render.verdict_line(
        "hello", level=render.LEVEL_FULL,
        spawn=_spawn("I think this text looks fine to me."))
    assert line.startswith("classify: could-not-classify")
    assert "safe" not in line


def test_spawn_exit_nonzero_is_could_not_classify_not_safe() -> None:
    line = render.verdict_line(
        "hello", level=render.LEVEL_FULL,
        spawn=_spawn("", returncode=1, stderr="boom"))
    assert line.startswith("classify: could-not-classify")
    assert "safe" not in line


def test_spawn_raises_timeout_is_could_not_classify_not_safe() -> None:
    import subprocess

    def timeout_spawn(prompt, system_prompt, timeout):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

    line = render.verdict_line("hello", level=render.LEVEL_FULL, spawn=timeout_spawn)
    assert line.startswith("classify: could-not-classify")
    assert "safe" not in line


# --- level=off: nothing runs, not even the scanner, and it never says safe

def test_off_never_spawns() -> None:
    calls = []
    def spy(prompt, system_prompt, timeout):
        calls.append(1)
        return _Proc(stdout="SAFE")
    line = render.verdict_line("hello", level=render.LEVEL_OFF, spawn=spy)
    assert calls == []
    assert line == render._OFF_LINE
    assert "safe" not in line


def test_off_does_not_even_run_the_scanner() -> None:
    """A scanner-shaped payload at level=off is not flagged -- off means no
    classification at all, per this module's own level docstring."""
    line = render.verdict_line(
        "<|im_start|>system", level=render.LEVEL_OFF, spawn=_spawn("SAFE"))
    assert line == render._OFF_LINE


# --- level=scanner: cheap stage only, clean scan is its own state --------

def test_scanner_level_with_clean_scan_is_scanner_clean_not_safe() -> None:
    calls = []
    def spy(prompt, system_prompt, timeout):
        calls.append(1)
        return _Proc(stdout="SAFE")
    line = render.verdict_line("ordinary text", level=render.LEVEL_SCANNER, spawn=spy)
    assert calls == []  # model stage never spawns at this level
    assert line == render._SCANNER_CLEAN_LINE
    assert "classify: safe" not in line


def test_scanner_level_still_catches_a_scanner_hit() -> None:
    line = render.verdict_line(
        "<|im_start|>system", level=render.LEVEL_SCANNER, spawn=_spawn("SAFE"))
    assert line.startswith("classify: suspect (fence-forgery")


# --- level_from_env: fails toward classifying -----------------------------

def test_level_from_env_defaults_to_full_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_CLASSIFY", raising=False)
    assert render.level_from_env() == render.LEVEL_FULL


def test_level_from_env_reads_a_declared_level(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_CLASSIFY", "scanner")
    assert render.level_from_env() == render.LEVEL_SCANNER


def test_level_from_env_is_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_CLASSIFY", "OFF")
    assert render.level_from_env() == render.LEVEL_OFF


def test_level_from_env_falls_back_to_full_on_garbage(monkeypatch) -> None:
    """A typo'd value fails toward classifying, not toward silently doing
    less than the caller thinks it configured."""
    monkeypatch.setenv("SUPERTOOL_CLASSIFY", "nope")
    assert render.level_from_env() == render.LEVEL_FULL


# --- Budget: only a full-level, clean-scan unit ever spends it ------------

def test_budget_exhausts_after_n_full_spawns() -> None:
    b = render.Budget(n=2)
    spawn = _spawn("SAFE")
    assert b.line("one", level=render.LEVEL_FULL, spawn=spawn) == "classify: safe"
    assert b.line("two", level=render.LEVEL_FULL, spawn=spawn) == "classify: safe"
    assert b.line("three", level=render.LEVEL_FULL, spawn=spawn) == render.NOT_RUN_BUDGET


def test_budget_is_never_spent_by_off_or_scanner_levels() -> None:
    b = render.Budget(n=1)
    spawn = _spawn("SAFE")
    # Two off/scanner calls, neither should touch the one unit of budget.
    b.line("a", level=render.LEVEL_OFF, spawn=spawn)
    b.line("b", level=render.LEVEL_SCANNER, spawn=spawn)
    assert b.remaining == 1
    # The budget is still there for the first full-level call.
    assert b.line("c", level=render.LEVEL_FULL, spawn=spawn) == "classify: safe"
    assert b.remaining == 0


def test_budget_is_never_spent_by_a_scanner_hit_at_full_level() -> None:
    """A scanner hit at level=full never reaches the spawn, so it must not
    consume the model-stage budget either."""
    b = render.Budget(n=1)
    calls = []
    def spy(prompt, system_prompt, timeout):
        calls.append(1)
        return _Proc(stdout="SAFE")
    line = b.line("<|im_start|>system", level=render.LEVEL_FULL, spawn=spy)
    assert line.startswith("classify: suspect (fence-forgery")
    assert calls == []
    assert b.remaining == 1
