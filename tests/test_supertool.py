"""Test suite for supertool.py.

Hermetic: uses pytest tmp_path for all file I/O. No dependency on /tmp, the
real log file, or the repo layout.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Add repo root to path so we can import supertool directly
sys.path.insert(0, str(Path(__file__).parent.parent))

import supertool  # noqa: E402


# ---------------------------------------------------------------------------
# op_read
# ---------------------------------------------------------------------------

def test_read_returns_line_numbered_content(tmp_path: Path) -> None:
    f = tmp_path / "hello.py"
    f.write_text("line1\nline2\nline3\n")
    out = supertool.op_read(str(f))
    assert "(3 lines, 18 bytes)" in out
    assert "     1→line1" in out
    assert "     3→line3" in out


def test_read_missing_file_returns_error(tmp_path: Path) -> None:
    out = supertool.op_read(str(tmp_path / "nope.py"))
    assert "ERROR: file not found" in out


def test_read_empty_path_returns_error() -> None:
    out = supertool.op_read("")
    assert "ERROR: file not found" in out


def test_read_complete_file_marker(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("x = 1\n")
    out = supertool.op_read(str(f))
    assert "[complete file — no more lines]" in out


def test_read_no_complete_marker_when_truncated(tmp_path: Path) -> None:
    f = tmp_path / "many.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = supertool.op_read(str(f), offset=0, limit=3)
    assert "[complete file" not in out
    assert "more lines" in out


def test_read_with_offset_and_limit(tmp_path: Path) -> None:
    f = tmp_path / "many.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = supertool.op_read(str(f), offset=3, limit=2)
    assert "     4→line4" in out
    assert "     5→line5" in out
    assert "line3" not in out
    assert "line6" not in out


def test_read_truncates_at_byte_cap(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    # Write more than 20KB of content: 500 lines × ~100 chars = ~50KB
    f.write_text(("x" * 100 + "\n") * 500)
    out = supertool.op_read(str(f))
    assert "truncated at" in out
    assert "20000 bytes" in out


def test_read_reports_more_lines_available(tmp_path: Path) -> None:
    f = tmp_path / "long.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 51)) + "\n")
    out = supertool.op_read(str(f), offset=0, limit=10)
    assert "more lines" in out


def test_read_directory_returns_error(tmp_path: Path) -> None:
    out = supertool.op_read(str(tmp_path))
    assert "ERROR: file not found" in out


# ---------------------------------------------------------------------------
# op_grep
# ---------------------------------------------------------------------------

def test_grep_finds_match_in_single_file(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("class Foo:\n    pass\n\nclass Bar:\n    pass\n")
    out = supertool.op_grep("class", str(f))
    assert "(2 results" in out
    assert "src.py:1:class Foo:" in out
    assert "src.py:4:class Bar:" in out


def test_grep_no_match_returns_zero(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("class Foo:\n")
    out = supertool.op_grep("NOTHINGMATCHES_XYZZY", str(f))
    assert "(0 results" in out


def test_grep_empty_pattern_errors() -> None:
    out = supertool.op_grep("", "/tmp")
    assert "ERROR: empty pattern" in out


def test_grep_respects_limit(tmp_path: Path) -> None:
    f = tmp_path / "many.py"
    content = "\n".join(f"match line {i}" for i in range(1, 20)) + "\n"
    f.write_text(content)
    out = supertool.op_grep("match", str(f), limit=3)
    assert "limit 3" in out
    # Count actual result lines (path:lineno:content format)
    result_lines = [ln for ln in out.split("\n") if ":" in ln and "match line" in ln]
    assert len(result_lines) == 3


def test_grep_on_directory_filters_by_extension(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("needle = 1\n")
    (tmp_path / "doc.md").write_text("needle in docs\n")
    (tmp_path / "log.log").write_text("needle in log\n")  # should be skipped
    out = supertool.op_grep("needle", str(tmp_path), limit=10)
    assert "code.py" in out
    assert "doc.md" in out
    assert "log.log" not in out


# --- Auto-read on grep (small single file + match) ---

def test_grep_auto_reads_small_single_file_on_match(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("found_it = True\n")
    out = supertool.op_grep("found_it", str(f))
    assert "[auto-read:" in out
    assert "(1 lines" in out  # The render_file output
    assert "     1→found_it = True" in out


def test_grep_no_auto_read_when_no_match(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("nothing_here = True\n")
    out = supertool.op_grep("XXX_NO_MATCH", str(f))
    assert "[auto-read:" not in out


def test_grep_no_auto_read_on_directory(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("foo = 1\n")
    out = supertool.op_grep("foo", str(tmp_path))
    # Matched in file a.py, but path is the directory
    assert "[auto-read:" not in out


def test_grep_no_auto_read_on_large_file(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "big.py"
    f.write_text("needle\n" + "x" * 30000)
    out = supertool.op_grep("needle", str(f))
    assert "[auto-read:" not in out


# ---------------------------------------------------------------------------
# op_glob
# ---------------------------------------------------------------------------

def test_glob_finds_wildcards(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("*.py")
    assert "(2 files)" in out
    assert "a.py" in out
    assert "b.py" in out
    assert "c.txt" not in out


def test_glob_recursive_double_star(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.py").write_text("")
    (tmp_path / "top.py").write_text("")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("**/*.py")
    assert "top.py" in out
    assert os.path.join("sub", "deep.py") in out


def test_glob_empty_pattern_errors() -> None:
    out = supertool.op_glob("")
    assert "ERROR: empty pattern" in out


def test_glob_no_match(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("*.nothinglikethis")
    assert "(0 files)" in out


# --- Auto-read on glob (concrete path, no wildcards, is a file) ---

def test_glob_auto_reads_concrete_file(tmp_path: Path) -> None:
    f = tmp_path / "specific.py"
    f.write_text("content = 42\n")
    out = supertool.op_glob(str(f))
    assert "[auto-read: concrete path, no wildcards]" in out
    assert "     1→content = 42" in out


def test_glob_auto_read_when_single_result(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("*.py")
    assert "[auto-read: glob returned 1 file]" in out
    assert "x = 1" in out


def test_glob_no_auto_read_when_multiple_results(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("*.py")
    assert "[auto-read:" not in out


def test_glob_no_auto_read_on_concrete_directory(tmp_path: Path) -> None:
    # Directory path with no wildcards — glob() returns the dir name, but
    # we should NOT auto-read (it's not a file).
    out = supertool.op_glob(str(tmp_path))
    assert "[auto-read:" not in out


def test_glob_no_auto_read_on_missing_concrete_file(tmp_path: Path) -> None:
    out = supertool.op_glob(str(tmp_path / "nope.py"))
    assert "[auto-read:" not in out
    assert "(0 files)" in out


def test_glob_question_mark_is_wildcard(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("?.py")
    # Question-mark means single-char wildcard — should NOT auto-read even
    # if it could match one file.
    assert "[auto-read:" not in out
    assert "a.py" in out


# ---------------------------------------------------------------------------
# op_ls
# ---------------------------------------------------------------------------

def test_ls_lists_dir_contents(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("")
    (tmp_path / "sub").mkdir()
    out = supertool.op_ls(str(tmp_path))
    assert "file.txt" in out
    assert "sub/" in out  # trailing slash for dirs


def test_ls_non_existent_path(tmp_path: Path) -> None:
    out = supertool.op_ls(str(tmp_path / "nope"))
    assert "ERROR: not a directory" in out


def test_ls_file_not_dir(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("")
    out = supertool.op_ls(str(f))
    assert "ERROR: not a directory" in out


def test_ls_empty_dir(tmp_path: Path) -> None:
    out = supertool.op_ls(str(tmp_path))
    assert "(0 items)" in out


# ---------------------------------------------------------------------------
# op_tail / op_head
# ---------------------------------------------------------------------------

def test_tail_returns_last_n_lines(tmp_path: Path) -> None:
    f = tmp_path / "log.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = supertool.op_tail(str(f), 3)
    assert "     8→line8" in out
    assert "     9→line9" in out
    assert "    10→line10" in out
    assert "line7" not in out


def test_head_returns_first_n_lines(tmp_path: Path) -> None:
    f = tmp_path / "log.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = supertool.op_head(str(f), 3)
    assert "     1→line1" in out
    assert "     2→line2" in out
    assert "     3→line3" in out
    assert "line4" not in out


def test_tail_missing_file(tmp_path: Path) -> None:
    out = supertool.op_tail(str(tmp_path / "nope.txt"), 5)
    assert "ERROR: file not found" in out


def test_head_missing_file(tmp_path: Path) -> None:
    out = supertool.op_head(str(tmp_path / "nope.txt"), 5)
    assert "ERROR: file not found" in out


def test_tail_file_shorter_than_n(tmp_path: Path) -> None:
    f = tmp_path / "short.txt"
    f.write_text("only\none\n")
    out = supertool.op_tail(str(f), 100)
    assert "     1→only" in out
    assert "     2→one" in out


def test_head_file_shorter_than_n(tmp_path: Path) -> None:
    f = tmp_path / "short.txt"
    f.write_text("only\none\n")
    out = supertool.op_head(str(f), 100)
    assert "showing first 2" in out


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
    assert f"{f}:1:foo" in out


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
# render_file edge cases (shared helper)
# ---------------------------------------------------------------------------

def test_render_file_handles_binary_gracefully(tmp_path: Path) -> None:
    f = tmp_path / "bin.dat"
    f.write_bytes(b"\x00\x01\x02\xff\n")
    out = supertool.render_file(str(f))
    # Should not raise, should emit something
    assert "     1→" in out


def test_render_file_handles_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "empty.txt"
    f.write_text("")
    out = supertool.render_file(str(f))
    assert "(0 lines, 0 bytes)" in out


# ---------------------------------------------------------------------------
# pre_tool_hook — enforcement logic
# ---------------------------------------------------------------------------

def test_pre_tool_hook_permissive_allows_everything() -> None:
    # Even with blocked tools, permissive mode returns 0
    code, msg = supertool.pre_tool_hook({"tool_name": "Grep"}, enforced=False)
    assert code == 0
    assert msg == ""


def test_pre_tool_hook_enforced_blocks_grep() -> None:
    code, msg = supertool.pre_tool_hook({"tool_name": "Grep"}, enforced=True)
    assert code == 2
    assert "Use ./supertool instead of Grep" in msg


def test_pre_tool_hook_enforced_blocks_glob() -> None:
    code, msg = supertool.pre_tool_hook({"tool_name": "Glob"}, enforced=True)
    assert code == 2
    assert "Use ./supertool instead of Glob" in msg


def test_pre_tool_hook_enforced_blocks_ls() -> None:
    code, msg = supertool.pre_tool_hook({"tool_name": "LS"}, enforced=True)
    assert code == 2
    assert "LS" in msg


def test_pre_tool_hook_enforced_allows_read() -> None:
    # Read must stay allowed — Edit requires it
    code, msg = supertool.pre_tool_hook({"tool_name": "Read"}, enforced=True)
    assert code == 0
    assert msg == ""


def test_pre_tool_hook_bash_blocks_cat() -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "cat /etc/hosts"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 2
    assert "Bash(cat ...)" in msg


def test_pre_tool_hook_bash_blocks_find() -> None:
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "find . -name '*.py'"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 2
    assert "find" in msg


def test_pre_tool_hook_bash_blocks_grep() -> None:
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "grep -rn needle src/"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 2


def test_pre_tool_hook_bash_blocks_sed() -> None:
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "sed -n '1,10p' file.txt"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 2


def test_pre_tool_hook_bash_allows_git() -> None:
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "git status"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 0


def test_pre_tool_hook_bash_allows_python() -> None:
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "python3 script.py"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 0


def test_pre_tool_hook_bash_allows_supertool() -> None:
    # The whole point — supertool itself must not be blocked
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "./supertool read:foo.py"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 0


def test_pre_tool_hook_bash_empty_command_allowed() -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": ""}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 0


def test_pre_tool_hook_missing_tool_name_allowed() -> None:
    code, msg = supertool.pre_tool_hook({}, enforced=True)
    assert code == 0


def test_pre_tool_hook_bash_missing_command_allowed() -> None:
    payload = {"tool_name": "Bash", "tool_input": {}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 0


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
# op_grep with context lines
# ---------------------------------------------------------------------------

def test_grep_context_zero_same_as_no_context(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("class Foo:\n    pass\n\nclass Bar:\n    pass\n")
    out_plain = supertool.op_grep("class", str(f), limit=10, context=0)
    out_ctx = supertool.op_grep("class", str(f), limit=10)
    assert out_plain == out_ctx


def test_grep_context_includes_surrounding_lines(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    # Match at line 5, 2 lines of context → lines 3-7 shown; lines 1-2 and 8-10 excluded
    f.write_text("skip1\nskip2\nctx_before2\nctx_before1\nMATCH\nctx_after1\nctx_after2\nskip3\nskip4\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=2)
    # Match line uses colon separator
    assert f"{f}:5:MATCH" in out
    # Context lines use dash separator
    assert f"{f}-4-ctx_before1" in out
    assert f"{f}-6-ctx_after1" in out
    assert f"{f}-3-ctx_before2" in out
    assert f"{f}-7-ctx_after2" in out
    # Lines beyond context are not included
    assert "skip1" not in out
    assert "skip2" not in out
    assert "skip3" not in out


def test_grep_context_header_shows_context_value(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("MATCH\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=3)
    assert "context 3" in out


def test_grep_context_separator_between_nonadjacent_groups(tmp_path: Path) -> None:
    lines = [f"line{i}" for i in range(1, 21)]
    lines[3] = "MATCH_A"   # line 4
    lines[16] = "MATCH_B"  # line 17
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=1)
    # Groups should be separated by --
    assert "--\n" in out
    assert "MATCH_A" in out
    assert "MATCH_B" in out


def test_grep_context_no_separator_for_adjacent_matches(tmp_path: Path) -> None:
    lines = ["before", "MATCH_A", "MATCH_B", "after"]
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=1)
    # Adjacent matches → merged group → no -- separator
    assert "--\n" not in out
    assert "MATCH_A" in out
    assert "MATCH_B" in out


def test_grep_context_overlapping_windows_merge(tmp_path: Path) -> None:
    # Two matches close enough that context windows overlap
    lines = ["a", "MATCH_A", "b", "MATCH_B", "c"]
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=2)
    # With context=2: window A covers lines 1-3, window B covers lines 2-5
    # They overlap → one group, no --
    assert "--\n" not in out
    assert "MATCH_A" in out
    assert "MATCH_B" in out


def test_grep_context_clamps_to_file_boundaries(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("MATCH\nline2\nline3\n")
    # Match at line 1 with context=5 — should not go negative
    out = supertool.op_grep("MATCH", str(f), limit=10, context=5)
    assert "MATCH" in out
    assert "ERROR" not in out


def test_grep_context_no_auto_read(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("MATCH\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=1)
    # Auto-read should be skipped when context is active
    assert "[auto-read:" not in out


def test_dispatch_grep_with_context(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("before\nMATCH\nafter\n")
    out = supertool.dispatch(f"grep:MATCH:{f}:10:1")
    assert f"{f}:2:MATCH" in out
    assert f"{f}-1-before" in out
    assert f"{f}-3-after" in out


# ---------------------------------------------------------------------------
# op_around
# ---------------------------------------------------------------------------

def test_around_finds_first_match(tmp_path: Path) -> None:
    lines = [f"line{i}" for i in range(1, 21)]
    lines[9] = "TARGET"  # line 10
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_around("TARGET", str(f), n=3)
    assert "match at line 10" in out
    assert "TARGET" in out
    # Should include 3 lines before and after
    assert "line7" in out
    assert "line13" in out
    # Should not include lines too far away
    assert "line6" not in out
    assert "line14" not in out


def test_around_uses_arrow_marker_on_match_line(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("a\nMATCH\nb\n")
    out = supertool.op_around("MATCH", str(f), n=1)
    # Match line gets → marker
    assert "     2→MATCH" in out
    # Context lines get space marker
    assert "     1 a" in out
    assert "     3 b" in out


def test_around_no_match(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("nothing here\n")
    out = supertool.op_around("XYZZY_NOMATCH", str(f))
    assert "no match" in out


def test_around_directory_returns_error(tmp_path: Path) -> None:
    out = supertool.op_around("pattern", str(tmp_path))
    assert "ERROR" in out
    assert "directories" in out or "directory" in out


def test_around_missing_file_returns_error(tmp_path: Path) -> None:
    out = supertool.op_around("pattern", str(tmp_path / "nope.py"))
    assert "ERROR: file not found" in out


def test_around_default_n_is_10(tmp_path: Path) -> None:
    lines = ["pad"] * 15 + ["MATCH"] + ["pad"] * 15
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_around("MATCH", str(f))
    # Default n=10: match at line 16, should show lines 6–26
    assert "showing lines 6" in out


def test_around_first_match_only(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("MATCH\nother\nMATCH\n")
    out = supertool.op_around("MATCH", str(f), n=0)
    # Only first match reported
    assert "match at line 1" in out


def test_around_empty_pattern_errors(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("content\n")
    out = supertool.op_around("", str(f))
    assert "ERROR: empty pattern" in out


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


# ---------------------------------------------------------------------------
# LOG_FILE — cross-platform
# ---------------------------------------------------------------------------

def test_log_file_uses_temp_dir() -> None:
    # LOG_FILE should resolve to a real temp directory — not hardcoded to /tmp
    log_dir = os.path.dirname(supertool.LOG_FILE)
    assert os.path.isdir(log_dir), f"LOG_FILE dir {log_dir} does not exist"
    assert supertool.LOG_FILE.endswith("supertool-calls.log")


# ---------------------------------------------------------------------------
# is_enforced + --pre-tool-hook CLI dispatch
# ---------------------------------------------------------------------------

def test_is_enforced_returns_false_when_state_file_absent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(supertool, "ENFORCE_STATE_FILE", str(tmp_path / "nope"))
    assert supertool.is_enforced() is False


def test_is_enforced_returns_true_when_state_file_present(monkeypatch, tmp_path) -> None:
    state = tmp_path / "enforced"
    state.touch()
    monkeypatch.setattr(supertool, "ENFORCE_STATE_FILE", str(state))
    assert supertool.is_enforced() is True


def test_main_pre_tool_hook_permissive(monkeypatch, tmp_path, capsys) -> None:
    # State file absent → permissive → exit 0 even for blocked tool
    monkeypatch.setattr(supertool, "ENFORCE_STATE_FILE", str(tmp_path / "nope"))
    monkeypatch.setattr("sys.stdin",
                        type("S", (), {"read": staticmethod(
                            lambda: '{"tool_name":"Grep"}')})())
    ret = supertool.main(["--pre-tool-hook"])
    assert ret == 0


def test_main_pre_tool_hook_enforced_blocks(monkeypatch, tmp_path, capsys) -> None:
    state = tmp_path / "enforced"
    state.touch()
    monkeypatch.setattr(supertool, "ENFORCE_STATE_FILE", str(state))
    monkeypatch.setattr("sys.stdin",
                        type("S", (), {"read": staticmethod(
                            lambda: '{"tool_name":"Grep"}')})())
    ret = supertool.main(["--pre-tool-hook"])
    captured = capsys.readouterr()
    assert ret == 2
    assert "Use ./supertool" in captured.err


def test_main_pre_tool_hook_malformed_json_fail_open(monkeypatch, tmp_path) -> None:
    state = tmp_path / "enforced"
    state.touch()
    monkeypatch.setattr(supertool, "ENFORCE_STATE_FILE", str(state))
    monkeypatch.setattr("sys.stdin",
                        type("S", (), {"read": staticmethod(
                            lambda: "not json{{")})())
    # Malformed input → fail-open (exit 0, don't block legit workflows)
    ret = supertool.main(["--pre-tool-hook"])
    assert ret == 0


# ---------------------------------------------------------------------------
# op_wc
# ---------------------------------------------------------------------------

def test_wc_counts(tmp_path: Path) -> None:
    f = tmp_path / "sample.txt"
    f.write_text("hello world\nfoo bar baz\n")
    out = supertool.op_wc(str(f))
    assert "2 " in out  # 2 newlines
    assert " 5 " in out  # 5 words
    assert str(f) in out

def test_wc_missing_file() -> None:
    out = supertool.op_wc("/nonexistent/file.txt")
    assert "ERROR" in out

def test_wc_empty_path() -> None:
    out = supertool.op_wc("")
    assert "ERROR" in out

def test_wc_directory(tmp_path: Path) -> None:
    out = supertool.op_wc(str(tmp_path))
    assert "ERROR" in out

def test_dispatch_wc(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("one two\nthree\n")
    out = supertool.dispatch(f"wc:{f}")
    assert "--- wc:" in out
    assert "2 " in out


# ---------------------------------------------------------------------------
# grep count mode
# ---------------------------------------------------------------------------

def test_grep_count_single_file(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("import os\nimport sys\ndef main():\n    pass\n")
    out = supertool.op_grep("import", str(f), count_only=True)
    assert "2 total matches across 1 files" in out
    assert f"{f}:2" in out

def test_grep_count_multiple_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("foo\nbar\n")
    (tmp_path / "b.py").write_text("foo\nfoo\n")
    out = supertool.op_grep("foo", str(tmp_path), count_only=True)
    assert "3 total matches across 2 files" in out

def test_grep_count_no_matches(tmp_path: Path) -> None:
    f = tmp_path / "empty.py"
    f.write_text("nothing here\n")
    out = supertool.op_grep("ZZZZZZ", str(f), count_only=True)
    assert "0 total matches across 0 files" in out

def test_dispatch_grep_count(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("foo\nbar\nfoo\n")
    out = supertool.dispatch(f"grep:foo:{f}:10:0:count")
    assert "2 total" in out

def test_grep_count_empty_pattern() -> None:
    out = supertool.op_grep("", ".", count_only=True)
    assert "ERROR" in out


# ---------------------------------------------------------------------------
# read with grep filter
# ---------------------------------------------------------------------------

def test_read_grep_filter(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("<?php\nuse Foo;\nuse Bar;\nclass X {\n}\n")
    out = supertool.op_read(str(f), grep_filter="use")
    assert "use Foo" in out
    assert "use Bar" in out
    assert "class X" not in out

def test_read_grep_filter_preserves_line_numbers(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("line1\nline2\ntarget\nline4\n")
    out = supertool.op_read(str(f), grep_filter="target")
    assert "3→target" in out

def test_read_grep_filter_no_matches(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("hello\nworld\n")
    out = supertool.op_read(str(f), grep_filter="ZZZZ")
    assert "no lines matching" in out

def test_read_grep_filter_with_offset(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    lines = [f"line{i}\n" for i in range(20)]
    f.write_text("".join(lines))
    out = supertool.op_read(str(f), offset=5, limit=10, grep_filter="line1")
    # only lines 6-15 searched, line10-line14 match "line1"
    assert "line10" in out
    assert "line0" not in out

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
