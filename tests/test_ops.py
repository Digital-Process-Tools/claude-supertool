from __future__ import annotations

from pathlib import Path

import supertool


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


# ---------------------------------------------------------------------------
# op_check
# ---------------------------------------------------------------------------

def test_check_no_config_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    out = supertool.op_check("phpstan", "some/file.php")
    assert "ERROR" in out
    assert ".supertool-checks.json" in out


def test_check_unknown_preset(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / ".supertool-checks.json"
    config.write_text('{"phpstan": "php -l {file}", "phpmd": "echo {file}"}')
    monkeypatch.chdir(tmp_path)
    out = supertool.op_check("unknown", "file.php")
    assert "ERROR" in out
    assert "unknown" in out
    assert "phpstan" in out


def test_check_pass(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "good.txt"
    f.write_text("hello")
    config = tmp_path / ".supertool-checks.json"
    config.write_text('{"lint": "cat {file}"}')
    monkeypatch.chdir(tmp_path)
    out = supertool.op_check("lint", str(f))
    assert "PASS" in out


def test_check_fail(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / ".supertool-checks.json"
    config.write_text('{"fail": "exit 1"}')
    monkeypatch.chdir(tmp_path)
    out = supertool.op_check("fail", "dummy.php")
    assert "FAIL" in out


def test_check_timeout(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / ".supertool-checks.json"
    config.write_text('{"slow": {"cmd": "sleep 10", "timeout": 1}}')
    monkeypatch.chdir(tmp_path)
    out = supertool.op_check("slow", "dummy.php")
    assert "TIMEOUT" in out


def test_check_dict_config(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "test.txt"
    f.write_text("ok")
    config = tmp_path / ".supertool-checks.json"
    config.write_text('{"mycheck": {"cmd": "cat {file}", "timeout": 5}}')
    monkeypatch.chdir(tmp_path)
    out = supertool.op_check("mycheck", str(f))
    assert "PASS" in out


def test_check_empty_preset(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / ".supertool-checks.json"
    config.write_text('{"lint": "echo ok"}')
    monkeypatch.chdir(tmp_path)
    out = supertool.op_check("", "file.php")
    assert "ERROR" in out
