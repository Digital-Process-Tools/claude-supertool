"""#1059 - op_vim read with errors="replace", so an edit destroyed every
non-UTF-8 byte in the file, including bytes the edit never went near.

op_edit, op_replace and op_replace_lines already read with surrogateescape and
say in a comment why. op_vim did not, so a single `cc` on line 1 rewrote a stray
latin-1 byte on line 2 to U+FFFD, under a receipt that named one line and
reported no problem. The bytes are unrecoverable.

Every assertion is on **bytes**. Decoding first would compare two strings that
both hold U+FFFD and call the corruption equality.

op_vim also re-reads the file from disk inside `:norm`, and reads a *second*
file for `:r FILE`. Both were the same errors="replace", so fixing only the
entry read would have left two live paths to the same destruction.

The receipt is pinned too: a surrogateescape read puts lone surrogates in the
context and diff blocks the receipt echoes, and those cannot be encoded to a
UTF-8 stream. Ending byte destruction by trading it for a UnicodeEncodeError
after a successful write is not a fix.
"""

from __future__ import annotations

from pathlib import Path

import _supertool


LATIN1 = b"alpha\n caf\xe9 latin1\nbeta\n"


def _latin1(tmp_path: Path, name: str = "v.txt") -> Path:
    f = tmp_path / name
    f.write_bytes(LATIN1)
    return f


def test_vim_leaves_non_utf8_bytes_it_never_touched_alone(tmp_path: Path) -> None:
    f = _latin1(tmp_path)
    out = _supertool.op_vim(str(f), "1G\nccHELLO")
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"HELLO\n caf\xe9 latin1\nbeta\n"


def test_receipt_of_a_surrogateescape_read_can_reach_the_caller(
    tmp_path: Path, monkeypatch
) -> None:
    """The receipt echoes context and a diff, both built from the buffer. With
    surrogateescape that buffer holds lone surrogates, and no UTF-8 stream can
    encode one - so the op wrote the right bytes and then died on the way to
    saying so. Trading destruction for a traceback over a file that DID change
    is not a fix. Sanitised at dispatch, the one frame every consumer shares.

    Display only: the assertion on disk is the same bytes as before."""
    monkeypatch.chdir(tmp_path)
    f = _latin1(tmp_path)
    out = _supertool.dispatch("vim:::v.txt:::1G\nccHELLO")
    out.encode("utf-8")
    assert "ERROR" not in out, out
    assert f.read_bytes() == b"HELLO\n caf\xe9 latin1\nbeta\n"


def test_the_same_seam_covers_op_edit(tmp_path: Path, monkeypatch) -> None:
    """op_edit / op_replace / op_replace_lines have read with surrogateescape
    since #1049 and echo their buffer too, so they carried the same crash."""
    monkeypatch.chdir(tmp_path)
    f = _latin1(tmp_path, "e.txt")
    out = _supertool.dispatch("edit:::alpha:::ALPHA:::e.txt")
    out.encode("utf-8")
    assert f.read_bytes() == b"ALPHA\n caf\xe9 latin1\nbeta\n"


def test_vim_r_file_does_not_destroy_the_source_files_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    """`:r FILE` reads a *second* file and splices it into the buffer that gets
    written. Its own read mode decides whether those bytes survive.

    chdir + a relative name on purpose: the vim tokenizer pre-normalises `\\e`
    inside a script, and an absolute Windows path is where a stray `\\e` segment
    actually shows up (#501). The test is about the read mode, not about how
    this platform spells a path."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src.txt").write_bytes(b"na\xefve\n")
    f = tmp_path / "t.txt"
    f.write_bytes(b"one\ntwo\n")
    out = _supertool.op_vim("t.txt", "1G\n:r src.txt")
    assert not out.startswith("ERROR"), out
    assert b"na\xefve" in f.read_bytes(), f.read_bytes()


def test_vim_norm_reread_does_not_destroy_untouched_bytes(tmp_path: Path) -> None:
    """`:norm` persists, recurses, then re-reads the file from disk twice. Each
    re-read is a chance to mangle bytes the actions never addressed."""
    f = tmp_path / "n.txt"
    f.write_bytes(b"a\xe9a\nbbb\n")
    out = _supertool.op_vim(str(f), ":1,2norm A!")
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"a\xe9a!\nbbb!\n"


def test_vim_on_a_clean_utf8_file_is_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "u.txt"
    f.write_bytes(b"alpha\nbeta\n")
    out = _supertool.op_vim(str(f), "1G\nccHELLO")
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"HELLO\nbeta\n"
