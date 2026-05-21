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
    _set_formatters({"fmt": {"cmd": "true", "match": "*.json"}})
    result = supertool.op_format("")
    assert result.startswith("ERROR")


def test_op_format_runs_matching_formatter(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    sentinel = tmp_path / "ran"
    _set_formatters({
        "prettier": {
            "cmd": f"touch {sentinel}",
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
        "fmt-a": {"cmd": f"touch {sentinel_a}", "match": "*.json"},
        "fmt-b": {"cmd": f"touch {sentinel_b}", "match": "*.json"},
    })
    result = supertool.op_format(str(f), tool_filter=["fmt-a"])
    assert sentinel_a.exists()
    assert not sentinel_b.exists()
    assert "fmt-a" in result
    assert "fmt-b" not in result


def test_op_format_tool_filter_no_match() -> None:
    _set_formatters({"prettier": {"cmd": "true", "match": "*.json"}})
    result = supertool.op_format("x.json", tool_filter=["nonexistent"])
    assert "no formatters matched filter" in result


def test_op_format_no_glob_match(tmp_path: Path) -> None:
    f = tmp_path / "x.php"
    f.write_text("<?php\n")
    _set_formatters({"prettier": {"cmd": "true", "match": "*.json"}})
    result = supertool.op_format(str(f))
    assert "no formatters matched this file" in result


def test_op_format_formatter_failure_shown(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    _set_formatters({"bad-fmt": {"cmd": "false", "match": "*.json"}})
    result = supertool.op_format(str(f))
    assert "fail" in result
    assert "bad-fmt" in result
