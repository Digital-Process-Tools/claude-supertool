"""guard-selftest.py's bash_candidates() must not resolve a cwd-only match
via raw `shutil.which()` (#2610).

`hooks/guard-selftest.py:bash_candidates()` put raw `shutil.which("bash")`
first in its candidate list, and `first_bash_that_runs_a_script()` spawns
each candidate directly -- the `supertool-bash-ok` probe only affects
*selection order*, not whether the spawn happens. On Windows,
`shutil.which()` inserts the current directory ahead of every real `PATH`
entry, so a repo-planted `bash.exe`/`bash.cmd` at cwd root would be
resolved and run. Because this script's whole premise is diagnosing a host
with no real bash, a planted file may be the ONLY candidate that resolves.

Same fixture shape as `tests/test_which_excludes_cwd_2575.py`: monkeypatch
`PATH` to place a shim at a directory equal to `os.curdir`, in the position
CPython's own curdir-insertion would put one, and load `guard-selftest.py`
by path (it is a dash-named script, not an importable module).
"""
from __future__ import annotations

import importlib.util
import os
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SELFTEST = _ROOT / "hooks" / "guard-selftest.py"

_spec = importlib.util.spec_from_file_location("guard_selftest_2610", _SELFTEST)
selftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(selftest)

TOOL = "bash"


def _shim(d: "pathlib.Path") -> "pathlib.Path":
    d.mkdir(parents=True, exist_ok=True)
    shim = d / TOOL
    shim.write_text("", encoding="utf-8")
    shim.chmod(0o755)
    return shim


def test_a_bash_that_only_exists_in_cwd_is_not_the_first_candidate(
        tmp_path, monkeypatch) -> None:
    """The exploit shape: nothing legitimate is on PATH, only a cwd-planted
    shim resolves -- the fixture's own precondition proves raw
    `shutil.which()` would have found it before asserting the guard does not.
    """
    planted = _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + "/nonexistent-bin")

    import shutil
    found = shutil.which(TOOL)
    assert found is not None and os.path.abspath(found) == str(planted), (
        "fixture does not reproduce cwd-first resolution -- shutil.which() "
        "did not find the planted shim, so the assertion below tests nothing"
    )

    candidates = selftest.bash_candidates({})
    assert candidates[0] != str(planted) and candidates[0] != found, (
        "bash_candidates() put the cwd-only match first, so "
        "first_bash_that_runs_a_script() would spawn it directly (#2610)"
    )


def test_a_real_path_entry_still_resolves_first(tmp_path, monkeypatch) -> None:
    """Positive control: the guard must not blind the ordinary case."""
    real_dir = tmp_path / "realbin"
    real = _shim(real_dir)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PATH", str(real_dir))
    assert selftest.bash_candidates({})[0] == str(real)


def test_the_report_says_when_the_cwd_guard_is_unavailable(monkeypatch) -> None:
    """#2578 review: a silent fallback to raw `shutil.which()` in a file
    whose entire premise is 'do not let a security gate fail quietly'
    would be exactly the defect this diagnostic exists to surface, one
    layer down in its own resolution logic. Pin that the report states
    the degraded state rather than saying nothing about it.
    """
    monkeypatch.setattr(selftest, "_CWD_GUARD_AVAILABLE", False)
    root = str(_ROOT)
    lines, _code = selftest.report(root, environ={
        "SUPERTOOL_SELFTEST_BASH_CANDIDATES": ""})
    joined = os.linesep.join(lines)
    assert "cwd guard" in joined and "NOT available" in joined, joined


def test_the_report_says_the_cwd_guard_is_available_by_default() -> None:
    """The positive control for the line above."""
    lines, _code = selftest.report(str(_ROOT))
    joined = os.linesep.join(lines)
    assert "cwd guard" in joined and "available" in joined, joined
