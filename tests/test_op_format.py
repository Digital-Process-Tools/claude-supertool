"""Tests for op_format — manual one-shot formatter runner."""
from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_formatters(fmt: dict) -> None:
    supertool._CONFIG = {"formatters": fmt}
    supertool._CONFIG_CHECKED = True


# ---------------------------------------------------------------------------
# op_format
# ---------------------------------------------------------------------------

def test_op_format_no_formatters_configured() -> None:
    _set_formatters({})
    result = supertool.op_format("some/file.json")
    assert result == "no formatters configured\n"


def test_op_format_no_path_returns_error() -> None:
    _set_formatters({"fmt": {"cmd": "{python} -c \"pass\"", "match": "*.json"}})
    result = supertool.op_format("")
    assert result.startswith("ERROR")


def test_op_format_runs_matching_formatter(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    sentinel = tmp_path / "ran"
    _set_formatters({
        "prettier": {
            "cmd": f"{{python}} -c \"open(r\'{sentinel.as_posix()}\', \'w\').close()\"",
            "match": "*.json",
        }
    })
    result = supertool.op_format(str(f))
    assert sentinel.exists(), "formatter did not run"
    assert "prettier" in result
    assert "ok" in result


def test_op_format_with_tool_filter(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    sentinel_a = tmp_path / "ran_a"
    sentinel_b = tmp_path / "ran_b"
    _set_formatters({
        "fmt-a": {"cmd": f"{{python}} -c \"open(r\'{sentinel_a.as_posix()}\', \'w\').close()\"", "match": "*.json"},
        "fmt-b": {"cmd": f"{{python}} -c \"open(r\'{sentinel_b.as_posix()}\', \'w\').close()\"", "match": "*.json"},
    })
    result = supertool.op_format(str(f), tool_filter=["fmt-a"])
    assert sentinel_a.exists()
    assert not sentinel_b.exists()
    assert "fmt-a" in result
    assert "fmt-b" not in result


def test_op_format_tool_filter_no_match() -> None:
    _set_formatters({"prettier": {"cmd": "{python} -c \"pass\"", "match": "*.json"}})
    result = supertool.op_format("x.json", tool_filter=["nonexistent"])
    assert "no formatters matched filter" in result


def test_op_format_no_glob_match(tmp_path: Path) -> None:
    f = tmp_path / "x.php"
    f.write_text("<?php\n")
    _set_formatters({"prettier": {"cmd": "{python} -c \"pass\"", "match": "*.json"}})
    result = supertool.op_format(str(f))
    assert "no formatters matched this file" in result


def test_op_format_formatter_failure_shown(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    _set_formatters({"bad-fmt": {"cmd": "{python} -c \"raise SystemExit(1)\"", "match": "*.json"}})
    result = supertool.op_format(str(f))
    assert "fail" in result
    assert "bad-fmt" in result


# ---------------------------------------------------------------------------
# op_format verbose mode
# ---------------------------------------------------------------------------

def test_op_format_verbose_shows_marker(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    _set_formatters({"prettier": {"cmd": "{python} -c \"pass\"", "match": "*.json"}})
    result = supertool.op_format(str(f), verbose=True)
    assert "[verbose]" in result


def test_op_format_non_verbose_no_marker(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    _set_formatters({"prettier": {"cmd": "{python} -c \"pass\"", "match": "*.json"}})
    result = supertool.op_format(str(f), verbose=False)
    assert "[verbose]" not in result


# ---------------------------------------------------------------------------
# dispatch: format verbose parsing
# ---------------------------------------------------------------------------

def test_dispatch_format_verbose_flag(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    _set_formatters({"prettier": {"cmd": "{python} -c \"pass\"", "match": "*.json"}})
    result = supertool.dispatch(f"format:{f}:verbose")
    assert "[verbose]" in result


def test_dispatch_format_tools_and_verbose(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    sentinel_a = tmp_path / "ran_a"
    sentinel_b = tmp_path / "ran_b"
    _set_formatters({
        "fmt-a": {"cmd": f"{{python}} -c \"open(r\'{sentinel_a.as_posix()}\', \'w\').close()\"", "match": "*.json"},
        "fmt-b": {"cmd": f"{{python}} -c \"open(r\'{sentinel_b.as_posix()}\', \'w\').close()\"", "match": "*.json"},
    })
    result = supertool.dispatch(f"format:{f}:fmt-a:verbose")
    assert sentinel_a.exists()
    assert not sentinel_b.exists()
    assert "[verbose]" in result
