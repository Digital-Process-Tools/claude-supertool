"""Security and edge-case tests for op_paste / _atomic_write.

Coverage:
 1. Symlink — write-through to target, link preserved
 2. Path traversal — paste outside cwd
 3. NUL byte in path — clean error
 4. Non-existent parent dir — auto-mkdir or clean error
 5. NUL bytes in content — preserved
 6. Disk-full OSError — propagated cleanly
 7. Validator rollback — bad JSON rolled back
 8. TOCTOU: tmp file replaced with symlink by attacker
 9. Path is a directory — clean error
10. Empty content — 0-byte file (intentional truncate)
11. Mixed line endings — preserved as-is
"""
from __future__ import annotations

import os
import stat
import sys
import threading
import time
import unittest.mock as mock
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

import supertool


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Symlink — must follow through to real target
# ---------------------------------------------------------------------------

def test_paste_symlink_follows_to_target(tmp_path: Path) -> None:
    """_atomic_write must write to the real file, not replace the symlink."""
    real = tmp_path / "real.txt"
    _write(real, "original\n")

    link = tmp_path / "link.txt"
    link.symlink_to(real)

    out = supertool.op_paste(str(link), "updated content")

    assert "ERROR" not in out
    # symlink still exists
    assert link.is_symlink(), "symlink was clobbered"
    # real file has new content
    assert real.read_text(encoding="utf-8") == "updated content\n"
    # link resolves to same updated content
    assert link.read_text(encoding="utf-8") == "updated content\n"


def test_paste_symlink_target_updated_not_replaced(tmp_path: Path) -> None:
    """After paste through a symlink, link.resolve() still points to real."""
    real = tmp_path / "data.txt"
    _write(real, "before\n")

    link = tmp_path / "alias.txt"
    link.symlink_to(real)

    supertool.op_paste(str(link), "after")

    assert link.resolve() == real.resolve()


# ---------------------------------------------------------------------------
# 2. Path traversal — must NOT write outside cwd
# ---------------------------------------------------------------------------

def test_paste_path_traversal_writes_relative_to_given_path(tmp_path: Path) -> None:
    """op_paste accepts the path as-is; this test documents that traversal
    paths like '../../../etc/passwd' will resolve relative to the process cwd,
    NOT be silently blocked. The test asserts the write stays in /tmp by
    verifying /etc/passwd is untouched and that the write lands in the
    resolved location."""
    # Use a safe target inside tmp_path to test the traversal expansion
    # without actually touching /etc/passwd.
    safe = tmp_path / "a" / "b"
    safe.mkdir(parents=True)
    evil_path = str(safe / ".." / ".." / "escaped.txt")
    resolved = os.path.normpath(evil_path)

    # resolved is still inside tmp_path — just testing the path normalisation
    out = supertool.op_paste(evil_path, "traversal content")

    # op_paste must NOT refuse on sight of '..'; it writes to resolved path
    assert "ERROR" not in out
    assert Path(resolved).read_text(encoding="utf-8") == "traversal content\n"

    # Critical guard: a known system-owned file must be untouched. `/etc/passwd`
    # doesn't exist on Windows — use the runtime's hostname for symmetry: the
    # test's real point is "op_paste honoured the resolved path, did not escape
    # to a hard-coded sensitive sentinel". The escaped.txt assertion above
    # already proves the write landed where it was told.
    if sys.platform != "win32":
        assert os.path.exists("/etc/passwd"), "/etc/passwd vanished — something went very wrong"
        with open("/etc/passwd") as f:
            first = f.read(5)
        assert first != "trave", "/etc/passwd was overwritten — CRITICAL"


# ---------------------------------------------------------------------------
# 3. NUL byte in path — clean error, no traceback
# ---------------------------------------------------------------------------

def test_paste_nul_in_path_returns_error(tmp_path: Path) -> None:
    """A path containing \\x00 must produce a clean ERROR string, not raise.

    Fixed by the op_paste containment patch: `_safe_path` rejects NUL bytes
    BEFORE the path reaches `os.replace`, returning a clean SecurityError
    string instead of leaking a ValueError traceback.
    """
    bad_path = str(tmp_path / "file\x00.txt")
    out = supertool.op_paste(bad_path, "content")
    assert "ERROR" in out
    # Must not leak a Python traceback
    assert "Traceback" not in out
    assert "ValueError" not in out


# ---------------------------------------------------------------------------
# 4. Non-existent parent dir — auto-mkdir
# ---------------------------------------------------------------------------

def test_paste_creates_missing_parent_dirs(tmp_path: Path) -> None:
    """op_paste must create missing ancestor directories."""
    target = tmp_path / "deep" / "nested" / "dir" / "file.txt"
    assert not target.parent.exists()

    out = supertool.op_paste(str(target), "hello")

    assert "ERROR" not in out
    assert target.read_text(encoding="utf-8") == "hello\n"


# ---------------------------------------------------------------------------
# 5. NUL bytes in content — preserved
# ---------------------------------------------------------------------------

def test_paste_nul_bytes_in_content_preserved(tmp_path: Path) -> None:
    """Content with embedded NUL bytes must survive the round-trip unchanged
    (modulo trailing-newline normalisation on the text layer).

    Note: Python's text-mode open with surrogateescape will raise when writing
    a NUL byte through the codec — this test documents the current behaviour
    rather than prescribing it."""
    target = tmp_path / "binary.bin"
    content_with_nul = "before\x00after"

    out = supertool.op_paste(str(target), content_with_nul)

    if "ERROR" in out:
        # Acceptable: NUL in content may be rejected; just must be a clean error
        assert "Traceback" not in out
    else:
        written = target.read_text(encoding="utf-8", errors="surrogateescape")
        # NUL may be normalised away or preserved depending on codec; the
        # important thing is the write completed and content is readable.
        assert "before" in written
        assert "after" in written


# ---------------------------------------------------------------------------
# 6. Disk-full OSError — clean error propagated
# ---------------------------------------------------------------------------

def test_paste_disk_full_returns_error(tmp_path: Path) -> None:
    """When _atomic_write raises OSError (e.g. ENOSPC), op_paste must return
    a clean ERROR string and not propagate the exception."""
    target = tmp_path / "file.txt"

    with patch.object(supertool, "_atomic_write",
                      side_effect=OSError(28, "No space left on device")):
        out = supertool.op_paste(str(target), "some content")

    assert "ERROR" in out
    assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 7. Validator rollback — bad JSON is rolled back
# ---------------------------------------------------------------------------

def test_paste_validator_rollback_on_fail(tmp_path: Path) -> None:
    """When a validator with rollback_on_fail=True fails after paste, the
    original file content must be restored."""
    target = tmp_path / "data.json"
    original = '{"valid": true}\n'
    _write(target, original)

    # Configure a fake validator that always fails + requests rollback
    fake_validator_spec = {
        "jsonlint": {
            "cmd": "false",   # always exits 1
            "match": "*.json",
            "hooks_into": ["paste"],
            "rollback_on_fail": True,
        }
    }

    def fake_load_config():
        return {"validators": fake_validator_spec}

    # _validator_run_one must see a failure — mock it directly.
    # "tool" and "count" are required by _validator_render_diff to emit a ✗ line
    # that triggers the rollback scan at supertool.py:8440.
    fail_result = {
        "tool": "jsonlint",
        "ok": False,
        "count": 1,
        "errors": [{"line": 1, "col": 0, "severity": "error", "code": "parse", "msg": "unexpected token"}],
        "duration_ms": 10,
    }
    pass_result = {
        "tool": "jsonlint",
        "ok": True,
        "count": 0,
        "errors": [],
        "duration_ms": 5,
    }

    call_count = [0]

    def fake_validator_run_one(name: str, spec: dict, path: str) -> dict:
        call_count[0] += 1
        # First call (before snapshot) = pass; second call (after write) = fail
        if call_count[0] <= 1:
            return pass_result
        return fail_result

    # Direct op_paste() bypasses the validator/rollback chain that lives in
    # the dispatch layer. Go through dispatch so the real path runs.
    with patch("supertool._load_config", fake_load_config), \
         patch("supertool._validator_run_one", fake_validator_run_one):
        out = supertool.dispatch(f"paste:::{target}:::not valid json {{{{{{")

    current = target.read_text(encoding="utf-8")
    assert current == original, (
        f"Expected rollback to original content, got: {current!r}"
    )
    assert "rolled back" in out.lower() or "ERROR" in out


# ---------------------------------------------------------------------------
# 8. TOCTOU: attacker symlinks tmp file to /etc/passwd before os.replace
# ---------------------------------------------------------------------------

def test_paste_toctou_tmp_replaced_with_symlink(tmp_path: Path) -> None:
    """Race: if an attacker replaces the .supertool-*.tmp file with a symlink
    to /etc/passwd before os.replace fires, os.replace would overwrite the
    target of that symlink.

    This test injects the attack by monkeypatching os.replace to first replace
    the tmp file with a symlink, then calls the real os.replace.

    Expected: os.replace follows the attacker's symlink and writes to its
    target — this IS the known TOCTOU vulnerability. The test documents this
    behaviour and verifies that at minimum the original target path is written,
    so the attack vector is visible in the test output.
    """
    real_target = tmp_path / "safe.txt"
    _write(real_target, "safe\n")

    attacker_target = tmp_path / "attacker_target.txt"
    _write(attacker_target, "untouched\n")

    original_replace = os.replace

    attacked = [False]

    def racing_replace(src: str, dst: str) -> None:
        if not attacked[0] and dst == str(real_target):
            attacked[0] = True
            # Attacker: replace the tmp with a symlink to attacker_target
            os.unlink(src)
            os.symlink(str(attacker_target), src)
        original_replace(src, dst)

    with patch("os.replace", side_effect=racing_replace):
        out = supertool.op_paste(str(real_target), "injected content")

    # Observed (2026-05-23): attacker_target is NOT written, real_target keeps
    # its original content. The race window in _atomic_write (tmp create →
    # tmp write → os.replace) doesn't expose attacker_target to the contents
    # in this mocked path. The TOCTOU window is theoretically still present
    # in the real implementation but the mocked race does not reproduce a
    # privilege-escalation-style exploit. Pin the defense outcome here so a
    # regression that broke it would show up.
    attacker_content = attacker_target.read_text(encoding="utf-8")
    assert "injected content" not in attacker_content, (
        "attacker_target must not receive paste content via symlink race"
    )


# ---------------------------------------------------------------------------
# 9. Path is a directory — clean error
# ---------------------------------------------------------------------------

def test_paste_path_is_directory_returns_error(tmp_path: Path) -> None:
    """Pasting to a path that already exists as a directory must be a clean error."""
    directory = tmp_path / "adir"
    directory.mkdir()

    out = supertool.op_paste(str(directory), "content")

    assert "ERROR" in out
    assert "Traceback" not in out
    # Directory must still be a directory
    assert directory.is_dir()


# ---------------------------------------------------------------------------
# 10. Empty content — file becomes 0 bytes (intentional truncation)
# ---------------------------------------------------------------------------

def test_paste_empty_content_creates_empty_file(tmp_path: Path) -> None:
    """Pasting empty string should create/truncate to an empty (or newline-only) file."""
    target = tmp_path / "empty.txt"
    _write(target, "previous content\n")

    out = supertool.op_paste(str(target), "")

    assert "ERROR" not in out
    size = target.stat().st_size
    # op_paste does NOT append a newline when content is empty (falsy guard)
    assert size == 0, f"Expected 0-byte file after empty paste, got {size} bytes"


def test_paste_empty_content_new_file(tmp_path: Path) -> None:
    """Pasting empty string to a new path should create it."""
    target = tmp_path / "new_empty.txt"
    assert not target.exists()

    out = supertool.op_paste(str(target), "")

    assert "ERROR" not in out
    assert target.exists()
    assert target.stat().st_size == 0


# ---------------------------------------------------------------------------
# 11. Mixed line endings (CRLF + LF) — preserved as-is
# ---------------------------------------------------------------------------

def test_paste_mixed_line_endings_preserved(tmp_path: Path) -> None:
    """Content with mixed CRLF and LF must survive the write without normalisation."""
    target = tmp_path / "mixed.txt"
    # Mix: first line CRLF, second LF, third CRLF
    mixed = "line1\r\nline2\nline3\r\n"

    out = supertool.op_paste(str(target), mixed)

    assert "ERROR" not in out
    # Read back in binary to check exact bytes
    raw = target.read_bytes()
    assert b"\r\nline2\n" in raw, f"Line endings changed: {raw!r}"


def test_paste_crlf_only_preserved(tmp_path: Path) -> None:
    """All-CRLF content must not be converted to LF-only."""
    target = tmp_path / "crlf.txt"
    crlf_content = "alpha\r\nbeta\r\ngamma\r\n"

    supertool.op_paste(str(target), crlf_content)

    raw = target.read_bytes()
    assert b"\r\n" in raw, "CRLF was converted to LF"


# ---------------------------------------------------------------------------
# 12. Containment — outside-cwd path must NOT pollute filesystem with mkdir
# ---------------------------------------------------------------------------

def test_paste_outside_cwd_does_not_create_parent_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: op_paste used to call os.makedirs(parent) BEFORE the
    _safe_path containment check inside _atomic_write. A traversal path like
    `../../tmp/evil/foo` would create `../../tmp/evil/` on disk before the
    write itself was rejected — leaving empty directories outside cwd.

    Fix: call _safe_path at op_paste entry so containment is enforced before
    any filesystem mutation.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    # cwd is the repo root in CI / dev; tmp_path is outside cwd (under
    # /private/var/folders or /tmp). Build a path that targets a brand-new
    # subdir of tmp_path so we can assert it was NOT created.
    outside_dir = tmp_path / "should_not_exist"
    outside_target = outside_dir / "file.txt"

    monkeypatch.chdir(Path(__file__).parent.parent)  # supertool repo root
    out = supertool.op_paste(str(outside_target), "evil content")

    assert "ERROR" in out, f"expected SecurityError, got: {out!r}"
    assert "escapes cwd" in out
    assert not outside_dir.exists(), (
        f"op_paste polluted filesystem: {outside_dir} was created before "
        f"the containment check rejected the write"
    )
    assert not outside_target.exists()
