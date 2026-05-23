"""Tests for the pyright validator adapter."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "validators" / "pyright" / "pyright.py"


def _run(file_path: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(ADAPTER), file_path],
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Tool missing — graceful degrade
# ---------------------------------------------------------------------------

def test_missing_tool_graceful(tmp_path: Path) -> None:
    """When pyright is not on PATH, exit 0 with ok=True and a stderr warning."""
    f = tmp_path / "hello.py"
    f.write_text("x: int = 1\n")
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True,
        text=True,
        env={"PATH": ""},
    )
    out = json.loads(result.stdout)
    assert out["ok"] is True
    assert out["count"] == 0
    assert "pyright" in result.stderr.lower()


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

def test_output_schema_present(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x: int = 1\n")
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out
    assert out["tool"] == "pyright"


def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x: int = 1\n")
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)


# ---------------------------------------------------------------------------
# Real pyright runs (only when pyright is available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("pyright"), reason="pyright not on PATH")
def test_clean_py(tmp_path: Path) -> None:
    f = tmp_path / "good.py"
    f.write_text("x: int = 42\nprint(x)\n")
    out = _run(str(f))
    assert out["ok"] is True
    assert out["count"] == 0


@pytest.mark.skipif(not shutil.which("pyright"), reason="pyright not on PATH")
def test_type_error_reported(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    # int annotation but assigned a str — pyright catches it.
    f.write_text("x: int = 'not a number'\nprint(x)\n")
    out = _run(str(f))
    assert out["ok"] is False
    assert out["count"] >= 1
    err = out["errors"][0]
    # pyright 0-indexed range.start converted to 1-indexed line/col.
    assert err["line"] is not None and err["line"] >= 1
    assert err["col"] is not None and err["col"] >= 1
    assert err["severity"] in ("error", "warning")
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    assert len(err["source_context"]) > 0


@pytest.mark.skipif(not shutil.which("pyright"), reason="pyright not on PATH")
def test_info_and_hint_severities_dropped(tmp_path: Path) -> None:
    """Only 'error' and 'warning' diagnostics should appear in the output —
    pyright's 'information' / 'hint' levels are filtered out by the adapter."""
    f = tmp_path / "good.py"
    f.write_text("x: int = 42\nprint(x)\n")
    out = _run(str(f))
    for err in out["errors"]:
        assert err["severity"] in ("error", "warning")
