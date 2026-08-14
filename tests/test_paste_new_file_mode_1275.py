"""#1275 -- a file `paste` brings into existence, and what its mode is.

`_atomic_write` writes through `tempfile.mkstemp`, which creates at `0600`, and
re-applies the *original* file's mode before the rename (#259). An overwrite is
therefore correct. A **create** has no original mode, so mkstemp's `0600` was
what reached disk -- not a chosen default, an artefact of the temp file, and one
no other file-creating tool has: `>`, `tee`, `cp` and every editor create at
`0666 & ~umask`.

Two claims are pinned here and a third is deliberately not:

* the mode on disk after a create is the umask default, not `0600`;
* the receipt states it, because a mode nobody is told about is a fact the
  reader has no reason to check -- and on Windows it states nothing, since the
  bit does not exist there and a mode line would be a lie either way.

**Not pinned: any inference of the executable bit.** `paste` does not read the
shebang, the neighbours' modes or an overwritten file's old exec bit to decide
to add `+x`; it says the file starts with `#!` and is not executable, and leaves
the `chmod` to the caller. See the changelog entry for why.

The mode assertions are gated on a **probe**, not on a platform name: a file is
created with an explicit `0666` and read back. Where permission bits are not
carried -- Windows, and any filesystem mounted without them -- the skip carries
TOKEN so `N skipped` can be resolved to one stated reason.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Optional, Tuple

import pytest

import supertool
from _changelog_findable import assert_change_is_findable

#: Grep handle. Every skip this module produces carries it.
TOKEN = "posix-file-modes(#1275)"

_PROBE = None  # type: Optional[Tuple[bool, str]]


def _umask() -> int:
    """The process umask, read the only way CPython offers -- set and restore."""
    cur = os.umask(0o022)
    os.umask(cur)
    return cur


def _probe() -> Tuple[bool, str]:
    """Create a file with an explicit mode and check what came back."""
    try:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "probe")
            fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o666)
            os.close(fd)
            got = stat.S_IMODE(os.stat(p).st_mode)
    except (OSError, AttributeError, ValueError) as e:
        return False, "{0}: {1}".format(type(e).__name__, e)
    want = 0o666 & ~_umask()
    if got != want:
        return False, (
            "a file created with mode 0o666 read back {0}, not {1} -- this "
            "filesystem does not carry POSIX permission bits".format(
                oct(got), oct(want)))
    return True, ""


def _support() -> Tuple[bool, str]:
    global _PROBE
    if _PROBE is None:
        _PROBE = _probe()
    return _PROBE


def require_posix_modes() -> None:
    ok, why = _support()
    if not ok:
        pytest.skip(TOKEN + ": " + why)


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_a_created_file_lands_at_the_umask_default(tmp_path: Path) -> None:
    require_posix_modes()
    want = 0o666 & ~_umask()
    if want == 0o600:
        pytest.skip(
            TOKEN + ": umask {0} makes the default 0600, so this assertion "
            "cannot tell the fix from the bug".format(oct(_umask())))
    f = tmp_path / "fresh.sh"
    out = supertool.op_paste(str(f), "#!/bin/sh\necho hi\n")
    assert "ERROR" not in out, out
    assert _mode(f) == want, (
        "created at {0}, expected the umask default {1} -- mkstemp's 0600 "
        "reached disk".format(oct(_mode(f)), oct(want)))


def test_a_rewrite_still_keeps_the_file_own_mode(tmp_path: Path) -> None:
    """#259's guarantee, restated here because #1275 changes the sibling arm."""
    require_posix_modes()
    f = tmp_path / "hook.sh"
    f.write_text("old\n", encoding="utf-8")
    os.chmod(f, 0o700)
    out = supertool.op_paste(str(f), "new\n")
    assert "ERROR" not in out, out
    assert _mode(f) == 0o700, (
        "the umask default was applied to an overwrite: {0}".format(
            oct(_mode(f))))


def test_the_receipt_states_the_mode_on_a_create_and_only_on_posix(
        tmp_path: Path) -> None:
    """True on both platforms: the line is present exactly where it is honest."""
    f = tmp_path / "data.txt"
    out = supertool.op_paste(str(f), "hello\n")
    assert "ERROR" not in out, out
    stated = "mode 0" in out
    assert stated == (os.name != "nt"), (
        "receipt {0!r} states a mode on {1}".format(out, os.name))
    if stated:
        require_posix_modes()
        assert "mode {0:04o}".format(_mode(f)) in out, out


def test_a_rewrite_does_not_repeat_the_mode(tmp_path: Path) -> None:
    """The caller chose an existing file's mode; only a create is news."""
    f = tmp_path / "data.txt"
    f.write_text("old\n", encoding="utf-8")
    out = supertool.op_paste(str(f), "new\n")
    assert "ERROR" not in out, out
    assert "mode 0" not in out, out


def test_a_created_shebang_file_is_told_it_cannot_run(tmp_path: Path) -> None:
    require_posix_modes()
    f = tmp_path / "deploy.sh"
    out = supertool.op_paste(str(f), "#!/usr/bin/env bash\nexit 0\n")
    assert "ERROR" not in out, out
    assert not (_mode(f) & 0o111), (
        "paste inferred the executable bit -- it must not guess: {0}".format(
            oct(_mode(f))))
    assert "chmod +x" in out and str(f) in out, (
        "a script that cannot run must say so in the receipt: {0!r}".format(out))


def test_a_created_file_without_a_shebang_gets_no_chmod_advice(
        tmp_path: Path) -> None:
    require_posix_modes()
    f = tmp_path / "notes.md"
    out = supertool.op_paste(str(f), "# notes\n")
    assert "ERROR" not in out, out
    assert "chmod +x" not in out, out


def test_a_changelog_fragment_exists() -> None:
    assert_change_is_findable(1275)
