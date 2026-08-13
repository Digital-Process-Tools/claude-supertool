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
        text=True, encoding="utf-8", errors="replace",
    )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Tool missing — graceful degrade
# ---------------------------------------------------------------------------

def test_missing_tool_is_the_third_state(tmp_path: Path) -> None:
    """Absent markdownlint is `skipped`, not `ok: true` (#1202).

    Escalation under `$SUPERTOOL_REQUIRE_VALIDATORS` is asserted in
    `tests/test_validators_absent_tool_third_state_1202.py`.
    """
    f = tmp_path / "readme.md"
    f.write_text("# Hello\n\nSome text.\n")
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True,
        text=True,
        env=empty_path_env(), encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert "skipped" in out, out
    assert "markdownlint" in out["skipped"]
    assert "ok" not in out, out


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
        text=True, encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert_declined(out)
    assert out["errors"][0]["code"] == "adapter"


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("markdownlint"), reason="markdownlint not on PATH")
def test_output_contains_required_fields(tmp_path: Path) -> None:
    f = tmp_path / "x.md"
    f.write_text("# Hello\n")
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out


@pytest.mark.skipif(not shutil.which("markdownlint"), reason="markdownlint not on PATH")
def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.md"
    f.write_text("# Hello\n")
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("markdownlint"), reason="markdownlint not on PATH")
def test_a_missing_file_is_the_third_state(tmp_path: Path) -> None:
    """A path that resolves to no files is not a file that linted clean (#1601).

    markdownlint-cli exits 0 and prints its usage banner when its arguments
    resolve to nothing, so this used to be `ok: true` about a file that does
    not exist. The unignored arm is stubbed in
    `tests/test_validators_scope_is_not_a_verdict_1601.py`, which runs where
    markdownlint is not installed — here and on CI both.
    """
    out = _run(str(tmp_path / "nonexistent.md"))
    assert "skipped" in out, out
    assert "ok" not in out, out


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
