r"""#1271 -- a Windows drive letter that sits after a SPACE (rather than
after ',' or '|') never rejoins in `_split_arg`, so an absolute Windows path
in a whitespace-separated position mis-tokenizes: `grep:pattern:file.py
C:\Users\x.py` reads as pattern=`'pattern:file.py C'` + path=`'\Users\x.py'`
instead of pattern=`'pattern:file.py'` + path=`'C:\Users\x.py'`.

The issue's own argument against widening `_split_arg` itself: doing so would
change tokenization for a call that works today (a pattern containing a
space followed by an absolute path currently parses as pattern-with-space
plus path, and would silently become one token on POSIX). The fix instead
lives at the point a path has already failed to resolve -- `_colon_split_hint`,
via the new `_drive_letter_swap_suggest` -- which cannot break a call that
currently succeeds, because it only ever runs after a failure.

`_drive_qualified_target` below produces a REAL, existing, drive-qualified
path string identically on every CI platform, but not by the same technique
on each: on Windows it is `tmp_path`'s own genuine absolute path (backslash
truly is the separator there); on POSIX it is a literal FILENAME that merely
contains the same shape a Windows path has (backslash is an ordinary
filename character on POSIX, so a file literally named "C:\something.py" is
indistinguishable, to os.path.exists, from what a genuine Windows absolute
path resolves to). These cannot be unified into one technique: `pathlib`'s
own `/` operator treats a drive-qualified RIGHT operand as replacing the left
one outright --

    >>> import pathlib
    >>> pathlib.PureWindowsPath("/tmp/foo") / "C:\\something.py"
    PureWindowsPath('C:/something.py')

-- so naively writing a literal "C:\something.py" FILENAME under `tmp_path`
on real Windows silently targets the real drive root instead of a file
scoped to the test's own sandbox.

Every "must fire" case is paired with a "must not fire" one in the same
fixture, per the same-fixture rule for a silence assertion (CLAUDE.md, "A
negative assertion needs a positive control").
"""

from __future__ import annotations

import sys
from pathlib import Path

import supertool
import _supertool as core

_ON_WINDOWS = sys.platform.startswith("win")


def _drive_qualified_target(tmp_path: Path, body: str = "x\n") -> str:
    """A real, EXISTING path shaped `LETTER:...` -- see the module docstring
    for why the two platforms need different techniques to get there."""
    if _ON_WINDOWS:
        target = tmp_path / "something.py"
        target.write_text(body, encoding="utf-8")
        return str(target)
    literal = "C:\\something.py"
    (tmp_path / literal).write_text(body, encoding="utf-8")
    return literal


def test_drive_letter_after_space_is_recognised_and_rejoined(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    candidate = _drive_qualified_target(tmp_path)
    assert candidate[1] == ":"
    out = supertool.dispatch(f"grep:pattern:file.py {candidate}")
    assert "Windows absolute path" in out, out
    assert candidate in out, out
    assert f"grep:pattern:file.py:{candidate}" in out, out


def test_drive_letter_after_space_must_not_fire_when_candidate_absent(
    tmp_path: Path, monkeypatch
) -> None:
    """Must-not-fire half: no file exists at the rejoined candidate, so this
    is an ordinary colon-absorbed-pattern failure and the drive-letter
    diagnosis must stay silent (falling through to the existing message).
    Never writes anything -- a nonexistent-path check is safe identically on
    every platform, so no `_drive_qualified_target` call is needed here."""
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch(r"grep:pattern:file.py C:\something-else.py")
    assert "Windows absolute path" not in out, out
    assert "split on ':'" in out, out


def test_drive_letter_swap_suggest_unit_positive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    candidate = _drive_qualified_target(tmp_path)
    letter, remainder = candidate[0], candidate[2:]
    out = core._drive_letter_swap_suggest("read", f"leading {letter}", remainder)
    assert out
    assert "leading" in out
    assert candidate in out
    assert out.endswith(f"read:leading:{candidate}")


def test_drive_letter_swap_suggest_unit_no_space_before_letter() -> None:
    """A letter NOT preceded by whitespace (e.g. an identifier ending in a
    letter that happens to be a valid drive letter) must not trigger this --
    only the whitespace-separated shape the issue describes is in scope."""
    out = core._drive_letter_swap_suggest("read", "leadingC", "/x.py")
    assert out == ""


def test_drive_letter_swap_suggest_unit_candidate_must_exist(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    out = core._drive_letter_swap_suggest("read", "leading C", r"\does-not-exist.py")
    assert out == ""


def test_drive_letter_after_space_still_fires_when_the_naive_path_also_exists(
    tmp_path: Path, monkeypatch
) -> None:
    """CI (windows-latest, all four interpreters) failure this pins: on a
    real Windows machine, dropping the drive letter from an absolute path
    leaves a bare "\\coincidence.py" shape, which Windows resolves against
    the CURRENT drive rather than failing -- so whenever `cwd` shares a
    drive with the intended target (a single-drive machine, i.e. virtually
    every real Windows install and every windows-latest runner), the naive,
    mis-tokenized path coincidentally EXISTS too. The old
    "os.path.exists(path): return \"\"" early-out in `_colon_split_hint`
    swallowed the diagnosis right there, before it ever reached
    `_drive_letter_swap_suggest`.

    Reproduced here via `os.name`, not a real Windows box: the new branch is
    gated on `os.name == "nt"`, and POSIX's own `os.path.splitdrive` always
    reports "no drive" regardless of content, so mocking just that one
    attribute reaches the exact branch real Windows takes -- the two literal
    filenames below stand in for "the naive path exists" and "the corrected
    one exists too", the same technique `_drive_qualified_target` already
    uses for the positive-control test above."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(core.os, "name", "nt")
    naive = r"\coincidence.py"
    (tmp_path / naive).write_text("naive\n", encoding="utf-8")
    candidate = r"C:\coincidence.py"
    (tmp_path / candidate).write_text("real\n", encoding="utf-8")

    out = core._colon_split_hint("grep", "pattern:file.py C", naive)
    assert "Windows absolute path" in out, out
    assert candidate in out, out


# --- regression: the WORKING (non-whitespace) drive-letter cases in
# `_split_arg` itself must parse exactly as before this change (#1271's own
# "must not fire" pairing, at the tokenizer level rather than the message
# level, since the fix deliberately never touches `_split_arg`). These are
# pure string-splitting assertions with no filesystem interaction, so they
# need no platform branching at all. ----------------------------------------

def test_split_arg_drive_letter_still_rejoins_without_whitespace() -> None:
    assert core._split_arg(r"read:C:\Users\file.py") == [
        "read", r"C:\Users\file.py",
    ]


def test_split_arg_drive_letter_still_rejoins_in_comma_list() -> None:
    assert core._split_arg(r"validate:C:\a.php,C:\b.php") == [
        "validate", r"C:\a.php,C:\b.php",
    ]


def test_split_arg_drive_letter_still_rejoins_after_pipe() -> None:
    assert core._split_arg(r"op:T|C:\a.php") == [
        "op", r"T|C:\a.php",
    ]


def test_split_arg_drive_letter_after_space_still_does_not_rejoin_in_tokenizer() -> None:
    """Pins the tokenizer's OWN behaviour as unchanged by this fix: the
    rejoin still never spans whitespace, which is the property the issue
    asked to be preserved rather than "fixed" in `_split_arg`."""
    assert core._split_arg(r"grep:pattern:file.py C:\x.py") == [
        "grep", "pattern", "file.py C", r"\x.py",
    ]
