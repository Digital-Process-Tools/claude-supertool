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
# _parse_grep_args — handles '::' in patterns (PHP static access, etc.)
# ---------------------------------------------------------------------------

def test_parse_grep_args_simple() -> None:
    parts = ["grep", "TODO", "src/", "10"]
    pattern, path, limit, context, count_only = supertool._parse_grep_args(parts)
    assert pattern == "TODO"
    assert path == "src/"
    assert limit == 10
    assert context == 0
    assert count_only is False


def test_parse_grep_args_double_colon_pattern() -> None:
    """PHP :: in pattern — e.g. TransmissionStatus::STATUS_PENDING"""
    parts = supertool._split_arg(
        "grep:TransmissionStatus::STATUS_PENDING:Dvsi/**/*.php:50:1"
    )
    pattern, path, limit, context, count_only = supertool._parse_grep_args(parts)
    assert pattern == "TransmissionStatus::STATUS_PENDING"
    assert path == "Dvsi/**/*.php"
    assert limit == 50
    assert context == 1
    assert count_only is False


def test_parse_grep_args_double_colon_with_count() -> None:
    parts = supertool._split_arg(
        "grep:Foo::BAR:src/:20:0:count"
    )
    pattern, path, limit, context, count_only = supertool._parse_grep_args(parts)
    assert pattern == "Foo::BAR"
    assert path == "src/"
    assert limit == 20
    assert context == 0
    assert count_only is True


def test_parse_grep_args_no_limit_no_context() -> None:
    parts = ["grep", "pattern", "path/"]
    pattern, path, limit, context, count_only = supertool._parse_grep_args(parts)
    assert pattern == "pattern"
    assert path == "path/"
    assert limit == supertool.MAX_GREP_RESULTS
    assert context == 0


def test_parse_grep_args_pattern_only() -> None:
    parts = ["grep", "TODO"]
    pattern, path, limit, context, count_only = supertool._parse_grep_args(parts)
    assert pattern == "TODO"
    assert path == "."


def test_parse_grep_args_empty() -> None:
    parts = ["grep"]
    pattern, path, limit, context, count_only = supertool._parse_grep_args(parts)
    assert pattern == ""
    assert path == "."


def test_parse_grep_args_pattern_and_path_only() -> None:
    """Two tokens after 'grep' — pattern + path, no trailing ints."""
    parts = ["grep", "TODO", "src/"]
    pattern, path, limit, context, count_only = supertool._parse_grep_args(parts)
    assert pattern == "TODO"
    assert path == "src/"
    assert limit == supertool.MAX_GREP_RESULTS
    assert context == 0


def test_parse_grep_args_triple_colon_pattern() -> None:
    """Edge case: namespace\\Class::METHOD pattern"""
    parts = supertool._split_arg(
        "grep:A::B::C:src/:5"
    )
    pattern, path, limit, context, count_only = supertool._parse_grep_args(parts)
    assert pattern == "A::B::C"
    assert path == "src/"
    assert limit == 5


# ---------------------------------------------------------------------------
# _parse_around_args — handles '::' in patterns
# ---------------------------------------------------------------------------

def test_parse_around_args_simple() -> None:
    parts = ["around", "TODO", "src/file.py", "15"]
    pattern, path, n = supertool._parse_around_args(parts)
    assert pattern == "TODO"
    assert path == "src/file.py"
    assert n == 15


def test_parse_around_args_double_colon() -> None:
    parts = supertool._split_arg("around:Class::METHOD:src/file.php:10")
    pattern, path, n = supertool._parse_around_args(parts)
    assert pattern == "Class::METHOD"
    assert path == "src/file.php"
    assert n == 10


def test_parse_around_args_no_n() -> None:
    parts = ["around", "pattern", "file.py"]
    pattern, path, n = supertool._parse_around_args(parts)
    assert pattern == "pattern"
    assert path == "file.py"
    assert n == 10


def test_parse_around_args_pattern_only() -> None:
    parts = ["around", "TODO"]
    pattern, path, n = supertool._parse_around_args(parts)
    assert pattern == "TODO"
    assert path == ""
    assert n == 10


def test_parse_around_args_empty() -> None:
    parts = ["around"]
    pattern, path, n = supertool._parse_around_args(parts)
    assert pattern == ""
    assert path == ""
    assert n == 10


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


# ---------------------------------------------------------------------------
# Error paths — OSError, invalid config, bad arguments
# ---------------------------------------------------------------------------

def test_dispatch_read_oserror() -> None:
    result = supertool.dispatch("read:/nonexistent/path/that/does/not/exist.py")
    assert "ERROR" in result


def test_dispatch_around_empty_path() -> None:
    result = supertool.dispatch("around:pattern")
    assert "ERROR" in result or "around" in result


def test_dispatch_grep_invalid_regex_fallback(tmp_path: Path) -> None:
    """Invalid regex in read grep filter should fall back to re.escape."""
    f = tmp_path / "test.py"
    f.write_text("foo[bar\nbaz\n")
    # '[bar' is invalid regex — should fall back to literal match
    result = supertool.dispatch(f"read:{f}:::grep=[bar")
    assert "foo[bar" in result


def test_dispatch_wc_oserror() -> None:
    result = supertool.dispatch("wc:/nonexistent/file.txt")
    assert "ERROR" in result


def test_dispatch_ls_oserror() -> None:
    result = supertool.dispatch("ls:/nonexistent/dir/that/does/not/exist/")
    assert "ERROR" in result


def test_dispatch_tail_oserror() -> None:
    result = supertool.dispatch("tail:/nonexistent/file.txt")
    assert "ERROR" in result


def test_dispatch_head_oserror() -> None:
    result = supertool.dispatch("head:/nonexistent/file.txt")
    assert "ERROR" in result


def test_dispatch_glob_branch(tmp_path: Path) -> None:
    """Verify glob dispatch branch is exercised."""
    f = tmp_path / "hello.txt"
    f.write_text("hi\n")
    result = supertool.dispatch(f"glob:{tmp_path}/*.txt")
    assert "hello.txt" in result


def test_dispatch_grep_empty_pattern() -> None:
    result = supertool.dispatch("grep:")
    assert "ERROR" in result


def test_dispatch_invalid_int_argument() -> None:
    """Non-numeric where int expected triggers ValueError handler."""
    result = supertool.dispatch("read:file.py:notanumber")
    assert "ERROR" in result


def test_dispatch_check_unknown_preset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".supertool.json"
    config.write_text('{"ops": {"lint": "php -l {file}"}}')
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    result = supertool.dispatch("check:nonexistent:file.py")
    assert "ERROR" in result or "unknown" in result.lower()
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False


# ---------------------------------------------------------------------------
# Paths with spaces — verify all major ops handle them
# ---------------------------------------------------------------------------

def test_read_file_with_spaces(tmp_path: Path) -> None:
    d = tmp_path / "my project"
    d.mkdir()
    f = d / "hello world.py"
    f.write_text("line one\nline two\n")
    result = supertool.dispatch(f"read:{f}")
    assert "line one" in result
    assert "line two" in result


def test_grep_file_with_spaces(tmp_path: Path) -> None:
    d = tmp_path / "my project"
    d.mkdir()
    f = d / "hello world.py"
    f.write_text("find me here\nnothing\n")
    result = supertool.dispatch(f"grep:find me:{f}")
    assert "find me here" in result


def test_grep_dir_with_spaces(tmp_path: Path) -> None:
    d = tmp_path / "my project"
    d.mkdir()
    f = d / "code.py"
    f.write_text("TODO fix this\n")
    result = supertool.dispatch(f"grep:TODO:{d}")
    assert "TODO fix this" in result


def test_glob_with_spaces(tmp_path: Path) -> None:
    d = tmp_path / "my project"
    d.mkdir()
    f = d / "hello world.py"
    f.write_text("content\n")
    result = supertool.dispatch(f"glob:{d}/*.py")
    assert "hello world.py" in result


def test_ls_dir_with_spaces(tmp_path: Path) -> None:
    d = tmp_path / "my project"
    d.mkdir()
    (d / "file one.txt").write_text("a\n")
    (d / "file two.txt").write_text("b\n")
    result = supertool.dispatch(f"ls:{d}")
    assert "file one.txt" in result
    assert "file two.txt" in result


def test_wc_file_with_spaces(tmp_path: Path) -> None:
    d = tmp_path / "my project"
    d.mkdir()
    f = d / "hello world.py"
    f.write_text("line one\nline two\nline three\n")
    result = supertool.dispatch(f"wc:{f}")
    assert "3" in result  # 3 lines


def test_tail_file_with_spaces(tmp_path: Path) -> None:
    d = tmp_path / "my project"
    d.mkdir()
    f = d / "hello world.py"
    f.write_text("first\nsecond\nthird\n")
    result = supertool.dispatch(f"tail:{f}:2")
    assert "third" in result


def test_head_file_with_spaces(tmp_path: Path) -> None:
    d = tmp_path / "my project"
    d.mkdir()
    f = d / "hello world.py"
    f.write_text("first\nsecond\nthird\n")
    result = supertool.dispatch(f"head:{f}:2")
    assert "first" in result


def test_around_file_with_spaces(tmp_path: Path) -> None:
    d = tmp_path / "my project"
    d.mkdir()
    f = d / "hello world.py"
    f.write_text("before\ntarget line\nafter\n")
    result = supertool.dispatch(f"around:target:{f}:1")
    assert "target line" in result
    assert "before" in result
    assert "after" in result


def test_map_file_with_spaces(tmp_path: Path) -> None:
    d = tmp_path / "my project"
    d.mkdir()
    f = d / "hello world.py"
    f.write_text("def my_function():\n    pass\n")
    result = supertool.dispatch(f"map:{f}")
    assert "my_function" in result


# ---------------------------------------------------------------------------
# Error paths — invalid config, bad arguments
# ---------------------------------------------------------------------------

def test_dispatch_custom_op_invalid_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".supertool.json"
    config.write_text('{"ops": {"broken": 42}}')
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    result = supertool.dispatch("broken:file.py")
    assert "ERROR" in result or "invalid" in result.lower()
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False


def test_dispatch_alias_invalid_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".supertool.json"
    config.write_text('{"aliases": {"bad": "not-a-list"}}')
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    result = supertool.dispatch("bad:file.py")
    assert "ERROR" in result or "must be a list" in result.lower()
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
