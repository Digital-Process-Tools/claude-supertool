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

_PROBE: Optional[Tuple[bool, str]] = None


def _umask() -> int:
    """The process umask, read the only way CPython offers -- set and restore."""
    cur = os.umask(0o022)
    os.umask(cur)
    return cur


def _probe() -> Tuple[bool, str]:
    """Create a file at `0o600` and check the group and other bits came back clear.

    **The mode has to be one the platform cannot represent, and `0o666` is not
    one** (#1667). The first spelling of this probe created at `0o666` and
    compared against `0o666 & ~umask`; Windows has a umask of `0` and reports
    every writable file as `0o666`, so the value it wanted was the value the
    platform returns unconditionally. The probe passed there, `require_posix_
    modes()` skipped nothing, and four Windows legs went red on assertions the
    gate existed to hold off. A probe whose positive answer is indistinguishable
    from the platform's default answer has established nothing -- this repo's
    own defect class, inside the guard written to avoid it.

    `0o600` is the discriminating question: clearing the group and other bits
    is precisely what a filesystem without permission bits cannot do. A umask
    that clears an OWNER bit would also fail this and skip, which is the safe
    direction and is stated in the reason.
    """
    try:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "probe")
            fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
            os.close(fd)
            got = stat.S_IMODE(os.stat(p).st_mode)
    except (OSError, AttributeError, ValueError) as e:
        return False, "{0}: {1}".format(type(e).__name__, e)
    if got != 0o600:
        return False, (
            "a file created with mode 0o600 read back {0} -- this filesystem "
            "does not carry POSIX permission bits (a umask of {1} would also "
            "land here)".format(oct(got), oct(_umask())))
    return True, ""


def _support() -> Tuple[bool, str]:
    global _PROBE
    if _PROBE is None:
        _PROBE = _probe()
    return _PROBE


def require_posix_modes() -> None:
    """For an assertion about the mode ON DISK."""
    ok, why = _support()
    if not ok:
        pytest.skip(TOKEN + ": " + why)


def require_mode_disclosure() -> None:
    """For an assertion about the RECEIPT, which is a different question.

    `_created_mode_note` prints nothing when `os.name == "nt"`, and that is the
    product's own condition -- not "does this filesystem carry permission
    bits". The two coincide on every runner in CI and are still not the same
    claim: a POSIX host on an exFAT or SMB mount discloses the mode while the
    probe would decline, and a receipt test gated on the probe would skip there
    for a reason that is not its own. Mirrored rather than shared so that a
    change to the product's condition has exactly one place to be matched.
    """
    if os.name == "nt":
        pytest.skip(
            TOKEN + ": the receipt states no mode on Windows, where the "
            "executable bit does not exist -- see _created_mode_note")


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_the_probe_declines_a_filesystem_that_reports_0666_for_everything(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows shape, which the first spelling of this probe walked into.

    That spelling created with `0o666` and compared against `0o666 & ~umask`.
    On Windows the umask is `0`, so it wanted `0o666` — and a writable file
    there reads back exactly `0o666`. The probe's positive answer was the
    platform's default answer, so it reported POSIX permission bits available
    on the one platform that has none, and four Windows legs went red inside
    the guard built to keep them green.

    Simulated rather than platform-gated, and BOTH halves have to be simulated:
    a umask of 0 alone is harmless, and a filesystem reporting `0o666` alone is
    caught by any umask that clears a bit. It is the pair that makes the
    probe's wanted value equal to the platform's constant answer, which is why
    a macOS run at `umask 022` could not see this.
    """
    monkeypatch.setattr(os, "umask", lambda mask: 0)
    real_stat = os.stat

    def windows_like(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if os.path.basename(str(path)) == "probe":
            return os.stat_result(
                tuple([(st.st_mode & ~0o777) | 0o666] + list(st)[1:]))
        return st

    monkeypatch.setattr(os, "stat", windows_like)
    ok, why = _probe()
    assert not ok, (
        "the probe called POSIX permission bits available on a filesystem "
        "that reports 0o666 for every file")
    assert "0o666" in why, why


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
        # No probe: the receipt is compared against `os.stat` on the same file,
        # so the two agree whatever the filesystem carries.
        assert "mode {0:04o}".format(_mode(f)) in out, out


def test_a_rewrite_does_not_repeat_the_mode(tmp_path: Path) -> None:
    """The caller chose an existing file's mode; only a create is news."""
    f = tmp_path / "data.txt"
    f.write_text("old\n", encoding="utf-8")
    out = supertool.op_paste(str(f), "new\n")
    assert "ERROR" not in out, out
    assert "mode 0" not in out, out


def test_a_created_shebang_file_does_not_gain_an_executable_bit(
        tmp_path: Path) -> None:
    """On disk -- gated on the probe."""
    require_posix_modes()
    f = tmp_path / "deploy.sh"
    out = supertool.op_paste(str(f), "#!/usr/bin/env bash\nexit 0\n")
    assert "ERROR" not in out, out
    assert not (_mode(f) & 0o111), (
        "paste inferred the executable bit -- it must not guess: {0}".format(
            oct(_mode(f))))


def test_a_created_shebang_file_is_told_it_cannot_run(tmp_path: Path) -> None:
    """In the receipt -- gated on disclosure, which is the product's condition."""
    require_mode_disclosure()
    f = tmp_path / "deploy.sh"
    out = supertool.op_paste(str(f), "#!/usr/bin/env bash\nexit 0\n")
    assert "ERROR" not in out, out
    assert "chmod +x" in out and str(f) in out, (
        "a script that cannot run must say so in the receipt: {0!r}".format(out))


def test_a_created_file_without_a_shebang_gets_no_chmod_advice(
        tmp_path: Path) -> None:
    """Asserts an absence that is true on every platform, so it is not gated."""
    f = tmp_path / "notes.md"
    out = supertool.op_paste(str(f), "# notes\n")
    assert "ERROR" not in out, out
    assert "chmod +x" not in out, out


def test_append_creates_at_the_umask_default_and_says_so(tmp_path: Path) -> None:
    """`append` creates too, through the same chokepoint (reviewer, #1275).

    The widening applies to any op that brings a file into existence, and
    `append` is the other one. Left undisclosed it would be exactly the silent
    case this change exists to remove.
    """
    require_posix_modes()
    want = 0o666 & ~_umask()
    f = tmp_path / "log.sh"
    out = supertool.op_append(str(f), "#!/bin/sh\necho hi\n")
    assert "ERROR" not in out, out
    if want != 0o600:
        assert _mode(f) == want, (
            "created at {0}, expected {1}".format(oct(_mode(f)), oct(want)))


def test_append_discloses_the_created_mode_in_its_receipt(
        tmp_path: Path) -> None:
    """The receipt half of the line above, gated on the product's condition."""
    require_mode_disclosure()
    f = tmp_path / "log.sh"
    out = supertool.op_append(str(f), "#!/bin/sh\necho hi\n")
    assert "ERROR" not in out, out
    assert "mode {0:04o}".format(_mode(f)) in out, out
    assert "chmod +x" in out, out


def test_append_to_an_existing_file_does_not_state_a_mode(
        tmp_path: Path) -> None:
    f = tmp_path / "log.txt"
    f.write_text("one\n", encoding="utf-8")
    out = supertool.op_append(str(f), "two\n")
    assert "ERROR" not in out, out
    assert "mode 0" not in out, out


def test_a_changelog_fragment_exists() -> None:
    assert_change_is_findable(1275)
