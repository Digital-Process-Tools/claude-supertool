from __future__ import annotations

import os
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# dispatch — argument parsing
# ---------------------------------------------------------------------------

def test_dispatch_read_basic(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("hi\n")
    out = supertool.dispatch(f"read:{f}")
    assert out.startswith(f"--- read:{f} ---\n")
    assert "     1→hi" in out


def test_dispatch_read_with_offset_limit(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\n")
    out = supertool.dispatch(f"read:{f}:1:2")
    assert "     2→b" in out
    assert "     3→c" in out
    assert "     1→a" not in out


def test_dispatch_grep(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\nbar\n")
    out = supertool.dispatch(f"grep:foo:{f}")
    assert "--- grep:" in out
    assert str(f) + "\n" in out
    assert "  1:foo" in out


def test_dispatch_unknown_op() -> None:
    out = supertool.dispatch("wat:something")
    assert "ERROR: unknown operation: wat" in out


def test_dispatch_ls(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("")
    out = supertool.dispatch(f"ls:{tmp_path}")
    assert "a.txt" in out


def test_dispatch_tail(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    out = supertool.dispatch(f"tail:{f}:2")
    assert "     2→b" in out
    assert "     3→c" in out


def test_dispatch_head(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    out = supertool.dispatch(f"head:{f}:2")
    assert "     1→a" in out
    assert "     2→b" in out


def test_dispatch_wc(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("one two\nthree\n")
    out = supertool.dispatch(f"wc:{f}")
    assert "--- wc:" in out
    assert "2 " in out


def test_dispatch_grep_count(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("foo\nbar\nfoo\n")
    out = supertool.dispatch(f"grep:foo:{f}:10:0:count")
    assert "2 total" in out


def test_dispatch_grep_with_context(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("before\nMATCH\nafter\n")
    out = supertool.dispatch(f"grep:MATCH:{f}:10:1")
    assert str(f) + "\n" in out
    assert "  2:MATCH" in out
    assert "  1-before" in out
    assert "  3-after" in out


def test_dispatch_around(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("a\nMATCH\nb\n")
    out = supertool.dispatch(f"around:MATCH:{f}:1")
    assert "--- around:" in out
    assert "MATCH" in out


def test_dispatch_around_default_n(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("a\nMATCH\nb\n")
    out = supertool.dispatch(f"around:MATCH:{f}")
    assert "MATCH" in out
    assert "ERROR" not in out


def test_dispatch_check(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "test.txt"
    f.write_text("content")
    config = tmp_path / ".supertool-checks.json"
    config.write_text('{"lint": "cat {file}"}')
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch(f"check:lint:{f}")
    assert "--- check:" in out
    assert "PASS" in out


def test_dispatch_read_grep_filter(tmp_path: Path) -> None:
    f = tmp_path / "test.php"
    f.write_text("alpha\nbeta\nalpha2\ngamma\n")
    out = supertool.dispatch(f"read:{f}:::grep=alpha")
    assert "alpha" in out
    assert "beta" not in out
    assert "gamma" not in out


def test_dispatch_read_grep_filter_empty_offset_limit(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("aaa\nbbb\nccc\n")
    out = supertool.dispatch(f"read:{f}:::grep=bbb")
    assert "bbb" in out
    assert "aaa" not in out


# ---------------------------------------------------------------------------
# _split_arg — Windows drive-letter handling
# ---------------------------------------------------------------------------

def test_split_arg_unix_path() -> None:
    assert supertool._split_arg("read:foo.py") == ["read", "foo.py"]


def test_split_arg_unix_with_offset_limit() -> None:
    assert supertool._split_arg("read:foo.py:10:20") == [
        "read", "foo.py", "10", "20"
    ]


def test_split_arg_windows_backslash_drive() -> None:
    result = supertool._split_arg("read:C:\\Users\\file.py")
    assert result == ["read", "C:\\Users\\file.py"]


def test_split_arg_windows_forward_slash_drive() -> None:
    result = supertool._split_arg("read:D:/src/app.py")
    assert result == ["read", "D:/src/app.py"]


def test_split_arg_windows_drive_with_offset() -> None:
    # read:C:/path:10:20 should be read, C:/path, 10, 20
    result = supertool._split_arg("read:C:/src/foo.py:10:20")
    assert result[0] == "read"
    assert result[1] == "C:/src/foo.py"


def test_split_arg_grep_with_windows_path() -> None:
    # grep:PATTERN:PATH — path has drive letter
    result = supertool._split_arg("grep:needle:C:\\src")
    assert result == ["grep", "needle", "C:\\src"]


def test_split_arg_glob_with_windows_path() -> None:
    result = supertool._split_arg("glob:C:/src/**/*.py")
    assert result == ["glob", "C:/src/**/*.py"]


def test_split_arg_normal_colon_in_pattern_not_confused() -> None:
    # 'http://' style things in grep patterns — first segment is 'grep', so
    # we don't merge. But a lone http in the path slot... let's just check
    # the common case doesn't break.
    result = supertool._split_arg("grep:TODO:src/")
    assert result == ["grep", "TODO", "src/"]


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------

def test_main_batches_multiple_ops(tmp_path: Path, capsys, monkeypatch) -> None:
    f1 = tmp_path / "a.py"
    f1.write_text("first\n")
    f2 = tmp_path / "b.py"
    f2.write_text("second\n")
    # Redirect log to tmp so we don't clobber /tmp/supertool-calls.log
    log_file = tmp_path / "calls.log"
    monkeypatch.setattr(supertool, "LOG_FILE", str(log_file))

    ret = supertool.main([f"read:{f1}", f"read:{f2}"])
    captured = capsys.readouterr()
    assert ret == 0
    assert "first" in captured.out
    assert "second" in captured.out
    # Two header lines: '--- read:... ---'
    header_lines = [ln for ln in captured.out.split("\n") if ln.startswith("--- ")]
    assert len(header_lines) == 2
    # Log should have one line with both ops
    log_content = log_file.read_text()
    assert str(f1) in log_content
    assert str(f2) in log_content


def test_main_no_args_prints_usage(capsys) -> None:
    ret = supertool.main([])
    captured = capsys.readouterr()
    assert ret == 1
    assert "Usage:" in captured.err


def test_main_logs_call(tmp_path: Path, monkeypatch) -> None:
    log_file = tmp_path / "calls.log"
    monkeypatch.setattr(supertool, "LOG_FILE", str(log_file))
    f = tmp_path / "x.py"
    f.write_text("hi\n")
    supertool.main([f"read:{f}"])
    log_content = log_file.read_text()
    assert "read:" in log_content
    # Format: "timestamp | caller_tag | meta | ops"
    parts = log_content.strip().split(" | ")
    assert len(parts) == 4
    # Timestamp: YYYY-MM-DD HH:MM:SS
    assert len(parts[0]) == 19
    # Caller tag fields
    assert "user=" in parts[1]
    assert "ppid=" in parts[1]
    assert "entry=" in parts[1]
    # Meta fields: ops count + output bytes
    assert "ops=1" in parts[2]
    assert "out=" in parts[2]
    assert "b" in parts[2]


def test_main_logs_batch_op_count(tmp_path: Path, monkeypatch) -> None:
    log_file = tmp_path / "calls.log"
    monkeypatch.setattr(supertool, "LOG_FILE", str(log_file))
    f1 = tmp_path / "a.py"
    f1.write_text("a\n")
    f2 = tmp_path / "b.py"
    f2.write_text("b\n")
    f3 = tmp_path / "c.py"
    f3.write_text("c\n")
    supertool.main([f"read:{f1}", f"read:{f2}", f"read:{f3}"])
    log_content = log_file.read_text()
    assert "ops=3" in log_content


def test_caller_tag_includes_user_ppid_entry(monkeypatch) -> None:
    monkeypatch.setenv("USER", "testuser")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    tag = supertool.caller_tag()
    assert "user=testuser" in tag
    assert f"ppid={os.getppid()}" in tag
    assert "entry=cli" in tag


def test_caller_tag_defaults_when_env_missing(monkeypatch) -> None:
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    tag = supertool.caller_tag()
    assert "user=?" in tag
    assert "entry=?" in tag


def test_main_logging_silent_on_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    # Point log at a path that can't be written
    monkeypatch.setattr(supertool, "LOG_FILE", "/nonexistent/dir/log")
    f = tmp_path / "x.py"
    f.write_text("hi\n")
    # Should not raise
    ret = supertool.main([f"read:{f}"])
    assert ret == 0


# ---------------------------------------------------------------------------
# LOG_FILE — cross-platform
# ---------------------------------------------------------------------------

def test_log_file_uses_temp_dir() -> None:
    # LOG_FILE should resolve to a real temp directory — not hardcoded to /tmp
    log_dir = os.path.dirname(supertool.LOG_FILE)
    assert os.path.isdir(log_dir), f"LOG_FILE dir {log_dir} does not exist"
    assert supertool.LOG_FILE.endswith("supertool-calls.log")
