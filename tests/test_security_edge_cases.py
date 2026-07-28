"""Path escape + corrupted file edge cases for read/edit.

Audit pass 2026-05-23: probe behaviors that could cause data loss, crashes,
or DoS in the most common ops. Each test either documents current behavior
or pins a fix.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Path escape
# ---------------------------------------------------------------------------

def test_read_path_with_null_byte_errors_cleanly(tmp_path: Path) -> None:
    """A NUL in the path must not crash the process — return a clean ERROR."""
    bad = f"{tmp_path}/foo\x00.txt"
    out = supertool.op_read(bad)
    assert "ERROR" in out
    # No traceback / no Python exception leak
    assert "Traceback" not in out


def test_read_path_with_newline_does_not_break_meta_line(tmp_path: Path) -> None:
    """A newline in path shouldn't fracture the meta-header line."""
    weird = tmp_path / "line1\nline2.txt"
    try:
        weird.write_text("body\n")
    except OSError:
        pytest.skip("filesystem rejects newline in filename")
    out = supertool.op_read(str(weird))
    # First line should still be the single meta header
    first = out.splitlines()[0]
    assert first.startswith("(")
    assert "lines, " in first


def test_read_traversal_outside_cwd_works_but_should_be_visible(tmp_path: Path, monkeypatch) -> None:
    """Reading via ../ traversal currently succeeds — document this."""
    sub = tmp_path / "sub"
    sub.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("classified\n")
    monkeypatch.chdir(sub)
    out = supertool.op_read("../secret.txt")
    # Current behavior: works (no chroot). Pin so we notice if it changes.
    assert "classified" in out


def test_edit_on_symlink_clobbers_link_with_regular_file(tmp_path: Path) -> None:
    """CRITICAL: edit/replace via atomic-write may turn a symlink into a regular file.

    If true, an edit through a symlink loses the link and the real file is left
    untouched — silent data divergence. Document the current behavior; if it
    follows symlinks (writes through to target), assert that instead.
    """
    real = tmp_path / "real.txt"
    real.write_text("hello\n")
    link = tmp_path / "via.txt"
    link.symlink_to(real)

    out = supertool.op_edit("hello", "world", str(link))
    assert "ERROR" not in out

    real_after = real.read_text(encoding="utf-8")
    link_is_symlink = link.is_symlink()
    link_content = link.read_text(encoding="utf-8")

    # Pin observed behavior. If link is replaced by a regular file holding
    # "world\n" and real.txt is unchanged → that's the silent clobber bug.
    # If real.txt now contains "world\n" → atomic_write followed the symlink.
    # Fixed 2026-05-23: edit now resolves symlinks before atomic-write,
    # so the link stays a link and the real file receives the edit.
    assert link_is_symlink, "symlink must survive edit (no clobber)"
    assert real_after == "world\n"
    assert link_content == "world\n"


# ---------------------------------------------------------------------------
# Corrupted file
# ---------------------------------------------------------------------------

def test_read_truncated_utf8_mid_codepoint_does_not_crash(tmp_path: Path) -> None:
    """Half-written UTF-8 (multibyte cut) → errors='replace' covers it."""
    # 0xC3 0xA9 = é. Write only the lead byte.
    f = tmp_path / "broken.txt"
    f.write_bytes(b"caf\xc3")
    out = supertool.op_read(str(f))
    assert "ERROR" not in out
    # 1 line, 4 bytes, content rendered (with replacement char for the lead)
    assert "1 lines, 4 bytes" in out or "lines, 4 bytes" in out


def test_read_bom_prefixed_utf8_is_not_flagged_binary(tmp_path: Path) -> None:
    """UTF-8 BOM (EF BB BF) is valid text — must not trigger 'bin' or 'non-utf8'."""
    f = tmp_path / "bom.txt"
    f.write_bytes(b"\xef\xbb\xbfhello\n")
    out = supertool.op_read(str(f))
    first = out.splitlines()[0]
    assert "bin" not in first
    assert "non-utf8" not in first


def test_read_null_bytes_late_in_file_not_misflagged(tmp_path: Path) -> None:
    """NULs beyond the first 8KB shouldn't flip 'bin' (sample only covers 8KB)."""
    f = tmp_path / "late_nul.txt"
    # 9KB of plain ASCII, then a NUL
    f.write_bytes(b"a" * 9000 + b"\x00tail")
    out = supertool.op_read(str(f))
    first = out.splitlines()[0]
    assert "bin" not in first


def test_edit_preserves_bytes_around_match_in_partly_corrupted_file(tmp_path: Path) -> None:
    """edit should not silently mangle a corrupted UTF-8 region outside the match.

    File: 'GOOD\\xc3MATCH\\xc3BAD' — lone 0xC3 bytes (illegal UTF-8 on their own).
    Edit MATCH → REPLACED. Bytes around must round-trip; current implementation
    reads with errors='replace' then writes back, which CAN lose data — pin
    that observation.
    """
    f = tmp_path / "mixed.bin"
    original = b"GOOD\xc3MATCH\xc3BAD"
    f.write_bytes(original)

    out = supertool.op_edit("MATCH", "REPLACED", str(f))
    assert "ERROR" not in out

    after = f.read_bytes()
    # Fixed 2026-05-23: surrogateescape encoding round-trips lone illegal
    # bytes — they must survive the edit unchanged.
    assert after == b"GOOD\xc3REPLACED\xc3BAD"


# ---------------------------------------------------------------------------
# Coverage targets — OSError paths in edit/replace/atomic_write
# ---------------------------------------------------------------------------

def test_op_edit_unreadable_file_returns_error(tmp_path: Path) -> None:
    """Edit a file with no read permission → clean ERROR, no traceback."""
    f = tmp_path / "locked.txt"
    f.write_text("content\n")
    os.chmod(f, 0o000)
    try:
        out = supertool.op_edit("content", "new", str(f))
        assert "ERROR" in out
        assert "Traceback" not in out
    finally:
        os.chmod(f, 0o644)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows filesystem ignores chmod 0o000 — file stays readable.",
)
def test_op_replace_skips_unreadable_binary_peek(tmp_path: Path) -> None:
    """Replace must continue when binary peek hits OSError on a single file."""
    f1 = tmp_path / "good.txt"
    f1.write_text("findme here\n")
    f2 = tmp_path / "locked.txt"
    f2.write_text("findme there\n")
    os.chmod(f2, 0o000)
    try:
        out = supertool.op_replace("findme", "GOT", str(tmp_path))
        assert "GOT here" in f1.read_text(encoding="utf-8")
        # locked file silently skipped, not raised
        assert "ERROR" not in out
    finally:
        os.chmod(f2, 0o644)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Mocked fdopen returns a stub that never closes the fd; Windows can't "
    "unlink an open file, so the cleanup-leftover assertion fails on Windows only.",
)
def test_atomic_write_recovers_from_write_failure(tmp_path: Path, monkeypatch) -> None:
    """If write fails mid-flight, tmp file is cleaned up + original preserved."""
    f = tmp_path / "target.txt"
    f.write_text("original\n")
    original_bytes = f.read_bytes()

    # Force the inner write to raise by monkey-patching os.fdopen
    real_fdopen = os.fdopen
    def boom(*a, **kw):
        class _F:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def write(self, _): raise OSError("disk full")
        return _F()
    monkeypatch.setattr(os, "fdopen", boom)
    raised = False
    try:
        supertool._atomic_write(str(f), "new content")
    except OSError:
        raised = True
    monkeypatch.setattr(os, "fdopen", real_fdopen)
    assert raised, "exception must propagate"
    # Original unchanged
    assert f.read_bytes() == original_bytes
    # No leftover .supertool-*.tmp files in target dir
    leftovers = list(tmp_path.glob(".supertool-*.tmp"))
    assert leftovers == [], f"tmp not cleaned: {leftovers}"
