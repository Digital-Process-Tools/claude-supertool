from __future__ import annotations

import json
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

def test_check_no_ops_defined() -> None:
    supertool._CONFIG = {}
    out = supertool.op_check("phpstan", "some/file.php")
    assert "ERROR" in out
    assert "no ops defined" in out


def test_check_unknown_preset() -> None:
    supertool._CONFIG = {
        "ops": {"phpstan": {"cmd": "php -l {file}"}, "phpmd": {"cmd": "echo {file}"}}
    }
    out = supertool.op_check("unknown", "file.php")
    assert "ERROR" in out
    assert "unknown" in out
    assert "phpstan" in out


def test_check_pass(tmp_path: Path) -> None:
    f = tmp_path / "good.txt"
    f.write_text("hello")
    supertool._CONFIG = {"ops": {"lint": {"cmd": "cat {file}"}}}
    out = supertool.op_check("lint", str(f))
    assert "PASS" in out


def test_check_fail() -> None:
    supertool._CONFIG = {"ops": {"fail": {"cmd": "exit 1"}}}
    out = supertool.op_check("fail", "dummy.php")
    assert "FAIL" in out


def test_check_timeout() -> None:
    supertool._CONFIG = {"ops": {"slow": {"cmd": "sleep 10", "timeout": 1}}}
    out = supertool.op_check("slow", "dummy.php")
    assert "FAIL" in out
    assert "timeout" in out


def test_check_dict_config(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("ok")
    supertool._CONFIG = {"ops": {"mycheck": {"cmd": "cat {file}", "timeout": 5}}}
    out = supertool.op_check("mycheck", str(f))
    assert "PASS" in out


def test_check_empty_preset() -> None:
    supertool._CONFIG = {"ops": {"lint": {"cmd": "echo ok"}}}
    out = supertool.op_check("", "file.php")
    assert "ERROR" in out


# ---------------------------------------------------------------------------
# op_diff
# ---------------------------------------------------------------------------

def test_diff_identical_files(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("hello\nworld\n")
    f2.write_text("hello\nworld\n")
    out = supertool.op_diff(str(f1), str(f2))
    assert "identical" in out


def test_diff_different_files(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("hello\nworld\n")
    f2.write_text("hello\nearth\n")
    out = supertool.op_diff(str(f1), str(f2))
    assert "---" in out
    assert "+++" in out
    assert "-world" in out
    assert "+earth" in out


def test_diff_file_not_found(tmp_path: Path) -> None:
    f1 = tmp_path / "exists.txt"
    f1.write_text("hello")
    out = supertool.op_diff(str(f1), str(tmp_path / "nope.txt"))
    assert "ERROR" in out
    assert "not found" in out


def test_diff_empty_path() -> None:
    out = supertool.op_diff("", "some/file.txt")
    assert "ERROR" in out


def test_diff_empty_files(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("")
    f2.write_text("")
    out = supertool.op_diff(str(f1), str(f2))
    assert "identical" in out


def test_diff_dispatch(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("line1\n")
    f2.write_text("line2\n")
    out = supertool.dispatch(f"diff:{f1}:{f2}")
    assert "--- diff:" in out
    assert "-line1" in out
    assert "+line2" in out


# ---------------------------------------------------------------------------
# op_stat
# ---------------------------------------------------------------------------

def test_stat_file(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("hello world")
    out = supertool.op_stat(str(f))
    assert "11" in out  # 11 bytes
    assert "file" in out
    assert str(f) in out


def test_stat_directory(tmp_path: Path) -> None:
    d = tmp_path / "subdir"
    d.mkdir()
    out = supertool.op_stat(str(d))
    assert "dir" in out
    assert str(d) in out


def test_stat_not_found(tmp_path: Path) -> None:
    out = supertool.op_stat(str(tmp_path / "nope"))
    assert "ERROR" in out
    assert "not found" in out


def test_stat_empty_path() -> None:
    out = supertool.op_stat("")
    assert "ERROR" in out


def test_stat_dispatch(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("data")
    out = supertool.dispatch(f"stat:{f}")
    assert "--- stat:" in out
    assert "file" in out


# ---------------------------------------------------------------------------
# op_around_line
# ---------------------------------------------------------------------------

def test_around_line_basic(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("".join(f"line {i}\n" for i in range(1, 21)))
    out = supertool.op_around_line(str(f), 10, 3)
    assert "→" in out
    assert "line 10" in out
    assert "line 7" in out   # 3 lines before
    assert "line 13" in out  # 3 lines after


def test_around_line_first_line(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("first\nsecond\nthird\n")
    out = supertool.op_around_line(str(f), 1, 2)
    assert "→" in out
    assert "first" in out


def test_around_line_last_line(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("a\nb\nc\n")
    out = supertool.op_around_line(str(f), 3, 2)
    assert "→" in out
    assert "c" in out


def test_around_line_file_not_found() -> None:
    out = supertool.op_around_line("/nonexistent/file.py", 5)
    assert "ERROR" in out
    assert "not found" in out


def test_around_line_exceeds_length(tmp_path: Path) -> None:
    f = tmp_path / "short.py"
    f.write_text("one\ntwo\n")
    out = supertool.op_around_line(str(f), 99)
    assert "ERROR" in out
    assert "exceeds" in out


def test_around_line_zero_line(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("hello\n")
    out = supertool.op_around_line(str(f), 0)
    assert "ERROR" in out
    assert ">= 1" in out


def test_around_line_dispatch(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("alpha\nbeta\ngamma\ndelta\n")
    out = supertool.dispatch(f"around_line:{f}:2:1")
    assert "--- around_line:" in out
    assert "→" in out
    assert "beta" in out


# ---------------------------------------------------------------------------
# op_version
# ---------------------------------------------------------------------------

def test_version_returns_version_string() -> None:
    out = supertool.op_version()
    assert out == f"supertool {supertool.VERSION}\n"


def test_version_dispatch() -> None:
    out = supertool.dispatch("version")
    assert supertool.VERSION in out
    assert "---" not in out  # meta-op, no header


# ---------------------------------------------------------------------------
# op_introduction — project-specific intro text from .supertool.json
# ---------------------------------------------------------------------------

def test_introduction_from_config(tmp_path: Path, monkeypatch) -> None:
    """introduction key in config is output verbatim."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "introduction": "supertool batches ops into one call.\nPack 6-7 per call."
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_introduction()
    assert "supertool batches ops into one call." in out
    assert "Pack 6-7 per call." in out


def test_introduction_no_config(tmp_path: Path, monkeypatch) -> None:
    """No .supertool.json → fallback message."""
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_introduction()
    assert "No introduction configured" in out


def test_introduction_missing_key(tmp_path: Path, monkeypatch) -> None:
    """Config exists but no introduction key → fallback message."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({"compact": True}))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_introduction()
    assert "No introduction configured" in out


def test_introduction_dispatch(tmp_path: Path, monkeypatch) -> None:
    """dispatch('introduction') routes to op_introduction."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "introduction": "Hello LLM, this is supertool."
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.dispatch("introduction")
    assert "--- introduction ---" not in out
    assert "Hello LLM, this is supertool." in out


# ---------------------------------------------------------------------------
# op_output_format — output format examples from .supertool.json
# ---------------------------------------------------------------------------

def test_output_format_from_config(tmp_path: Path, monkeypatch) -> None:
    """output-format key in config is output verbatim."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "output-format": "--- read:foo ---\n     1→hello"
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_output_format()
    assert "--- read:foo ---" in out
    assert "1→hello" in out


def test_output_format_no_config(tmp_path: Path, monkeypatch) -> None:
    """No config → fallback message."""
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_output_format()
    assert "No output-format configured" in out


def test_output_format_dispatch(tmp_path: Path, monkeypatch) -> None:
    """dispatch('output-format') routes to op_output_format."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "output-format": "--- example ---\nsome output"
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.dispatch("output-format")
    assert "--- output-format ---" not in out
    assert "--- example ---" in out


# ---------------------------------------------------------------------------
# op_ops — self-documenting ops reference from .supertool.json
# ---------------------------------------------------------------------------

def test_ops_no_config(tmp_path: Path, monkeypatch) -> None:
    """No .supertool.json → shows built-in op names only (no descriptions)."""
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    # Should list built-in op names as fallback
    assert "read" in out
    assert "grep" in out
    assert "glob" in out
    assert "No descriptions configured" in out


def test_ops_builtin_ops_section(tmp_path: Path, monkeypatch) -> None:
    """builtin-ops section renders syntax, description, and example."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "builtin-ops": {
            "read": {
                "syntax": "read:PATH[:OFFSET:LIMIT]",
                "description": "Read file (300 lines, 20KB cap)",
                "example": "read:src/foo.py:10:50"
            },
            "grep": {
                "syntax": "grep:PATTERN:PATH[:LIMIT]",
                "description": "Search files",
                "example": "grep:extends:src/:20"
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "read:PATH[:OFFSET:LIMIT]" in out
    assert "Read file (300 lines, 20KB cap)" in out
    assert "read:src/foo.py:10:50" in out
    assert "grep:PATTERN:PATH[:LIMIT]" in out
    assert "Search files" in out


def test_ops_custom_ops_with_description(tmp_path: Path, monkeypatch) -> None:
    """Custom ops from ops section show description and example."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "ops": {
            "phpstan": {
                "cmd": "php -l {file}",
                "timeout": 30,
                "description": "PHPStan level 9. Use after every Edit.",
                "example": "phpstan:src/Foo.php"
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "phpstan" in out
    assert "PHPStan level 9. Use after every Edit." in out
    assert "phpstan:src/Foo.php" in out


def test_ops_custom_ops_without_description(tmp_path: Path, monkeypatch) -> None:
    """Custom ops without description still appear with just the name."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "ops": {
            "phpmd": {
                "cmd": "phpmd {file} text codesize",
                "timeout": 60
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "phpmd" in out


def test_ops_status_0_hides_builtin(tmp_path: Path, monkeypatch) -> None:
    """builtin-ops with status: 0 are hidden from output."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "builtin-ops": {
            "read": {
                "syntax": "read:PATH",
                "description": "Read file",
                "status": 1
            },
            "map": {
                "syntax": "map:PATH",
                "description": "Symbol tree",
                "status": 0
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "read:PATH" in out
    assert "map" not in out


def test_ops_status_0_hides_custom(tmp_path: Path, monkeypatch) -> None:
    """Custom ops with status: 0 are hidden from output."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "ops": {
            "phpstan": {
                "cmd": "php -l {file}",
                "description": "PHPStan",
                "status": 1
            },
            "psr": {
                "cmd": "phpcs {file}",
                "description": "PSR12 check",
                "status": 0
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "phpstan" in out
    assert "psr" not in out


def test_ops_both_sections_combined(tmp_path: Path, monkeypatch) -> None:
    """Both builtin-ops and custom ops appear in output."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "builtin-ops": {
            "read": {
                "syntax": "read:PATH",
                "description": "Read file"
            }
        },
        "ops": {
            "lint": {
                "cmd": "php -l {file}",
                "description": "PHP syntax check",
                "example": "lint:src/Foo.php"
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "## Operations" in out
    assert "read:PATH" in out
    assert "lint" in out
    assert "PHP syntax check" in out


def test_ops_dispatch_integration(tmp_path: Path, monkeypatch) -> None:
    """dispatch('ops') routes to op_ops."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "builtin-ops": {
            "read": {"syntax": "read:PATH", "description": "Read file"}
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.dispatch("ops")
    assert "--- ops ---" not in out
    assert "read:PATH" in out


def test_ops_default_status_is_shown(tmp_path: Path, monkeypatch) -> None:
    """Missing status field defaults to shown (status: 1)."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "builtin-ops": {
            "read": {
                "syntax": "read:PATH",
                "description": "Read file"
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "read:PATH" in out


def test_ops_aliases_section_rendered() -> None:
    """Aliases appear in a separate section in op_ops output."""
    supertool._CONFIG = {
        "aliases": {
            "verify": {
                "ops": ["phpstan:{file}", "phpmd:{file}"],
                "description": "Run all quality checks",
                "example": "verify:src/Module.php"
            }
        }
    }
    supertool._CONFIG_CHECKED = True
    out = supertool.op_ops()
    assert "## Aliases" in out
    assert "verify" in out
    assert "Run all quality checks" in out
    assert "verify:src/Module.php" in out


def test_ops_aliases_status_0_hidden() -> None:
    """Aliases with status: 0 are hidden from output."""
    supertool._CONFIG = {
        "aliases": {
            "visible": {"ops": ["read:{file}"], "description": "Shown"},
            "hidden": {"ops": ["read:{file}"], "description": "Hidden", "status": 0}
        }
    }
    supertool._CONFIG_CHECKED = True
    out = supertool.op_ops()
    assert "visible" in out
    assert "hidden" not in out


def test_alias_ops_not_a_list() -> None:
    """Alias with ops as a string instead of list gives error."""
    supertool._CONFIG = {
        "aliases": {"bad": {"ops": "not-a-list"}}
    }
    out = supertool._resolve_alias("bad", ["bad", "file.php"])
    assert "ERROR" in out
    assert "must be a list" in out
