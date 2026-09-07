"""#1370 — `_safe_path` expanded `$VAR` on the string it *checked*, while no
op ever expands `$` on the string it *opens*. A directory literally named
`$HOME` (or any `$name`) sitting INSIDE cwd was refused with a message
claiming it "escapes cwd" — false: the literal path never left cwd, only its
expanded interpretation would have (had it existed at all).

The twin of #1300 for `~`, but the opposite fix: #1300 made the *use* match
the *check* (both expand `~`). Here `$` is a legal filename character no op
ever expands on open, so the fix drops `expandvars` from the *check*
instead — one rule, applied uniformly, rather than a per-op special case
(which #1366's review specifically rejected for `glob`, on the grounds that a
second copy of `_containment_error` drifts — #882, #889).

`test_a_traversal_escape_is_still_refused` and
`test_a_symlink_escape_is_still_refused` are the positive controls: dropping
`expandvars` must not also weaken containment for a path that genuinely
escapes cwd by some other mechanism.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import supertool

from _symlink import require_symlink

MARK = "DOLLAR-1370-OK"


@pytest.fixture()
def boxed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    box = tmp_path / "box"
    box.mkdir()
    monkeypatch.chdir(box)
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    return box


def test_a_literal_dollar_named_dir_inside_cwd_is_not_refused(
        boxed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`$HOME/weird.txt` where `$HOME` is a literal subdirectory NAME inside
    cwd, not an env-var reference. `HOME` is pointed somewhere that does not
    even exist, so if the check still expanded `$HOME` it would resolve
    against that nonexistent target and refuse — proving the check, not the
    use, is what is under test."""
    monkeypatch.setenv("HOME", str(boxed.parent / "nonexistent-elsewhere"))
    literal_dir = boxed / "$HOME"
    literal_dir.mkdir()
    f = literal_dir / "weird.txt"
    f.write_text(MARK + chr(10), encoding="utf-8")

    out = supertool.dispatch("read:$HOME/weird.txt")
    assert "escapes cwd" not in out, out
    assert MARK in out, out


def test_glob_and_ls_agree_on_the_literal_dollar_name(
        boxed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(boxed.parent / "nonexistent-elsewhere"))
    literal_dir = boxed / "$HOME"
    literal_dir.mkdir()
    (literal_dir / "inside.txt").write_text(MARK, encoding="utf-8")

    out = supertool.dispatch("ls:$HOME")
    assert "escapes cwd" not in out, out
    assert "inside.txt" in out, out


def test_grep_reaches_a_literal_dollar_named_file(
        boxed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(boxed.parent / "nonexistent-elsewhere"))
    literal_dir = boxed / "$HOME"
    literal_dir.mkdir()
    f = literal_dir / "weird.txt"
    f.write_text(MARK + chr(10), encoding="utf-8")

    out = supertool.dispatch("grep:" + MARK + ":$HOME/weird.txt")
    assert "escapes cwd" not in out, out
    assert MARK in out, out


def test_a_traversal_escape_is_still_refused(boxed: Path) -> None:
    """Positive control: a path that genuinely escapes cwd via `..` must
    still be refused. Dropping `expandvars` must not weaken this."""
    outside = boxed.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(MARK + chr(10), encoding="utf-8")

    out = supertool.dispatch("read:../outside/secret.txt")
    assert "escapes cwd" in out, out
    assert MARK not in out, out


def test_a_symlink_escape_is_still_refused(
        boxed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: a symlink inside cwd pointing outside cwd must
    still be refused."""
    require_symlink()
    outside = boxed.parent / "outside2"
    outside.mkdir()
    real = outside / "secret2.txt"
    real.write_text(MARK + chr(10), encoding="utf-8")
    link = boxed / "link.txt"
    link.symlink_to(real)

    out = supertool.dispatch("read:link.txt")
    assert "escapes cwd" in out, out
    assert MARK not in out, out
