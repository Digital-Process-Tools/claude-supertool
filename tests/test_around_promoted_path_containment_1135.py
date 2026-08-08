"""`around`'s colon route promotes parts[1] to a filename after the gate (#1135).

`_PATH_ARG_POSITIONS["around"]` is `(2,)` because in the normal reading parts[1]
is a PATTERN and parts[2] is the path. `_around_line_delegation` (#1086) re-reads
parts[1] as a filesystem path and hands it to `op_around_line`, and it runs from
`_dispatch_impl` *after* the containment gate has already passed on parts[2].

So the same file, named in two different slots of the same op, gets two different
answers: refused at parts[2], read at parts[1]. The receipt is honest about the
substitution it made -- what silently grew is the set of files the tool will open.

These tests drive a path through the PROMOTED slot. The v0.29.0 answer to the
same call was `ERROR: file not found: 3` -- an error, but for the wrong reason
(no delegation existed yet), so asserting "it errors" would pass on a build that
never had the hole. Every case here asserts the bytes stayed unread AND that the
refusal names containment.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import supertool

NL = chr(10)
MARKER = "TOPSECRET-1135"


@pytest.fixture
def outside(tmp_path: Path, monkeypatch) -> Path:
    """cwd is a box under tmp_path; the secret sits one level above it.

    The suite turns containment off globally (conftest sets
    SUPERTOOL_ALLOW_OUTSIDE_CWD=1 so tmp_path fixtures work at all), so a
    containment test has to put it back or it asserts nothing.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    secret = tmp_path / "outside.txt"
    secret.write_text(MARKER + NL + "second line" + NL, encoding="utf-8")
    box = tmp_path / "box"
    box.mkdir()
    monkeypatch.chdir(box)
    return secret


def test_the_promoted_slot_refuses_a_relative_escape(outside: Path) -> None:
    """The reported shape: the pattern slot holds the path, the path slot a line."""
    out = supertool.dispatch("around:../outside.txt:1")
    assert MARKER not in out, (
        "the delegation read a file outside cwd through the pattern slot -- "
        "the same file is refused when named in the path slot:" + NL + out)
    assert "escapes cwd" in out, (
        "refusing has to name containment; `file not found` is what v0.29.0 "
        "said for an unrelated reason and would pass with the hole open:"
        + NL + out)


def test_the_promoted_slot_refuses_an_absolute_escape(outside: Path) -> None:
    out = supertool.dispatch("around:" + str(outside) + ":1")
    assert MARKER not in out, out
    assert "escapes cwd" in out, out


def test_the_window_cannot_walk_the_file_via_line_and_n(outside: Path) -> None:
    """LINE is free and N is caller-chosen, so one refusal per line is not a fix."""
    out = supertool.dispatch("around:../outside.txt:2:50")
    assert MARKER not in out, out
    assert "second line" not in out, out
    assert "escapes cwd" in out, out


def test_a_contained_promotion_still_answers(tmp_path: Path, monkeypatch) -> None:
    """The boundary. #1086 exists to answer a call that would otherwise fail; a
    fix that disabled the delegation would trade this bug for that one."""
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    box = tmp_path / "box"
    box.mkdir()
    (box / "many.txt").write_bytes(
        (NL.join("line" + str(i) for i in range(1, 61)) + NL).encode())
    monkeypatch.chdir(box)
    out = supertool.dispatch("around:many.txt:30:4")
    assert "line30" in out, out
    assert "around_line" in out, out


def test_a_real_pattern_that_looks_like_an_outside_path_still_greps(
    tmp_path: Path, monkeypatch
) -> None:
    """The regression the table-based fix would cause and this one must not.

    `/etc/passwd` is a perfectly ordinary string to search a repo for. It is only
    a path when the delegation promotes it, which happens only when the path slot
    is a bare number naming nothing. Refusing it as a pattern would make
    containment a grep filter.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    box = tmp_path / "box"
    box.mkdir()
    (box / "code.py").write_text(
        "SHADOW = " + chr(34) + "/etc/passwd" + chr(34) + NL, encoding="utf-8")
    monkeypatch.chdir(box)
    out = supertool.dispatch("around:/etc/passwd:code.py:1")
    assert "SHADOW" in out, (
        "a pattern is not a path -- containment must not filter what may be "
        "searched for:" + NL + out)
    assert "escapes cwd" not in out, out
