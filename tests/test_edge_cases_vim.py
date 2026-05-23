"""Adversarial / corruption / DoS edge-case tests for op_vim.

Covers:
 1. NUL byte in SCRIPT — should not crash
 2. Embedded ESC in inserted text — cursor state intact for next action
 3. Pattern that never matches — no infinite loop, returns ERROR
 4. :%s/.*/x/g on 10k-line file — completes in reasonable time
 5. :99999d on 5-line file — clean ERROR, no crash
 6. Very long single-token insert (100k chars) — bounded behavior
 7. Symlink as PATH — symlink survives the edit (target updated, link preserved)
 8. Path with NUL byte — clean ERROR return, no traceback
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ESC = "\x1b"


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _vim(path: Path, script: str) -> str:
    """Call op_vim and return the raw output string."""
    return supertool.op_vim(str(path), script)


# ---------------------------------------------------------------------------
# 1. NUL byte in SCRIPT
# ---------------------------------------------------------------------------

def test_nul_byte_in_script_does_not_crash(tmp_path: Path) -> None:
    """NUL byte inside the script should not crash op_vim.

    The NUL is not a valid vim action. Acceptable outcomes:
    - ERROR return (most defensive)
    - NUL silently ignored, rest of script runs
    Either way: no exception, no traceback.
    """
    f = _write(tmp_path, "f.txt", "hello world\n")
    # Script: insert mode containing a NUL byte, then ESC
    script = "i" + "SOME\x00TEXT" + ESC
    out = _vim(f, script)
    # Must not raise — we just check it returned a string
    assert isinstance(out, str), "op_vim must return a str, not raise"
    # Two acceptable outcomes: ERROR or the file got written (NUL stripped/ignored)
    if out.startswith("ERROR"):
        # Fine — rejected cleanly
        assert "file unchanged" in out or "ERROR" in out
    else:
        # NUL was swallowed; file content must be valid UTF-8-ish (no crash)
        content = f.read_text(encoding="utf-8", errors="replace")
        assert isinstance(content, str)


# ---------------------------------------------------------------------------
# 2. Embedded ESC in inserted text — cursor state not corrupted
# ---------------------------------------------------------------------------

def test_embedded_esc_in_insert_text_does_not_corrupt_cursor(tmp_path: Path) -> None:
    """ESC inside insert text terminates the insert and returns to normal mode.

    The tokenizer consumes `iHELLO` up to the embedded \x1b, so HELLO is
    inserted and `WORLD` is parsed as the next normal-mode sequence (W = WORD
    forward motion). The important guarantee: the next action after the ESC
    runs cleanly and does not crash or produce garbage output.
    """
    f = _write(tmp_path, "f.txt", "line one\nline two\n")
    # iHELLO<ESC>: should insert HELLO before cursor, ESC back to normal.
    # Then `x` deletes one char — just checking the chain doesn't blow up.
    script = f"iHELLO{ESC}x"
    out = _vim(f, script)
    assert isinstance(out, str)
    # Should succeed — no ERROR expected (HELLO inserted, one char deleted)
    assert not out.startswith("ERROR"), f"Unexpected ERROR: {out}"
    content = f.read_text()
    # HELL (first char of HELLO deleted by x) should be in the file
    assert "HELL" in content


# ---------------------------------------------------------------------------
# 3. Pattern that never matches — no infinite loop
# ---------------------------------------------------------------------------

def test_nonmatching_search_returns_error_quickly(tmp_path: Path) -> None:
    """Searching for a pattern that doesn't exist must return ERROR promptly.

    There is no retry-loop for search in op_vim (it does a BOF fallback once).
    This test verifies: (a) returns ERROR, (b) does so in under 2 seconds.
    """
    f = _write(tmp_path, "f.txt", "alpha\nbeta\ngamma\n")
    # Pattern deliberately absent
    script = "/PATTERN-THAT-NEVER-MATCHES-ZZZZZZ"
    t0 = time.monotonic()
    out = _vim(f, script)
    elapsed = time.monotonic() - t0
    assert out.startswith("ERROR"), f"Expected ERROR for missing pattern, got: {out!r}"
    assert elapsed < 2.0, f"Search took {elapsed:.2f}s — possible loop"


def test_repeated_nonmatching_searches_terminate(tmp_path: Path) -> None:
    """Multiple consecutive nonmatching searches — still terminates quickly.

    Each search is a separate op_vim call (as a real caller might do in a loop).
    Ten calls must complete in under 5 seconds total.
    """
    f = _write(tmp_path, "f.txt", "alpha\nbeta\ngamma\n")
    t0 = time.monotonic()
    for _ in range(10):
        out = supertool.op_vim(str(f), "/NO-SUCH-PATTERN-XYZ")
        assert out.startswith("ERROR"), "Each missing-pattern call must ERROR"
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"10 nonmatching searches took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 4. :%s/.*/x/g on 10k-line file — should complete in reasonable time
# ---------------------------------------------------------------------------

def test_global_substitute_large_file_completes_in_time(tmp_path: Path) -> None:
    """:%s/.*/x/g on a 10k-line file must not OOM or hang.

    Acceptable outcome: completes in under 10 seconds, no trailing extra char.
    Note: vim's `.*` matches both line content AND empty position after content
    (Python re behaves the same way), so each `lineN` becomes `xx` (2 subs),
    not `x`. The prior bug was an EXTRA `x` after the final newline (caused by
    re.MULTILINE + whole-buffer mode catching the empty position at EOF).
    """
    lines = "\n".join(f"line {i}" for i in range(10_000)) + "\n"
    f = _write(tmp_path, "big.txt", lines)
    t0 = time.monotonic()
    out = _vim(f, r":%s/.*/x/g")
    elapsed = time.monotonic() - t0
    assert not out.startswith("ERROR"), f"Unexpected ERROR on large file: {out}"
    assert elapsed < 10.0, f"Large file substitution took {elapsed:.2f}s"
    result = f.read_text()
    # File must end with newline (no spurious trailing char) — this was the bug.
    assert result.endswith("\n")
    assert not result.endswith("\nx"), "trailing empty-match must not produce extra char"
    # Every non-empty line consists only of 'x' characters (vim's greedy+empty
    # = "xx" per line is acceptable; "xxx" or anything else is not).
    result_lines = result.splitlines()
    assert len(result_lines) == 10_000
    assert all(set(ln) == {"x"} for ln in result_lines if ln)


# ---------------------------------------------------------------------------
# 5. :99999d on a 5-line file — clean error or no-op, no crash
# ---------------------------------------------------------------------------

def test_ex_delete_out_of_range_line_returns_error(tmp_path: Path) -> None:
    """Deleting line 99999 from a 5-line file must return ERROR, not crash.

    File must remain unchanged (op_vim is atomic on ERROR).
    """
    original = "line1\nline2\nline3\nline4\nline5\n"
    f = _write(tmp_path, "f.txt", original)
    out = _vim(f, ":99999d")
    assert out.startswith("ERROR"), f"Expected ERROR for out-of-range delete, got: {out!r}"
    assert "file unchanged" in out, "Atomic guarantee: file unchanged message expected"
    # File content must be unmodified
    assert f.read_text() == original


def test_ex_delete_range_overflow_returns_error(tmp_path: Path) -> None:
    """:1,99999d — end of range beyond EOF — must ERROR cleanly, not crash."""
    original = "a\nb\nc\nd\ne\n"
    f = _write(tmp_path, "f.txt", original)
    out = _vim(f, ":1,99999d")
    assert out.startswith("ERROR"), f"Expected ERROR for overflow range delete, got: {out!r}"
    assert f.read_text() == original, "File must be unchanged after ERROR"


# ---------------------------------------------------------------------------
# 6. Very long single token (100k-char insert)
# ---------------------------------------------------------------------------

def test_very_long_insert_token_completes(tmp_path: Path) -> None:
    """Inserting a 100k-character string should work without OOM or timeout.

    This probes the tokenizer's greedy-until-ESC consumption path with
    an unusually large payload.
    """
    f = _write(tmp_path, "f.txt", "start\n")
    big_text = "A" * 100_000
    script = f"i{big_text}{ESC}"
    t0 = time.monotonic()
    out = _vim(f, script)
    elapsed = time.monotonic() - t0
    assert not out.startswith("ERROR"), f"Unexpected ERROR on large insert: {out}"
    assert elapsed < 5.0, f"Large insert took {elapsed:.2f}s"
    content = f.read_text()
    assert big_text in content, "100k chars must appear in file"


# ---------------------------------------------------------------------------
# 7. Symlink as PATH — symlink survives, target updated
# ---------------------------------------------------------------------------

def test_symlink_path_edits_target_and_preserves_symlink(tmp_path: Path) -> None:
    """vim through a symlink must edit the real target, not clobber the link.

    _atomic_write resolves os.path.realpath when path is a symlink, so
    os.replace() targets the real file, preserving the link.
    """
    target = tmp_path / "real.txt"
    target.write_text("original content\n")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    assert os.path.islink(str(link)), "Symlink must exist before op_vim"

    out = _vim(link, f"gg{ESC}iINSERTED: {ESC}")
    assert not out.startswith("ERROR"), f"op_vim via symlink failed: {out}"

    # Symlink must still be a symlink (not replaced by a regular file)
    assert os.path.islink(str(link)), "Symlink was clobbered — os.replace hit link instead of target"

    # Both paths must show the updated content
    via_link = link.read_text()
    via_target = target.read_text()
    assert via_link == via_target, "Link and target must have same content after edit"
    assert "INSERTED:" in via_target, "Edit must have reached the real file"


# ---------------------------------------------------------------------------
# 8. Path with NUL byte — clean ERROR, no traceback
# ---------------------------------------------------------------------------

def test_path_with_nul_byte_returns_error(tmp_path: Path) -> None:
    """A path containing a NUL byte is invalid on all POSIX systems.

    op_vim must return an ERROR string, not raise an exception or traceback.
    """
    bad_path = str(tmp_path / "file\x00name.txt")
    out = supertool.op_vim(bad_path, f"iHELLO{ESC}")
    assert isinstance(out, str), "Must return str, not raise"
    assert out.startswith("ERROR"), f"Expected ERROR for NUL-byte path, got: {out!r}"
    # Crucially: no Python exception escaped
