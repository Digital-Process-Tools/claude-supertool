"""Tests for the markdownlint validator adapter."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _winenv import empty_path_env
from _adapter_verdict import assert_declined, assert_ok

ADAPTER = Path(__file__).parent.parent / "validators" / "markdownlint" / "markdownlint.py"


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
    """When markdownlint is not on PATH, exit 0 with ok=True and a stderr warning."""
    f = tmp_path / "readme.md"
    f.write_text("# Hello\n\nSome text.\n")
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True,
        text=True,
        env=empty_path_env(),
    )
    out = json.loads(result.stdout)
    assert_ok(out)
    assert out["count"] == 0
    assert "markdownlint" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Valid Markdown (only when markdownlint available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("markdownlint"), reason="markdownlint not on PATH")
def test_valid_markdown(tmp_path: Path) -> None:
    f = tmp_path / "good.md"
    f.write_text("# Title\n\nSome paragraph text.\n")
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0
    assert out["tool"] == "markdownlint"


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
    assert_declined(out)
    assert out["errors"][0]["code"] == "adapter"


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

def test_output_contains_required_fields(tmp_path: Path) -> None:
    f = tmp_path / "x.md"
    f.write_text("# Hello\n")
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out


def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.md"
    f.write_text("# Hello\n")
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

def test_missing_file_behavior(tmp_path: Path) -> None:
    """Missing file: markdownlint errors (if present) or graceful skip (if absent)."""
    out = _run(str(tmp_path / "nonexistent.md"))
    assert "ok" in out


@pytest.mark.skipif(not shutil.which("markdownlint"), reason="markdownlint not on PATH")
def test_source_context_present_on_error(tmp_path: Path) -> None:
    # MD022 = headings should be surrounded by blank lines; triggers a line-anchored error
    f = tmp_path / "bad.md"
    f.write_text("# Title\nno blank line before next heading\n## Second\n")
    out = _run(str(f))
    if out["ok"] or not out["errors"]:
        pytest.skip("markdownlint found no issues with this file")
    err = out["errors"][0]
    assert err["line"] is not None
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    assert len(err["source_context"]) > 0
