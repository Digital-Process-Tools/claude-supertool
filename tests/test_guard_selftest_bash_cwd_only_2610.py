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

#: `bash_candidates()` always searches for the bare literal "bash" -- that
#: is the string under test, not a fixture choice. What filename actually
#: gets found by that search is platform-dependent, and this is the same
#: mismatch `tests/test_which_excludes_cwd_2575.py` documents and fixes by
#: naming its own probe `st-probe-2575.cmd`: CPython's `shutil.which()` on
#: Windows only tries `cmd + ext` for `ext` in PATHEXT when `cmd` does not
#: already end in a recognised extension -- for an extension-less search
#: term like "bash" it builds `["bash.COM", "bash.EXE", "bash.BAT",
#: "bash.CMD"]` and never tries the bare name at all. A shim literally
#: named "bash" (no extension) is therefore invisible to a real Windows
#: `shutil.which("bash")`, even though `which_excluding_cwd()`'s own loop
#: (which always tries `ext=""` first) would find it fine -- planting the
#: wrong filename would make this fixture's own precondition fail on
#: Windows without ever reaching the guard under test, exactly as it did
#: for #2575's first draft (see that file's own comment on the point).
SEARCH_TERM = "bash"
TOOL = SEARCH_TERM + ".cmd" if os.name == "nt" else SEARCH_TERM


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

    Comparisons below go through `os.path.normcase` (#2610 review, CI-caught:
    4/4 windows-latest legs, `bash.CMD` vs `bash.cmd`). `shutil.which()` and
    `which_excluding_cwd()` build the matched path by concatenating the
    search term with a `PATHEXT` entry -- `os.environ.get("PATHEXT")` or the
    default `".COM;.EXE;.BAT;.CMD"`, uppercase -- so the returned string
    reads `bash.CMD` even though the file the case-insensitive Windows
    filesystem actually matched is named `bash.cmd` on disk (`_shim()`
    writes the lowercase name `TOOL` builds). The two strings name the same
    file and differ only in case; a bare `==` does not know that.
    """
    planted = _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + "/nonexistent-bin")

    import shutil
    found = shutil.which(SEARCH_TERM)
    found_norm = os.path.normcase(os.path.abspath(found)) if found else found
    assert found is not None and found_norm == os.path.normcase(str(planted)), (
        "fixture does not reproduce cwd-first resolution -- shutil.which() "
        "did not find the planted shim, so the assertion below tests nothing"
    )

    candidates = selftest.bash_candidates({})
    first_norm = os.path.normcase(candidates[0]) if candidates[0] else candidates[0]
    same_as_planted = first_norm == os.path.normcase(str(planted))
    same_as_found = first_norm == os.path.normcase(found)
    assert not same_as_planted and not same_as_found, (
        "bash_candidates() put the cwd-only match first, so "
        "first_bash_that_runs_a_script() would spawn it directly (#2610)"
    )


def test_a_real_path_entry_still_resolves_first(tmp_path, monkeypatch) -> None:
    """Positive control: the guard must not blind the ordinary case.

    `os.path.normcase` on both sides for the same reason as the test above.
    """
    real_dir = tmp_path / "realbin"
    real = _shim(real_dir)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PATH", str(real_dir))
    first = selftest.bash_candidates({})[0]
    assert os.path.normcase(first) == os.path.normcase(str(real))


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
