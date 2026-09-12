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

Every reproduction here uses a literal backslash-containing FILENAME (not a
nested directory) so it is exercised identically on every CI platform: POSIX
treats a backslash as an ordinary filename character, so a file literally
named "C:\something.py" in cwd is indistinguishable, to os.path.exists, from
what a genuine Windows absolute path resolves to on that platform. This is a
positive control on every platform, not a Windows-only test -- the mechanism
under test (string splitting and rejoining) has no OS dependency; only the
real-world *filesystem* interpretation of a backslash does, and this fixture
sidesteps that by making the candidate string itself the filename.

Every "must fire" case is paired with a "must not fire" one in the same
fixture, per the same-fixture rule for a silence assertion (CLAUDE.md, "A
negative assertion needs a positive control").
"""

from __future__ import annotations

from pathlib import Path

import supertool
import _supertool as core


def _write_literal(tmp_path: Path, literal_name: str, body: str = "x\n") -> None:
    (tmp_path / literal_name).write_text(body, encoding="utf-8")


def test_drive_letter_after_space_is_recognised_and_rejoined(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_literal(tmp_path, r"C:\something.py")
    out = supertool.dispatch(r"grep:pattern:file.py C:\something.py")
    assert "Windows absolute path" in out, out
    assert r"C:\something.py" in out, out
    assert r"grep:pattern:file.py:C:\something.py" in out, out


def test_drive_letter_after_space_must_not_fire_when_candidate_absent(
    tmp_path: Path, monkeypatch
) -> None:
    """Must-not-fire half: no file exists at the rejoined candidate, so this
    is an ordinary colon-absorbed-pattern failure and the drive-letter
    diagnosis must stay silent (falling through to the existing message)."""
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch(r"grep:pattern:file.py C:\something.py")
    assert "Windows absolute path" not in out, out
    assert "split on ':'" in out, out


def test_drive_letter_swap_suggest_unit_positive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_literal(tmp_path, r"C:\x.py")
    out = core._drive_letter_swap_suggest("read", "leading C", r"\x.py")
    assert out
    assert "leading" in out
    assert r"C:\x.py" in out
    assert out.endswith(r"read:leading:C:\x.py")


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


# --- regression: the WORKING (non-whitespace) drive-letter cases in
# `_split_arg` itself must parse exactly as before this change (#1271's own
# "must not fire" pairing, at the tokenizer level rather than the message
# level, since the fix deliberately never touches `_split_arg`). ------------

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
