"""Tests for the inilint validator adapter."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "validators" / "inilint" / "inilint.py"


def _run(file_path: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(ADAPTER), file_path],
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Valid INI
# ---------------------------------------------------------------------------

def test_valid_ini_simple(tmp_path: Path) -> None:
    f = tmp_path / "good.ini"
    f.write_text("[section]\nkey = value\nnum = 42\n")
    out = _run(str(f))
    assert out["ok"] is True
    assert out["count"] == 0
    assert out["errors"] == []
    assert out["tool"] == "inilint"


def test_valid_ini_multiple_sections(tmp_path: Path) -> None:
    f = tmp_path / "multi.ini"
    f.write_text("[db]\nhost = localhost\nport = 3306\n\n[app]\ndebug = false\n")
    out = _run(str(f))
    assert out["ok"] is True
    assert out["count"] == 0


def test_valid_ini_dvsi_like(tmp_path: Path) -> None:
    f = tmp_path / "configuration-dev.ini"
    f.write_text(
        "[database]\nhostname = localhost\nusername = dvsi\npassword = dvsi\ndatabase = dvsi\n"
    )
    out = _run(str(f))
    assert out["ok"] is True


def test_valid_ini_no_section(tmp_path: Path) -> None:
    """configparser supports DEFAULT section implicitly — sectionless keys fail."""
    f = tmp_path / "nosection.ini"
    f.write_text("[DEFAULT]\nkey = val\n")
    out = _run(str(f))
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# Invalid INI
# ---------------------------------------------------------------------------

def test_invalid_ini_missing_section_header(tmp_path: Path) -> None:
    f = tmp_path / "bad.ini"
    f.write_text("key = value_without_section\n")
    out = _run(str(f))
    assert out["ok"] is False
    assert out["count"] == 1
    assert len(out["errors"]) == 1


def test_invalid_ini_error_has_line_info(tmp_path: Path) -> None:
    f = tmp_path / "bad.ini"
    f.write_text("key = value_without_section\n")
    out = _run(str(f))
    assert out["ok"] is False
    err = out["errors"][0]
    assert err["line"] is not None
    assert err["severity"] == "error"
    assert err["code"] == "syntax"


def test_invalid_ini_msg_populated(tmp_path: Path) -> None:
    f = tmp_path / "bad.ini"
    f.write_text("not valid ini content here\n")
    out = _run(str(f))
    assert out["ok"] is False
    assert out["errors"][0]["msg"]


def test_invalid_ini_two_sectionless_keys(tmp_path: Path) -> None:
    f = tmp_path / "multi_bad.ini"
    # Two keys before any section header — first triggers MissingSectionHeaderError
    f.write_text("key1 = val1\nkey2 = val2\n[section]\nk = v\n")
    out = _run(str(f))
    assert out["ok"] is False
    assert out["count"] >= 1


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

def test_missing_file_returns_error(tmp_path: Path) -> None:
    out = _run(str(tmp_path / "nonexistent.ini"))
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
    f = tmp_path / "x.ini"
    f.write_text("[s]\nk = v\n")
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out


def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.ini"
    f.write_text("[s]\nk = v\n")
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)
