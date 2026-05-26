"""Security and edge-case audit for the @file payload parser.

Covers path traversal, NUL bytes, symlinks, malformed/malicious payloads,
type confusion, huge files, stdin edge cases, and TOML literal strings.

Each test either:
  - documents current (possibly unsafe) behavior and pins it, OR
  - asserts a clean-error contract that the implementation must honour.

Run:
  cd /Users/floriandavid/Documents/claude-supertool
  python3 -m pytest tests/test_edge_cases_at_file_security.py -v --no-cov
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Ensure the repo root is importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).parent.parent))
import supertool  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json_file(tmp_path: Path, name: str, payload: Any) -> Path:
    f = tmp_path / name
    f.write_text(json.dumps(payload), encoding="utf-8")
    return f


def _dispatch_reset(arg: str) -> str:
    """dispatch() with the @file registry cleared so tests are isolated."""
    supertool._AT_FILE_REGISTRY_BUILT = False  # type: ignore[attr-defined]
    supertool._AT_FILE_REGISTRY = {}  # type: ignore[attr-defined]
    return supertool.dispatch(arg)


# ---------------------------------------------------------------------------
# 1. Path traversal in @file path
# ---------------------------------------------------------------------------

class TestPathTraversalAtFile:
    """edit:@../../etc/passwd — does the parser escape cwd?

    The @file mechanism calls open(fpath) where fpath = ref[1:] with no
    canonicalisation or cwd check. This test documents whether the traversal
    is blocked or silently allowed.

    Severity: HIGH — allows reading arbitrary files accessible to the process.
    """

    def test_path_traversal_relative_dotdot(self, tmp_path: Path, monkeypatch) -> None:
        """../../../ escape: either clean error or file-not-found; must not crash."""
        monkeypatch.chdir(tmp_path)

        # Create a "secret" file one level up in a temp parent.
        parent = tmp_path.parent
        secret = parent / f"_supertool_secret_{os.getpid()}.txt"
        secret.write_text("secret_content\n")
        try:
            relative = os.path.relpath(secret, tmp_path)  # e.g. "../_supertool_secret_NNN.txt"
            out = _dispatch_reset(f"edit:@{relative}")
        finally:
            secret.unlink(missing_ok=True)

        # OBSERVED: supertool does NOT block path traversal.
        # It will attempt to open the file. If missing fields → ERROR on payload
        # parse, which is a clean error by accident (not by design).
        # The file IS read — that's the security issue.
        # We pin the observed behavior: output contains ERROR (because the traversed
        # file is not valid JSON/TOML edit payload), not a crash.
        assert "ERROR" in out, (
            "BUG: traversal file read silently or crashed instead of erroring cleanly"
        )
        assert "Traceback" not in out

    def test_path_traversal_absolute_path_outside_cwd(self, tmp_path: Path) -> None:
        """Absolute path outside cwd — should produce ERROR, not crash."""
        # Point at /etc/passwd (always exists on macOS/Linux).
        out = _dispatch_reset("edit:@/etc/passwd")
        # /etc/passwd is a text file; not valid JSON/TOML edit payload → ERROR.
        assert "ERROR" in out
        assert "Traceback" not in out
        # BUG NOTE: the file is still opened and read. No path restriction in place.


# ---------------------------------------------------------------------------
# 2. NUL byte in @file path
# ---------------------------------------------------------------------------

class TestNulByteAtFilePath:
    """edit:@foo\x00.json — Python open() raises ValueError for embedded NUL.

    Severity: LOW — likely results in a clean exception; audit confirms it's
    caught and surfaced as ERROR rather than crashing the process.
    """

    def test_nul_byte_in_at_file_path(self, tmp_path: Path) -> None:
        nul_path = str(tmp_path) + "/payload" + "\x00" + ".json"
        out = _dispatch_reset(f"edit:@{nul_path}")
        assert "ERROR" in out, "NUL byte in @file path must produce clean ERROR"
        assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 3. Symlinked @file
# ---------------------------------------------------------------------------

class TestSymlinkedAtFile:
    """edit:@/tmp/link_to_secret.json — does the parser follow symlinks?

    The parser calls open(fpath) without resolving symlinks, so it will
    follow them. This test documents that behavior.

    Severity: MED — in a restricted environment a symlink could point to
    sensitive data. Supertool reads it without restriction.
    """

    def test_symlink_to_nonexistent_target_errors_cleanly(self, tmp_path: Path) -> None:
        """Dangling symlink → file not found → clean ERROR."""
        link = tmp_path / "link.json"
        link.symlink_to(tmp_path / "does_not_exist.json")
        out = _dispatch_reset(f"edit:@{link}")
        assert "ERROR" in out
        assert "Traceback" not in out

    def test_symlink_followed_and_read(self, tmp_path: Path) -> None:
        """Symlink to valid payload file: link IS followed — document behavior.

        BUG: no symlink restriction. Any readable file the process can reach
        via a symlink is accessible through the @file route.
        """
        real = tmp_path / "real_payload.json"
        target = tmp_path / "target.txt"
        target.write_text("hello world\n")
        real.write_text(json.dumps({"old": "hello world", "new": "replaced", "path": str(target)}))
        link = tmp_path / "link_to_payload.json"
        link.symlink_to(real)

        out = _dispatch_reset(f"edit:@{link}")
        # The symlink is followed → edit executes → "edited" in output.
        assert "ERROR" not in out, "Symlink should be followed; edit should succeed"
        assert "replaced" in target.read_text()

    def test_symlink_to_secret_file_is_read(self, tmp_path: Path) -> None:
        """Symlink pointing to a non-payload file: read occurs, parse fails, ERROR returned.

        The ERROR comes from JSON/TOML parse failure, not from path restriction.
        The secret file's bytes WERE read before the error was raised.
        Severity: MED — read happens silently before error surfacing.
        """
        secret = tmp_path / "secret.txt"
        secret.write_text("top secret content\n")
        link = tmp_path / "link_to_secret.json"
        link.symlink_to(secret)

        out = _dispatch_reset(f"edit:@{link}")
        assert "ERROR" in out  # parse error, not access denial
        assert "Traceback" not in out
        # The file was silently read. No access control whatsoever.


# ---------------------------------------------------------------------------
# 4. Malformed JSON (truncated)
# ---------------------------------------------------------------------------

class TestMalformedJson:
    """Truncated JSON → clean error, no crash.

    Severity: LOW — already handled; this confirms the contract.
    """

    def test_truncated_json_returns_clean_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "truncated.json"
        bad.write_text('{"old": "x", "new": ', encoding="utf-8")
        out = _dispatch_reset(f"edit:@{bad}")
        assert "ERROR" in out
        assert "JSON parse error" in out
        assert "Traceback" not in out

    def test_empty_file_returns_clean_error(self, tmp_path: Path) -> None:
        """Empty file: _detect_payload_format falls through to 'json' (empty string).
        json.loads('') raises JSONDecodeError → clean ERROR.
        """
        empty = tmp_path / "empty.json"
        empty.write_text("", encoding="utf-8")
        out = _dispatch_reset(f"edit:@{empty}")
        assert "ERROR" in out
        assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 5. JSON with unknown extra fields
# ---------------------------------------------------------------------------

class TestExtraFieldsInPayload:
    """Extra fields in JSON payload: documented to be silently ignored.

    Severity: LOW — extra fields can't cause code execution; they're discarded
    by the lower_payload lookup. But an 'evil_field' with shell metacharacters
    must not be interpreted anywhere.
    """

    def test_extra_fields_are_ignored(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("a = 1\n")
        spec = _write_json_file(tmp_path, "e.json", {
            "old": "a = 1",
            "new": "a = 99",
            "path": str(target),
            "evil_field": "'; DROP TABLE users; --",
            "injected": "$(rm -rf /)",
        })
        out = _dispatch_reset(f"edit:@{spec}")
        # Extra fields silently ignored; edit succeeds.
        assert "ERROR" not in out
        assert "a = 99" in target.read_text()

    def test_extra_fields_with_shell_metacharacters_not_executed(self, tmp_path: Path) -> None:
        """Shell metacharacters in extra fields must not be executed.

        Supertool doesn't pass payload values through a shell, so this should
        be safe — but we pin it explicitly.
        """
        canary = tmp_path / "canary.txt"
        target = tmp_path / "safe.py"
        target.write_text("x = 1\n")
        spec = _write_json_file(tmp_path, "e.json", {
            "old": "x = 1",
            "new": "x = 2",
            "path": str(target),
            "evil": f"$(touch {canary})",
        })
        _dispatch_reset(f"edit:@{spec}")
        assert not canary.exists(), "Shell injection via extra JSON field must not execute"


# ---------------------------------------------------------------------------
# 6. JSON type confusion
# ---------------------------------------------------------------------------

class TestJsonTypeConfusion:
    """Non-string types in required fields (old/new/path).

    _at_file_to_parts() coerces via str() — integers and arrays become strings
    like "123" or "[...]". This is a silent type confusion, not a rejection.

    Severity: MED — an integer 'old' becomes str(123) = "123"; search-and-replace
    proceeds on that string. Null path becomes "None" and will likely error on
    file-not-found.
    """

    def test_integer_old_is_coerced_to_string(self, tmp_path: Path) -> None:
        target = tmp_path / "nums.txt"
        target.write_text("123\n")
        spec = _write_json_file(tmp_path, "e.json", {
            "old": 123,       # integer, not string
            "new": "456",
            "path": str(target),
        })
        out = _dispatch_reset(f"edit:@{spec}")
        # str(123) = "123" → matches "123" in the file.
        # BUG NOTE: no type validation; silently coerces.
        assert "Traceback" not in out
        # Coercion succeeds and edit runs.
        if "ERROR" not in out:
            assert "456" in target.read_text()

    def test_list_new_is_coerced_to_string(self, tmp_path: Path) -> None:
        """new: ["array"] → str(["array"]) = "['array']" — silent coercion."""
        target = tmp_path / "f.txt"
        target.write_text("hello\n")
        spec = _write_json_file(tmp_path, "e.json", {
            "old": "hello",
            "new": ["array"],  # list, not string
            "path": str(target),
        })
        out = _dispatch_reset(f"edit:@{spec}")
        # No crash; coercion to "['array']" and the edit either finds/replaces
        # or silently writes that string into the file.
        assert "Traceback" not in out

    def test_null_path_errors_on_file_not_found(self, tmp_path: Path) -> None:
        """path: null → str(None) = "None" → file not found error."""
        spec = _write_json_file(tmp_path, "e.json", {
            "old": "x",
            "new": "y",
            "path": None,
        })
        out = _dispatch_reset(f"edit:@{spec}")
        assert "ERROR" in out
        assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 7. TOML with space-prefixed brace (auto-detect as TOML, not JSON)
# ---------------------------------------------------------------------------

class TestTomlSpacePrefixedBrace:
    """Payload starting with '{ ' (space after brace) — detected as JSON.

    _detect_payload_format skips leading whitespace then checks first char.
    A payload of '{ "old": ...' still starts with '{' → JSON.
    A payload of ' { "old": ...' (leading space) → skip space → '{' → JSON.
    The "space before brace" case mentioned in task 7 means a leading SPACE,
    not a space after the brace. Both routes below are tested.
    """

    def test_leading_space_then_brace_detected_as_json(self, tmp_path: Path) -> None:
        """' {' (leading space, then brace) → JSON. Clean parse."""
        target = tmp_path / "t.txt"
        target.write_text("old_val\n")
        # as_posix avoids backslash sequences in the JSON string (Windows
        # paths like `C:\Users\...` would otherwise be invalid escapes).
        payload_str = ' {"old": "old_val", "new": "new_val", "path": "' + target.as_posix() + '"}'
        f = tmp_path / "p.json"
        f.write_text(payload_str)
        out = _dispatch_reset(f"edit:@{f}")
        assert "ERROR" not in out
        assert "new_val" in target.read_text()

    def test_brace_space_content_is_json_not_toml(self, tmp_path: Path) -> None:
        """'{ ' prefix does NOT flip to TOML — still detected as JSON."""
        f = tmp_path / "p.json"
        f.write_text('{ "old": "x", "new": "y", "path": "/nonexistent" }')
        # Detection: first non-whitespace = '{' → json
        fmt = supertool._detect_payload_format(f.read_text())
        assert fmt == "json"

    def test_toml_format_when_starts_with_identifier(self, tmp_path: Path) -> None:
        """Payload starting with 'old = ...' → detected as TOML."""
        f = tmp_path / "p.toml"
        f.write_text('old = "x"\nnew = "y"\npath = "/nonexistent"\n')
        fmt = supertool._detect_payload_format(f.read_text())
        assert fmt == "toml"


# ---------------------------------------------------------------------------
# 8. Huge payload (DoS / OOM)
# ---------------------------------------------------------------------------

class TestHugePayload:
    """100 MB JSON file — does supertool cap or OOM?

    Severity: MED — no size cap; a 100 MB @file is fully read into memory.
    On constrained systems this could cause OOM. Pin the behavior.
    """

    @pytest.mark.slow
    def test_huge_json_is_fully_loaded_no_cap(self, tmp_path: Path) -> None:
        """BUG: no size cap on @file reads. 100 MB loaded into memory."""
        huge = tmp_path / "huge.json"
        # Build a payload with a very large "old" field (100 MB of 'a').
        padding = "a" * (100 * 1024 * 1024)
        payload = {"old": padding, "new": "tiny", "path": str(tmp_path / "x.txt")}
        huge.write_text(json.dumps(payload), encoding="utf-8")

        # The file is ~100 MB. supertool reads it fully, then fails on file-not-found
        # for the target. No size guard fires.
        out = _dispatch_reset(f"edit:@{huge}")
        assert "ERROR" in out  # target file doesn't exist
        assert "Traceback" not in out
        # BUG NOTE: no cap, no timeout. Full 100 MB was read into process memory.

    def test_moderately_large_json_succeeds(self, tmp_path: Path) -> None:
        """1 MB payload — should work without issues."""
        target = tmp_path / "f.txt"
        target.write_text("needle\n")
        padding = "x" * (1024 * 1024)
        spec = _write_json_file(tmp_path, "e.json", {
            "old": "needle",
            "new": "replaced" + padding,
            "path": str(target),
        })
        out = _dispatch_reset(f"edit:@{spec}")
        assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 9. Payload with embedded NUL bytes inside content fields
# ---------------------------------------------------------------------------

class TestEmbeddedNulInContentFields:
    """JSON allows \\u0000 in strings. Does it round-trip safely through edit?

    json.loads decodes \\u0000 to the actual NUL character (\x00 in Python).
    str() coercion preserves it. The downstream op_edit call then searches for
    a string containing \x00.

    Severity: LOW — implementation specific. If the file itself contains \x00
    the replacement may work; if not it just silently misses. No crash expected.
    """

    def test_nul_in_old_field_does_not_crash(self, tmp_path: Path) -> None:
        target = tmp_path / "f.txt"
        target.write_text("hello world\n")
        # JSON \u0000 decodes to the NUL character in Python
        payload_str = '{"old": "hello\\u0000world", "new": "replaced", "path": "' + str(target) + '"}'
        f = tmp_path / "p.json"
        f.write_text(payload_str)
        out = _dispatch_reset(f"edit:@{f}")
        # "hello\x00world" is not in the file → edit fails (not-found error) or no-op.
        # Either way: no crash.
        assert "Traceback" not in out

    def test_nul_in_new_field_written_to_file(self, tmp_path: Path) -> None:
        """If 'new' contains \x00, it gets written into the file via str()."""
        target = tmp_path / "g.txt"
        target.write_text("replace_me\n")
        payload_str = '{"old": "replace_me", "new": "val\\u0000ue", "path": "' + str(target) + '"}'
        f = tmp_path / "p.json"
        f.write_text(payload_str)
        out = _dispatch_reset(f"edit:@{f}")
        assert "Traceback" not in out
        # If the edit succeeded, NUL is now in the file.
        if "ERROR" not in out:
            content = target.read_bytes()
            assert b"\x00" in content, "NUL byte in 'new' field must be written to file"


# ---------------------------------------------------------------------------
# 10. TOML triple-single-quote with embedded escape sequences
# ---------------------------------------------------------------------------

class TestTomlTripleSingleQuote:
    """TOML ''' literal strings: backslashes must NOT be decoded.

    Per TOML spec: single-quoted strings are literal — no escape processing.
    Triple-single-quote is the multi-line literal form.
    '''content with \\n and \\x1b''' must deliver 'content with \\n and \\x1b'
    (two characters each: backslash + n, backslash + x1b).
    """

    def test_triple_single_quote_backslash_n_is_literal(self) -> None:
        raw = "old = '''content with \\\\n and \\\\x1b inside'''\nnew = 'y'\npath = '/tmp/z'\n"
        result = supertool._mini_toml_loads(raw)
        # \\\\n in Python source = \\n in the raw TOML = two chars preserved literally
        assert result["old"] == "content with \\\\n and \\\\x1b inside"

    def test_triple_single_quote_embedded_newline_preserved(self) -> None:
        """Actual newline inside ''' is preserved as-is (literal)."""
        raw = "old = '''\nfirst line\nsecond line\n'''\nnew = 'y'\n"
        result = supertool._mini_toml_loads(raw)
        # Per spec, first newline immediately after ''' is stripped.
        assert result["old"] == "first line\nsecond line\n"

    def test_triple_single_quote_no_escape_processing(self) -> None:
        """\\t inside ''' must remain backslash-t, not a tab."""
        raw = "val = '''tab:\\t here'''\n"
        result = supertool._mini_toml_loads(raw)
        assert result["val"] == "tab:\\t here"
        assert "\t" not in result["val"]

    def test_triple_double_quote_does_process_escapes(self) -> None:
        """\"\"\" (basic multiline) DOES process backslash escapes — contrast."""
        raw = 'val = """tab:\\t here"""\n'
        result = supertool._mini_toml_loads(raw)
        assert result["val"] == "tab:\t here"
        assert "\t" in result["val"]


# ---------------------------------------------------------------------------
# 11. @- stdin route
# ---------------------------------------------------------------------------

class TestStdinRoute:
    """@- reads payload from stdin via subprocess or mock.

    Tests: normal flow, closed stdin (read returns ''), empty stdin.
    """

    def test_stdin_valid_json_payload(self, tmp_path: Path) -> None:
        """Valid JSON via @- (mocked stdin) succeeds."""
        target = tmp_path / "s.txt"
        target.write_text("original\n")
        payload = json.dumps({"old": "original", "new": "replaced", "path": str(target)})

        with patch("sys.stdin", io.StringIO(payload)):
            supertool._AT_FILE_REGISTRY_BUILT = False
            supertool._AT_FILE_REGISTRY = {}
            out = supertool.dispatch("edit:@-")

        assert "ERROR" not in out
        assert "replaced" in target.read_text()

    def test_stdin_closed_returns_empty_string(self) -> None:
        """When stdin is closed/empty, sys.stdin.read() returns '' → JSON parse error."""
        # Empty string → _detect_payload_format returns 'json' → json.loads('') → JSONDecodeError
        with patch("sys.stdin", io.StringIO("")):
            supertool._AT_FILE_REGISTRY_BUILT = False
            supertool._AT_FILE_REGISTRY = {}
            out = supertool.dispatch("edit:@-")

        assert "ERROR" in out
        assert "Traceback" not in out

    def test_stdin_via_subprocess_blocks_on_no_input(self, tmp_path: Path) -> None:
        """@- with no stdin supplied: subprocess must not block forever.

        We pass stdin=subprocess.DEVNULL — this immediately closes stdin,
        so sys.stdin.read() returns '' immediately on the other end.
        No blocking (DEVNULL != a pipe that never closes).
        """
        supertool_path = Path(__file__).parent.parent / "supertool.py"
        proc = subprocess.run(
            [sys.executable, str(supertool_path), "edit:@-"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,  # must complete in 5 seconds
        )
        combined = proc.stdout.decode("utf-8", errors="replace") + proc.stderr.decode("utf-8", errors="replace")
        assert "ERROR" in combined or proc.returncode != 0, (
            "Closed stdin should produce an error or non-zero exit, not hang"
        )

    def test_stdin_malformed_json_via_subprocess(self, tmp_path: Path) -> None:
        """Malformed JSON via stdin → clean ERROR in subprocess output."""
        supertool_path = Path(__file__).parent.parent / "supertool.py"
        proc = subprocess.run(
            [sys.executable, str(supertool_path), "edit:@-"],
            input=b'{"old": "x", "new": ',  # truncated
            capture_output=True,
            timeout=5,
        )
        combined = proc.stdout.decode("utf-8", errors="replace")
        assert "ERROR" in combined
        assert "Traceback" not in combined


# ---------------------------------------------------------------------------
# 12. Recursive @file (path field contains another @file expression)
# ---------------------------------------------------------------------------

class TestRecursiveAtFile:
    """payload's 'path' field set to '@other_file' — does the parser recurse?

    _at_file_to_parts() coerces all values via str(). The 'path' value becomes
    the literal string "@other.json" and is passed directly to op_edit as the
    file path. Python's open("@other.json") then fails with file-not-found.

    There is NO recursive @file expansion — the '@' in a field value is inert.
    Severity: LOW — not a vulnerability; just documenting that recursion is absent.
    """

    def test_at_ref_in_path_field_is_literal_not_recursive(self, tmp_path: Path) -> None:
        other = tmp_path / "other.json"
        other.write_text(json.dumps({"path": str(tmp_path / "real.txt")}))

        spec = _write_json_file(tmp_path, "e.json", {
            "old": "x",
            "new": "y",
            "path": f"@{other}",  # looks like an @file ref
        })
        out = _dispatch_reset(f"edit:@{spec}")
        # 'path' value is treated as a literal filename starting with '@'.
        # File "@/path/to/other.json" doesn't exist → clean error.
        assert "ERROR" in out
        assert "Traceback" not in out

    def test_at_dash_in_path_field_does_not_read_stdin(self, tmp_path: Path) -> None:
        """path: '@-' is treated as a literal filename, not a stdin redirect."""
        spec = _write_json_file(tmp_path, "e.json", {
            "old": "x",
            "new": "y",
            "path": "@-",  # literal, not stdin
        })
        # If this tried to read stdin it could hang; it must not.
        out = _dispatch_reset(f"edit:@{spec}")
        assert "ERROR" in out  # file named "@-" doesn't exist
        assert "Traceback" not in out
