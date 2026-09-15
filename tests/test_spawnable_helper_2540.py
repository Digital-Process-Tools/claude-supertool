"""`argv0()` resolves a bare name and leaves a path alone (#2540).

Two halves, and the second exists because the first broke CI. Resolving a
bare name through `shutil.which()` is the whole fix for the Windows `.cmd`
spawn. Sending an already-executable PATH through it is a regression: on
Python 3.12 `shutil.which` was rewritten to resolve an explicit path as
``os.path.join(os.path.dirname(cmd), os.path.basename(cmd))``, so a
forward-slash-normalised Windows path comes back with a native separator
spliced in before the filename --
``C:/Program Files/glab/glab.exe`` -> ``C:/Program Files/glab\\glab.exe``.

That undoes the normalisation #2176 and #2249 exist to get right. It cost
eight tests on the windows 3.12 leg of PR #2542 and passed on the other
eleven, because 3.9 through 3.11 return the argument verbatim. A test that
only ran the happy path would not have seen it on any platform I have.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "validators" / "common"))
from bin_resolve import resolve_bin_cmd  # noqa: E402
from spawnable import argv0  # noqa: E402


@pytest.fixture()
def executable(tmp_path) -> str:
    """A real executable file, addressed with forward slashes throughout."""
    d = tmp_path / "Program Files" / "toolkit"
    d.mkdir(parents=True)
    exe = d / ("tool.exe" if os.name == "nt" else "tool")
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    return exe.as_posix()


def test_an_executable_path_comes_back_byte_identical(executable) -> None:
    """The regression. Not `== Path(x)`, not `samefile` -- byte-identical.

    The 3.12 rewrite returns a path that still points at the same file, so
    every equality that normalises separators passes straight through the
    bug. The separator itself is the thing under test.
    """
    assert argv0(executable) == executable
    assert resolve_bin_cmd(executable, "tool") == [executable]


def test_a_bare_name_is_resolved_to_a_path(executable, monkeypatch) -> None:
    """The positive control, and the fix itself.

    Without this the test above is satisfied by an `argv0` that returns its
    argument unchanged for everything, which is the pre-#2540 behaviour and
    the bug.
    """
    monkeypatch.setenv("PATH", str(pathlib.Path(executable).parent))
    name = pathlib.Path(executable).name
    resolved = argv0(name)
    assert resolved != name, (
        "argv0 returned the bare name, so nothing was resolved and the "
        "Windows .cmd spawn is still broken (#2540)"
    )
    assert os.path.isfile(resolved), resolved


def test_an_absent_name_is_returned_unchanged() -> None:
    """The third arm. `argv0` never raises: the caller's own absent-tool
    gate and its `except FileNotFoundError` arm both stay reachable and keep
    reporting what they report today.
    """
    assert argv0("st-definitely-not-a-tool-2540") == \
        "st-definitely-not-a-tool-2540"
