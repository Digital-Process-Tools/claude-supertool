"""Tests for the jsonlint validator adapter."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "validators" / "jsonlint" / "jsonlint.py"


def _run(file_path: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(ADAPTER), file_path],
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Valid JSON
# ---------------------------------------------------------------------------

def test_valid_json_object(tmp_path: Path) -> None:
    f = tmp_path / "good.json"
    f.write_text('{"key": "value", "num": 42}')
    out = _run(str(f))
    assert out["ok"] is True
    assert out["count"] == 0
    assert out["errors"] == []
    assert out["tool"] == "jsonlint"


def test_valid_json_array(tmp_path: Path) -> None:
    f = tmp_path / "arr.json"
    f.write_text('[1, 2, 3]')
    out = _run(str(f))
    assert out["ok"] is True
    assert out["count"] == 0


def test_valid_json_nested(tmp_path: Path) -> None:
    f = tmp_path / "nested.json"
    f.write_text('{"a": {"b": [true, false, null]}}')
    out = _run(str(f))
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# Invalid JSON
# ---------------------------------------------------------------------------

def test_invalid_json_returns_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text('{"key": "value"')  # missing closing brace
    out = _run(str(f))
    assert out["ok"] is False
    assert out["count"] == 1
    assert len(out["errors"]) == 1


def test_invalid_json_error_has_line_col(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text('{\n  "key": INVALID\n}')
    out = _run(str(f))
    assert out["ok"] is False
    err = out["errors"][0]
    assert err["line"] is not None
    assert err["col"] is not None
    assert err["severity"] == "error"
    assert err["code"] == "syntax"


def test_invalid_json_trailing_comma(tmp_path: Path) -> None:
    f = tmp_path / "trailing.json"
    f.write_text('{"a": 1,}')
    out = _run(str(f))
    assert out["ok"] is False


def test_invalid_json_msg_populated(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text('not json at all')
    out = _run(str(f))
    assert out["ok"] is False
    assert out["errors"][0]["msg"]  # non-empty


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

def test_missing_file_returns_error(tmp_path: Path) -> None:
    out = _run(str(tmp_path / "nonexistent.json"))
    assert out["ok"] is False
    assert out["count"] == 1
    err = out["errors"][0]
    assert err["code"] == "adapter"
    assert "not found" in err["msg"]


# ---------------------------------------------------------------------------
# No argument
# ---------------------------------------------------------------------------

def test_no_arg_returns_error() -> None:
    result = subprocess.run(
        [sys.executable, str(ADAPTER)],
        capture_output=True,
        text=True,
    )
    out = json.loads(result.stdout)
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "adapter"


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

def test_output_contains_required_fields(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text('{}')
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out


def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text('{}')
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)
