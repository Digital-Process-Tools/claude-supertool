"""The retraction line must spell one file the same way twice (#1146).

`_run_with_validators` computes the rollback's target via `_write_target`,
which resolves a symlinked PATH through `os.path.realpath` -- and realpath
also canonicalises any symlinked ANCESTOR directory, not just the leaf link.
The success line the retraction quotes back (`_receipt_head`) names the file
the caller actually typed, via `os.path.abspath` elsewhere in this file --
which does NOT canonicalise ancestor symlinks. Where an ancestor directory of
the edited symlink is itself a symlink (macOS routes every `/tmp` path
through `/private/tmp`; this test builds the same shape without relying on
that), the two lines about the identical retracted write showed two
different absolute prefixes for what both are pointing at.
"""
from __future__ import annotations

import os
from pathlib import Path

from _symlink import require_symlink

import supertool

NL = chr(10)
Q3 = chr(39) * 3
BROKEN = "def f(:" + NL + "    pass" + NL


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(chr(92), chr(92) * 2) + chr(34)


def _paste(tmp_path: Path, target: Path, content: str) -> str:
    body = (
        "path = " + _toml_path(target) + NL
        + "content = " + Q3 + content + Q3 + NL
    )
    p = tmp_path / "p.toml"
    p.write_text(body, encoding="utf-8")
    return supertool.dispatch("paste:@" + str(p))


def test_retraction_subject_matches_the_quoted_success_lines_own_spelling(
    tmp_path: Path,
) -> None:
    """Build the exact shape macOS's /tmp -> /private/tmp gives for free:
    an ancestor directory that is itself a symlink, one level above the
    edited symlink. The retracted write's two lines about that same
    directory must agree.
    """
    require_symlink()
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    alias = tmp_path / "alias"
    os.symlink(str(real_dir), str(alias))
    link = alias / "link.py"
    os.symlink("target.py", str(link))

    out = _paste(tmp_path, link, BROKEN)
    assert "rolled back" in out, out

    # The two absolute directory spellings this receipt uses for the SAME
    # directory -- one via the caller's own path (`alias`), one via
    # `_write_target`'s realpath (`real_dir`) -- must not both appear.
    alias_dir = str(alias)
    real_dir_str = str(real_dir)
    assert not (alias_dir in out and real_dir_str in out), (
        "the retraction names the same directory two different ways:" + NL + out
    )
