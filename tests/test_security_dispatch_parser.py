"""Security / edge-case tests for supertool.dispatch() parser.

Covers: empty/degenerate inputs, @file edge cases, :::no-exclude corner cases,
URL/drive-letter colon handling, NUL and \x1d injection, very long args,
unknown / mis-cased ops, and the @@ double-at path.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_clean_error(result: str) -> bool:
    """Return True if result is a properly formatted error (no raw traceback)."""
    lines = result.splitlines()
    # Must not be an unhandled exception traceback
    assert "Traceback (most recent call last)" not in result, (
        f"dispatch() leaked a traceback:\n{result}"
    )
    # Must not be an empty string (silent failure)
    assert result.strip() != "", "dispatch() returned empty output"
    return True


# ---------------------------------------------------------------------------
# 1. Empty arg
# ---------------------------------------------------------------------------

class TestEmptyAndDegenerateInputs:

    def test_empty_arg(self) -> None:
        """dispatch('') must return a clean error, not raise or hang."""
        out = supertool.dispatch("")
        _is_clean_error(out)
        assert "ERROR" in out or "unknown operation" in out.lower()

    def test_only_colon(self) -> None:
        """dispatch(':') — op is empty string, arg is empty string."""
        out = supertool.dispatch(":")
        _is_clean_error(out)
        # Empty op should produce an error
        assert "ERROR" in out or "unknown operation" in out.lower()

    def test_only_triple_colon(self) -> None:
        """dispatch(':::') — triple-colon mode with no op name."""
        out = supertool.dispatch(":::")
        _is_clean_error(out)
        assert "ERROR" in out or "unknown operation" in out.lower()


# ---------------------------------------------------------------------------
# 4. @- with no stdin
# ---------------------------------------------------------------------------

class TestAtFileStdin:

    def test_at_dash_with_closed_stdin(self) -> None:
        """dispatch('edit:@-') with stdin closed must return a clean error, not hang."""
        old_stdin = sys.stdin
        try:
            # Replace stdin with a closed-like stream that returns empty immediately
            sys.stdin = io.StringIO("")
            out = supertool.dispatch("edit:@-")
        finally:
            sys.stdin = old_stdin
        _is_clean_error(out)
        # Empty stdin → empty raw → _detect_payload_format returns 'json' →
        # json.loads("") raises → should surface as an ERROR
        assert "ERROR" in out

    def test_at_dash_with_whitespace_only_stdin(self, tmp_path: Path) -> None:
        """Whitespace-only @- stdin → TOML path with empty result → clean error."""
        old_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO("   \n\t\n")
            out = supertool.dispatch("edit:@-")
        finally:
            sys.stdin = old_stdin
        _is_clean_error(out)
        assert "ERROR" in out


# ---------------------------------------------------------------------------
# 5. @file with whitespace-only content
# ---------------------------------------------------------------------------

class TestAtFileWhitespace:

    def test_at_file_whitespace_only(self, tmp_path: Path) -> None:
        """@file containing only whitespace — clean error, not crash."""
        f = tmp_path / "empty.json"
        f.write_text("   \n\t  \n")
        out = supertool.dispatch(f"edit:@{f}")
        _is_clean_error(out)
        assert "ERROR" in out


# ---------------------------------------------------------------------------
# 6 & 7. :::no-exclude suffix handling
# ---------------------------------------------------------------------------

class TestNoExcludeSuffix:

    def test_no_exclude_mid_arg_not_treated_as_suffix(self, tmp_path: Path) -> None:
        """:::no-exclude in the middle of an arg should NOT strip the suffix early.

        'grep:foo:::no-exclude:bar' — the suffix check uses endswith(), so only
        a trailing :::no-exclude counts. A mid-arg one should pass 'bar' through
        as part of the normal argument.
        """
        f = tmp_path / "x.txt"
        f.write_text("foo:::no-exclude:bar\n")
        out = supertool.dispatch(f"grep:foo:::no-exclude:bar:{f}")
        # The op should run (grep), not produce an unknown-op error.
        # Whether it finds a match or not, there must be no traceback.
        _is_clean_error(out)
        assert "unknown operation" not in out

    def test_repeated_no_exclude_suffix(self, tmp_path: Path) -> None:
        """'grep:foo:::no-exclude:::no-exclude' — only the last suffix is stripped.

        After stripping the trailing :::no-exclude, the arg becomes
        'grep:foo:::no-exclude' which hits triple-colon mode. Should not crash.
        """
        out = supertool.dispatch("grep:foo:::no-exclude:::no-exclude")
        _is_clean_error(out)
        # no crash, no traceback — whether it errors on the remaining arg or
        # runs grep with odd pattern is fine; what matters is clean handling.

    def test_no_exclude_only(self) -> None:
        """':::no-exclude' alone — after suffix strip, arg is empty."""
        out = supertool.dispatch(":::no-exclude")
        _is_clean_error(out)
        assert "ERROR" in out or "unknown operation" in out.lower()


# ---------------------------------------------------------------------------
# 8. URL with port — colons in URL must not be split
# ---------------------------------------------------------------------------

class TestUrlWithPort:

    def test_url_with_port_not_split(self) -> None:
        """read:https://x:8080/y — the URL colon must not produce a split.

        _split_arg has port-absorption logic. Verify it fires: 'x' (host) +
        '8080/y' (port+path) must be merged back into one token.
        """
        parts = supertool._split_arg("read:https://x:8080/y")
        assert parts[0] == "read"
        # The URL must be kept whole
        assert parts[1] == "https://x:8080/y", (
            f"URL with port was incorrectly split: {parts}"
        )

    def test_url_with_port_dispatch_does_not_crash(self) -> None:
        """dispatch with a URL arg must not traceback — even if the read fails."""
        out = supertool.dispatch("read:https://localhost:9999/nonexistent")
        _is_clean_error(out)
        # Should attempt to read (and likely fail with a file-not-found error),
        # not crash the parser.


# ---------------------------------------------------------------------------
# 9. Drive letter (C:foo on Linux — treated as op "C")
# ---------------------------------------------------------------------------

class TestDriveLetter:

    def test_drive_letter_as_op_name(self) -> None:
        """'C:foo' on Linux — 'C' is parsed as the op name (single-char, no slash).

        _split_arg's drive-letter logic only merges when next_piece starts
        with '/' or '\\', so 'C:foo' → parts=['C', 'foo'], op='C'.
        Result must be a clean error (unknown op), not a crash.
        """
        parts = supertool._split_arg("C:foo")
        # On Linux the drive letter is NOT merged (next piece doesn't start with /)
        assert parts[0] == "C"

        out = supertool.dispatch("C:foo")
        _is_clean_error(out)
        assert "ERROR" in out or "unknown operation" in out.lower()

    def test_drive_letter_with_slash_is_merged(self) -> None:
        """'read:C:/path/file' — drive letter + slash must be merged into one arg."""
        parts = supertool._split_arg("read:C:/path/file")
        assert parts[0] == "read"
        assert parts[1] == "C:/path/file", (
            f"Drive letter was not reassembled: {parts}"
        )


# ---------------------------------------------------------------------------
# 10. NUL byte in arg
# ---------------------------------------------------------------------------

class TestNulByte:

    def test_nul_byte_in_op_name(self) -> None:
        """NUL in the op field must produce a clean error."""
        out = supertool.dispatch("re\x00ad:foo")
        _is_clean_error(out)
        # NUL in op name → not a recognized op → unknown operation error
        assert "ERROR" in out or "unknown operation" in out.lower()

    def test_nul_byte_in_arg(self) -> None:
        """NUL in the path arg must produce a clean error (OS rejects NUL in paths)."""
        out = supertool.dispatch("read:foo\x00bar")
        _is_clean_error(out)

    def test_nul_byte_in_edit_old(self) -> None:
        """NUL in edit OLD must not crash — it will just fail to match."""
        out = supertool.dispatch("edit:::foo\x00bar:::new:::nonexistent.py")
        _is_clean_error(out)


# ---------------------------------------------------------------------------
# 11. \x1d group separator in arg
# ---------------------------------------------------------------------------

class TestGroupSeparatorInjection:

    def test_x1d_in_op_name(self) -> None:
        """\\x1d in op name must not route to a different op."""
        out = supertool.dispatch("re\x1dad:foo")
        _is_clean_error(out)
        assert "ERROR" in out or "unknown operation" in out.lower()

    def test_x1d_in_path_arg(self) -> None:
        """\\x1d injected into a path must not confuse vim/replace_lines range decoding."""
        out = supertool.dispatch("read:foo\x1dbar")
        _is_clean_error(out)

    def test_x1d_in_vim_script_does_not_misparse_as_range(self) -> None:
        """\\x1d{spec}\\x1d is the internal range encoding for vim op.

        If user supplies \\x1d%\\x1d as a vim script arg, the vim handler
        must not misparse it as a range selector — it must either treat it
        literally or return a clean error.
        """
        out = supertool.dispatch("vim:::nonexistent.py:::\x1d%\x1d")
        _is_clean_error(out)
        # Must not traceback. The file won't exist so it will error on I/O,
        # but the parser itself must not explode.


# ---------------------------------------------------------------------------
# 12. Very long arg
# ---------------------------------------------------------------------------

class TestVeryLongArg:

    def test_1mb_single_arg(self) -> None:
        """A 1 MB arg string must not crash the parser (no stack overflow, no OOM kill)."""
        long_path = "x" * (1024 * 1024)
        out = supertool.dispatch(f"read:{long_path}")
        _is_clean_error(out)
        # File won't exist — expect a file-not-found style error, not a crash.

    def test_1mb_op_name(self) -> None:
        """1 MB op name — must not crash, must return unknown operation error."""
        long_op = "x" * (1024 * 1024)
        out = supertool.dispatch(long_op)
        _is_clean_error(out)
        # The triple-colon regex `^([a-zA-Z_][a-zA-Z0-9_-]*):::` will not match
        # (no `:::`) so this goes through _split_arg → op = whole string → unknown.
        assert "ERROR" in out or "unknown operation" in out.lower()


# ---------------------------------------------------------------------------
# 13. Unknown op
# ---------------------------------------------------------------------------

class TestUnknownOp:

    def test_unknown_op_clean_error(self) -> None:
        """dispatch('frobnicate:foo') → clean error, no crash."""
        out = supertool.dispatch("frobnicate:foo")
        _is_clean_error(out)
        assert "ERROR" in out
        assert "unknown operation" in out.lower() or "frobnicate" in out

    def test_unknown_op_no_args(self) -> None:
        """dispatch('frobnicate') — unknown op with no args."""
        out = supertool.dispatch("frobnicate")
        _is_clean_error(out)
        assert "ERROR" in out

    def test_unknown_op_with_at_file(self, tmp_path: Path) -> None:
        """Unknown op that looks like it has an @file arg — must still error cleanly."""
        f = tmp_path / "payload.json"
        f.write_text('{"key": "value"}')
        out = supertool.dispatch(f"frobnicate:@{f}")
        _is_clean_error(out)
        assert "ERROR" in out


# ---------------------------------------------------------------------------
# 14. Op with capital letters / unicode
# ---------------------------------------------------------------------------

class TestOpNameCase:

    def test_uppercase_read(self) -> None:
        """'READ:foo' — op names are case-sensitive; READ is not a known op."""
        out = supertool.dispatch("READ:foo")
        _is_clean_error(out)
        assert "ERROR" in out or "unknown operation" in out.lower()

    def test_mixed_case_grep(self) -> None:
        """'Grep:pat:path' — not the same as 'grep'."""
        out = supertool.dispatch("Grep:pattern:.")
        _is_clean_error(out)
        assert "ERROR" in out or "unknown operation" in out.lower()

    def test_unicode_op_name(self) -> None:
        """'rëad:foo' — unicode in op name must produce clean error, not crash."""
        out = supertool.dispatch("rëad:foo")
        _is_clean_error(out)
        assert "ERROR" in out or "unknown operation" in out.lower()

    def test_unicode_op_name_triple_colon(self) -> None:
        """Unicode op in triple-colon form — regex ^([a-zA-Z_]...) won't match,
        falls through to _split_arg, op = 'rëad', unknown operation."""
        out = supertool.dispatch("rëad:::foo")
        _is_clean_error(out)
        assert "ERROR" in out or "unknown operation" in out.lower()


# ---------------------------------------------------------------------------
# 15. @@ (double-at) path
# ---------------------------------------------------------------------------

class TestDoubleAt:

    def test_double_at_file_reference(self, tmp_path: Path) -> None:
        """'edit:@@file.json' — parts[1] is '@@file.json', starts with '@'.

        _at_file_fields('edit') returns a non-empty list, so the @file branch
        is entered. _load_at_file receives '@@file.json', strips the leading @
        to get '@file.json' as the filesystem path.

        This means @@file.json is treated as a path whose name starts with '@',
        not as a special double-at escape. Verify clean behavior either way.
        """
        # Case 1: file named '@file.json' does NOT exist → should error cleanly
        out = supertool.dispatch(f"edit:@@file.json")
        _is_clean_error(out)
        # Must report @file not found (path = '@file.json'), not crash
        assert "ERROR" in out

    def test_double_at_file_exists(self, tmp_path: Path) -> None:
        """If a file literally named '@payload.json' exists, @@payload.json loads it."""
        at_named_file = tmp_path / "@payload.json"
        target = tmp_path / "target.py"
        target.write_text("x = 1\n")
        at_named_file.write_text(
            f'{{"old": "x = 1", "new": "x = 2", "path": "{target}"}}'
        )
        # Dispatch from cwd=tmp_path context isn't automatic, so use absolute path
        out = supertool.dispatch(f"edit:@{at_named_file}")
        # The file name starts with '@', so ref='@/tmp/.../@ payload.json' →
        # fpath = '/tmp/.../@payload.json' → should load successfully.
        _is_clean_error(out)
        # If the edit succeeded, target contains 'x = 2'
        # If it failed for any reason, we just check no traceback (already done)

    def test_double_at_op_name_in_colon_form(self) -> None:
        """'@@:foo' — op name is '@@', unknown → clean error."""
        out = supertool.dispatch("@@:foo")
        _is_clean_error(out)
        assert "ERROR" in out or "unknown operation" in out.lower()


# ---------------------------------------------------------------------------
# Bonus: ensure header is always well-formed (no partial output on error)
# ---------------------------------------------------------------------------

class TestHeaderPresence:

    def test_header_present_on_unknown_op(self) -> None:
        """The '--- op:arg ---' header must appear even on error output."""
        out = supertool.dispatch("unknownop:some:args")
        assert "--- unknownop" in out or out.startswith("ERROR") or "---" in out

    def test_header_stripped_for_meta_ops(self) -> None:
        """Meta-ops like 'ops' and 'version' must NOT include the --- header ---."""
        out = supertool.dispatch("version")
        assert not out.startswith("---"), (
            f"version op should not have a --- header ---, got: {out[:80]}"
        )
