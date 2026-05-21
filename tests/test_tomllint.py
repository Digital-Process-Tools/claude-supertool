"""Tests for the tomllint validator adapter."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "validators" / "tomllint" / "tomllint.py"


def _run(file_path: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(ADAPTER), file_path],
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Graceful degrade when tomllib unavailable (Python < 3.11, no tomli)
# Covered implicitly — on 3.11+ stdlib is used; on older without tomli, ok=True
# We test the output contract rather than mocking the import.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Valid TOML
# ---------------------------------------------------------------------------

def test_valid_toml_simple(tmp_path: Path) -> None:
    f = tmp_path / "good.toml"
    f.write_text('[package]\nname = "myapp"\nversion = "1.0.0"\n')
    out = _run(str(f))
    # On Python < 3.11 without tomli, ok=True (graceful skip) — still acceptable
    assert out["ok"] is True or (out["ok"] is True and out["count"] == 0)
    assert out["tool"] == "tomllint"


def test_valid_toml_complex(tmp_path: Path) -> None:
    f = tmp_path / "complex.toml"
    f.write_text(
        '[database]\nhost = "localhost"\nport = 5432\n\n'
        '[[servers]]\nname = "alpha"\nip = "10.0.0.1"\n'
    )
    out = _run(str(f))
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# Invalid TOML (only meaningful when tomllib is available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib stdlib requires Python 3.11+")
def test_invalid_toml_returns_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.toml"
    f.write_text("[package\nname = missing_bracket\n")
    out = _run(str(f))
    assert out["ok"] is False
    assert out["count"] >= 1
    assert len(out["errors"]) >= 1


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib stdlib requires Python 3.11+")
def test_invalid_toml_error_has_msg(tmp_path: Path) -> None:
    f = tmp_path / "bad.toml"
    f.write_text("key = \n")  # bare value
    out = _run(str(f))
    assert out["ok"] is False
    assert out["errors"][0]["msg"]
    assert out["errors"][0]["severity"] == "error"
    assert out["errors"][0]["code"] == "syntax"


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

def test_missing_file_returns_error(tmp_path: Path) -> None:
    out = _run(str(tmp_path / "nonexistent.toml"))
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "adapter"
    assert "not found" in out["errors"][0]["msg"]


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
    f = tmp_path / "x.toml"
    f.write_text('[x]\n')
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out


def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.toml"
    f.write_text('[x]\n')
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib stdlib requires Python 3.11+")
def test_source_context_present_on_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.toml"
    # Line 2 has the error — line 1 content provides context
    f.write_text('[package]\nname = \n')
    out = _run(str(f))
    assert out["ok"] is False
    err = out["errors"][0]
    # source_context only populated if line was extractable from the error message
    # tomllib may or may not embed line info — check shape only if line is not None
    assert "source_context" in err
    if err["line"] is not None:
        assert isinstance(err["source_context"], list)
