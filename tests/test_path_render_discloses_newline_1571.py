r"""#1571 - four renders collapse a path's newline to a space, which the
module they call declares a lie.

`presets/_untrusted.flat` (`disclose_newline=True`) exists specifically for a
field the reader has to be able to identify again on disk (#1557): the
default turns a newline into a space, which is right for a title and wrong
for a path, where the space produces a DIFFERENT, plausible, and possibly
existing name. `12c3a8f` set the flag for `git-worktrees`' own banner line;
these four render sites kept the title-shaped default.

Measured (issue's own reproduction): a file actually named `vict<LF>im.txt`
rendered as `vict im.txt` -- a name that is not on disk -- in the dispatch
header, in `op_edit`'s own success line, and in `_path_not_found`'s "tried"
line. `presets/git/worktrees.py`'s board did the same for `entry['path']` and
for the `wanted` PATH argument disclosed one line above it in the same
listing (two spellings of one path in one render).
"""
import os
from pathlib import Path

import pytest

import supertool

LF = chr(10)
DISCLOSED = chr(0x240A)


@pytest.mark.skipif(
    os.name == "nt",
    reason="a newline is not a legal NTFS filename character, so this test "
           "needs a POSIX filesystem to create its own fixture; the string-"
           "level tests below exercise the same render without touching disk "
           "and run on every platform",
)
def test_dispatch_header_discloses_a_newline_in_the_path(tmp_path: Path) -> None:
    target = tmp_path / ("vict" + LF + "im.txt")
    target.write_text("ALPHA" + LF, encoding="utf-8")
    out = supertool.dispatch(
        "edit:::ALPHA:::CHANGED:::" + str(target))
    assert LF not in out.split(chr(10), 1)[0], "a real newline still made a line"
    assert DISCLOSED in out, out
    assert "vict im.txt" not in out, (
        "the header renders a DIFFERENT file that happens to exist under a "
        "plausible name: " + out
    )


@pytest.mark.skipif(
    os.name == "nt",
    reason="a newline is not a legal NTFS filename character, so this test "
           "needs a POSIX filesystem to create its own fixture; the string-"
           "level tests below exercise the same render without touching disk "
           "and run on every platform",
)
def test_edit_success_line_discloses_a_newline_in_the_path(tmp_path: Path) -> None:
    target = tmp_path / ("vict" + LF + "im.txt")
    target.write_text("ALPHA" + LF, encoding="utf-8")
    out = supertool.dispatch(
        "edit:::ALPHA:::CHANGED:::" + str(target))
    assert "edited vict im.txt" not in out, out
    assert DISCLOSED in out, out


def test_path_not_found_discloses_a_newline_in_the_tried_path(
    tmp_path: Path,
) -> None:
    missing = str(tmp_path / ("a" + LF + "b.txt"))
    out = supertool._path_not_found(missing)
    assert "a b.txt" not in out, out
    assert DISCLOSED in out, out


def test_path_not_found_discloses_a_newline_in_the_exists_at_suggestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer finding (self-review, #1571): the `tried:` line was fixed and
    the `exists at ... cwd:...` suggestion two lines below it, in the SAME
    refusal, was not -- naming a different, collapsed spelling of the same
    file the line above had just disclosed correctly."""
    rel = "a" + LF + "b.txt"
    root = tmp_path / "root"
    root.mkdir()
    exists_path = os.path.join(str(root), rel)
    monkeypatch.setattr(supertool, "_project_root_above_cwd", lambda: str(root))
    monkeypatch.setattr(
        os.path, "exists", lambda p: p == exists_path,
    )
    out = supertool._path_not_found(rel)
    assert "a b.txt" not in out, out
    assert DISCLOSED in out, out
