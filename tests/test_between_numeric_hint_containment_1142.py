"""`between`'s line-range hint stats parts[1], a slot containment does not cover (#1142).

`_PATH_ARG_POSITIONS["between"]` is `(2, 4)` -- the path slot in each of the two
readings. `_between_numeric_hint` (#983) reads parts[1] as a filename and asks
`os.path.isfile` about it, from `_dispatch_impl`, after the gate has passed on a
slot this call did not use as a path.

Two different messages come back depending on the answer, so the pair is an
existence oracle for any path the process can stat:

    between:/etc/hosts:3:5   -> "does not take line ranges"  (it is a file)
    between:/etc/nope:3:5    -> "path not found: '5'"        (it is not)

Strictly weaker than #1135 -- existence only, no bytes -- so these tests assert
the two answers have become indistinguishable, not that content stayed unread.
The refusal has to name containment: `path not found: '5'` is what the absent
branch already says, and asserting merely "it errors" would pass with the hole
wide open.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import supertool

NL = chr(10)


@pytest.fixture
def outside(tmp_path: Path, monkeypatch) -> Path:
    """cwd is a box under tmp_path; a real file sits one level above it.

    conftest sets SUPERTOOL_ALLOW_OUTSIDE_CWD=1 so tmp_path fixtures work at
    all, so a containment test has to put the boundary back or it asserts
    nothing. Same reason applies to this repo's own `.supertool.json`, which
    opts out -- containment does not apply inside the checkout.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    present = tmp_path / "outside_a.txt"
    present.write_text("one" + NL + "two" + NL + "three" + NL, encoding="utf-8")
    box = tmp_path / "box"
    box.mkdir()
    monkeypatch.chdir(box)
    return present


def test_the_probed_slot_refuses_an_absolute_escape(outside: Path) -> None:
    out = supertool.dispatch("between:" + str(outside) + ":1:2")
    assert "does not take line ranges" not in out, (
        "the hint fired on a path outside cwd, which answers whether it "
        "exists:" + NL + out)
    assert "escapes cwd" in out, (
        "refusing has to name containment -- `path not found` is what the "
        "absent branch says and would pass with the oracle open:" + NL + out)


def test_the_probed_slot_refuses_a_relative_escape(outside: Path) -> None:
    out = supertool.dispatch("between:../outside_a.txt:1:2")
    assert "does not take line ranges" not in out, out
    assert "escapes cwd" in out, out


def test_the_single_line_form_refuses_too(outside: Path) -> None:
    """`between:PATH:LINE` is the other shape #983 redirects; same slot."""
    out = supertool.dispatch("between:../outside_a.txt:2")
    assert "does not take line ranges" not in out, out
    assert "escapes cwd" in out, out


def test_present_and_absent_outside_paths_are_indistinguishable(
    outside: Path,
) -> None:
    """The oracle itself. Both names are the same length, so normalising the
    one differing token makes the two answers comparable byte for byte."""
    present = supertool.dispatch("between:../outside_a.txt:1:2")
    absent = supertool.dispatch("between:../outside_b.txt:1:2")
    assert present.replace("outside_a", "X") == absent.replace("outside_b", "X"), (
        "the answer still depends on whether the outside file exists:"
        + NL + "present:" + NL + present + NL + "absent:" + NL + absent)


def test_a_contained_range_call_still_gets_the_redirect(
    tmp_path: Path, monkeypatch
) -> None:
    """The boundary. #983 exists to turn a mis-split into a usable redirect; a
    fix that killed the hint outright would trade this bug for that one."""
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    box = tmp_path / "box"
    box.mkdir()
    (box / "many.txt").write_text(
        NL.join("line" + str(i) for i in range(1, 21)) + NL, encoding="utf-8")
    monkeypatch.chdir(box)
    out = supertool.dispatch("between:many.txt:3:5")
    assert "does not take line ranges" in out, out
    assert "read:many.txt:3-5" in out, out


def test_a_symbol_that_looks_like_an_outside_path_still_resolves(
    tmp_path: Path, monkeypatch
) -> None:
    """The regression a table-widening fix would cause and this one must not.

    parts[1] is a SYMBOL in every other reading of `between`. Containing it
    unconditionally would refuse a legitimate symbol whose name happens to
    look like an absolute path outside the tree.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    box = tmp_path / "box"
    box.mkdir()
    (box / "code.py").write_text(
        "def f():" + NL + "    return 1" + NL, encoding="utf-8")
    monkeypatch.chdir(box)
    out = supertool.dispatch("between:/etc/passwd:code.py")
    assert "escapes cwd" not in out, (
        "a symbol is not a path -- containment must not filter what may be "
        "looked up:" + NL + out)
