"""A resolution that only exists via the current directory is refused (#2575).

`validators/common/spawnable.py` and `bin_resolve.py` both hand a bare tool
name to `shutil.which()` and spawn whatever it returns. On Windows,
`shutil.which()` inserts `os.curdir` at the front of its search path unless
`NoDefaultCurrentDirectoryInExePath` is set -- and it does this even when a
caller passes an explicit `path=` argument, because the insertion happens
*after* the `path is None` branch in CPython's own source, not inside it.

`_supertool.py`'s validator/formatter spawn sites (`_validator_resolve`,
`_validator_run_one`, `_formatter_run_one`) pass no `cwd=`, so the adapter
subprocess inherits supertool's own process cwd -- the repository under
inspection. A repository shipping `ruff.cmd`, `markdownlint.cmd` or
`eslint.cmd` at its own root therefore has that file resolved ahead of every
real PATH entry and executed, on Windows, the moment the matching validator
or formatter runs against it (#2540 turned this from a dead end -- the old
code spawned the bare name, which `CreateProcess` cannot find as a `.cmd` --
into live execution, by spawning `which()`'s own resolved path instead).

These tests ask the mechanism directly rather than the platform: they
monkeypatch `PATH` to place a shim at a directory equal to `os.curdir`, in
exactly the position CPython's own curdir-insertion would put one (position
0), and check whether the resolved form ever comes back pointing inside the
current directory. That is testable on every platform this suite runs on --
the vulnerable *mechanism* (`shutil.which()` inserting curdir at position 0)
is Windows-only, but the *guard* being tested here (never trust a match that
lands in cwd) has nothing platform-specific in it, so it is asked directly
instead of behind a platform skip.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "validators" / "common"))
from bin_resolve import _is_executable, _spawnable  # noqa: E402
from spawnable import argv0, spawnable, which_excluding_cwd  # noqa: E402

# A name ending in a PATHEXT extension, not a bare name (#2577 review of
# #2575's own tests). CPython's shutil.which() on Windows does NOT try the
# bare name once it decides the name has no recognised extension -- for a
# name that does not already end in one of PATHEXT's suffixes it builds
# `files = [cmd + ext for ext in pathext]` and never includes bare `cmd` in
# that list at all (see cpython/Lib/shutil.py, the `if any(cmd.lower()...)`
# branch). A shim planted as bare "st-probe-2575" is therefore invisible to
# the *real* shutil.which() on a genuine Windows host -- the very call the
# fixture's own precondition below uses to prove it planted a reachable
# shim -- even though `which_excluding_cwd()` under test tries the bare
# name itself (its own `exts = [""] + [...]` includes "" first) and would
# have found it fine. The mismatch was reasoned on macOS, not observed on
# Windows, and CI caught it: all three Windows legs failed the fixture's
# own self-check, never reaching the guard under test at all.
#
# ".cmd" is one of the extensions `shutil.which()` matches unconditionally
# via `cmd.lower().endswith(ext.lower())` against the default PATHEXT
# (".COM;.EXE;.BAT;.CMD"), so a name that already ends in it takes the
# "already has an extension" branch and `files = [cmd]` -- the literal
# name, unmodified -- which is exactly the shape the module's own docstring
# names as the real-world attack (a repo-planted "ruff.cmd"). On POSIX,
# `shutil.which()` has no PATHEXT logic at all and treats the whole string
# as a literal filename either way, so this is not a Windows-only special
# case in the fixture -- the same TOOL name is correct on every platform
# this suite runs on.
TOOL = "st-probe-2575.cmd"


def _shim(d: "pathlib.Path", name: str = TOOL) -> "pathlib.Path":
    d.mkdir(parents=True, exist_ok=True)
    shim = d / name
    shim.write_text("", encoding="utf-8")
    shim.chmod(0o755)
    return shim


def test_a_real_path_entry_still_resolves(tmp_path, monkeypatch) -> None:
    """Positive control: the guard must not blind the ordinary case."""
    real = _shim(tmp_path / "realbin")
    monkeypatch.setenv("PATH", str(tmp_path / "realbin"))
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    assert which_excluding_cwd(TOOL) == str(real)


def test_a_match_that_only_exists_in_cwd_is_refused(tmp_path, monkeypatch) -> None:
    """The exploit shape: PATH puts the current directory first, as
    CPython's own curdir-insertion would, and only a cwd-planted shim
    matches -- nothing legitimate is on PATH at all.
    """
    planted = _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + "/nonexistent-bin")
    # The fixture must actually simulate the vulnerable condition before the
    # guard is credited with anything: raw shutil.which() has to find it.
    # On Windows, CPython's shutil.which() inserts os.curdir literally as
    # the string "." into the search list ahead of the explicit PATH entry
    # (the insertion happens unconditionally, after the `path is None`
    # check, not gated on whether cwd already appears in PATH) and joins
    # the candidate name onto THAT entry, so the hit it returns is the
    # relative path ".\\st-probe-2575.cmd", not the absolute planted path
    # -- comparing against `str(planted)` directly fails even though the
    # shim genuinely was found via cwd. Normalise both sides through
    # abspath() before comparing so the assertion checks *that a match
    # happened*, not which of the two equivalent spellings of it came back.
    #
    # This was observed directly on a Windows CI leg (job 105118201619,
    # pytest 3.10 and 3.12), not reasoned from source: it is sharper
    # evidence than #2575's own issue text, which reasoned about the
    # curdir-insertion from reading CPython's shutil.py but never ran it on
    # a real Windows host. The relative form matters beyond this fixture --
    # it is what a caller-level guard sees back, and a relative path
    # resolves against cwd *again* at spawn time, so a naive `is None` check
    # is not the only thing a fix would need to get right.
    import shutil
    found = shutil.which(TOOL)
    assert found is not None and os.path.abspath(found) == str(planted), (
        "fixture does not reproduce cwd-first resolution -- shutil.which() "
        "did not find the planted shim, so the assertions below are not "
        "testing the #2575 shape at all"
    )
    assert which_excluding_cwd(TOOL) is None


def test_a_cwd_match_never_shadows_a_real_one_further_down_path(
        tmp_path, monkeypatch) -> None:
    """The case that matters most: a repo-planted shim must not mask a
    tool the user genuinely has installed elsewhere on PATH.
    """
    _shim(tmp_path)  # the attacker's plant, at cwd
    real = _shim(tmp_path / "realbin")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + str(tmp_path / "realbin"))
    assert which_excluding_cwd(TOOL) == str(real)


def test_spawnable_rejects_a_cwd_only_match(tmp_path, monkeypatch) -> None:
    _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert spawnable(TOOL) is None


def test_argv0_refuses_a_cwd_only_match_rather_than_the_bare_name(
        tmp_path, monkeypatch) -> None:
    """#2578: the bare name is no longer returned here. On Windows,
    `CreateProcess` would perform the exact cwd search this module refuses,
    reintroducing #2575 through the OS's own resolution of the bare name.
    """
    _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    resolved = argv0(TOOL)
    assert resolved != TOOL
    assert os.path.dirname(resolved), (
        "a refused result must carry a directory component -- a bare name "
        "is exactly what a Windows CreateProcess search resolves via cwd"
    )
    assert resolved.endswith(TOOL), (
        "the tool name is expected at the end of the refused path"
    )
    assert not os.path.exists(resolved), (
        "a refused result must not point at anything real -- if it did, "
        "subprocess would run it instead of failing"
    )


def test_bin_resolve_is_executable_rejects_a_cwd_only_match(
        tmp_path, monkeypatch) -> None:
    _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _is_executable(TOOL) is False


def test_bin_resolve_spawnable_refuses_a_cwd_only_match(
        tmp_path, monkeypatch) -> None:
    """#2578: same shift as `argv0` above, applied to the second chokepoint."""
    _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    resolved = _spawnable(TOOL)
    assert resolved != TOOL
    assert os.path.dirname(resolved)


def test_an_explicit_path_is_never_routed_through_the_guard(
        tmp_path, monkeypatch) -> None:
    """An **absolute** `name` (as tested here, `tmp_path` is always
    absolute) carries no cwd risk -- it never touches PATH at all -- and
    must resolve exactly as before. A relative directory component is a
    different, deliberately-allowed case not covered by this fixture
    (#2613).
    """
    exe = _shim(tmp_path / "explicit")
    monkeypatch.chdir(tmp_path)
    assert which_excluding_cwd(str(exe)) == str(exe)


def test_a_differently_cased_path_entry_is_still_recognised_as_cwd(
        tmp_path, monkeypatch) -> None:
    """#2581: `spawnable.py:91` compared a PATH entry to `here` with a raw
    `==`, while the dedupe two lines above it (`:87`) folds case first with
    `os.path.normcase`. On a case-insensitive filesystem (Windows, and
    macOS by default) a PATH entry spelling the repository directory in a
    different case is therefore not recognised as `here` and its match is
    returned rather than excluded -- the same "file supplied by the
    repository is run as a program" shape #2575 closed for the exact-case
    spelling only.

    Isolated from whether *this* host's filesystem/`os.path.normcase` fold
    case at all by monkeypatching `os.path.normcase` to `str.lower` (the
    Windows behaviour, POSIX's own `os.path.normcase` is the identity
    function) and `os.path.isfile`/`os.access` to always answer True for
    the candidate: what is under test is the string comparison at `:91`,
    not this runner's platform.
    """
    monkeypatch.chdir(tmp_path)
    cased = str(tmp_path).upper()
    if cased == str(tmp_path):
        cased = str(tmp_path).lower()
    monkeypatch.setenv("PATH", cased)
    monkeypatch.setattr(os.path, "normcase", str.lower)
    monkeypatch.setattr(os.path, "isfile", lambda p: True)
    monkeypatch.setattr(os, "access", lambda p, m: True)
    assert which_excluding_cwd(TOOL) is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
