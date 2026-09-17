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

TOOL = "st-probe-2575"


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
    import shutil
    assert shutil.which(TOOL) == str(planted), (
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


def test_argv0_falls_back_to_the_bare_name_on_a_cwd_only_match(
        tmp_path, monkeypatch) -> None:
    _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert argv0(TOOL) == TOOL


def test_bin_resolve_is_executable_rejects_a_cwd_only_match(
        tmp_path, monkeypatch) -> None:
    _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _is_executable(TOOL) is False


def test_bin_resolve_spawnable_falls_back_to_the_bare_name(
        tmp_path, monkeypatch) -> None:
    _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _spawnable(TOOL) == TOOL


def test_an_explicit_path_is_never_routed_through_the_guard(
        tmp_path, monkeypatch) -> None:
    """`name` already containing a directory carries no cwd risk -- it
    never touches PATH at all -- and must resolve exactly as before.
    """
    exe = _shim(tmp_path / "explicit")
    monkeypatch.chdir(tmp_path)
    assert which_excluding_cwd(str(exe)) == str(exe)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
