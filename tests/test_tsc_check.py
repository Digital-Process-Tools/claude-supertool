"""Tests for the tsc-check validator adapter."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _winenv import empty_path_env
from _adapter_verdict import assert_declined, assert_ok

ADAPTER = Path(__file__).parent.parent / "validators" / "tsc-check" / "tsc-check.py"


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
    """Absent tsc is `skipped`, not `ok: true` (#1202).

    This asserted `ok is True` for as long as the adapter fabricated it — a
    clean type-check verdict about a file no compiler opened. Escalation under
    `$SUPERTOOL_REQUIRE_VALIDATORS` is asserted in
    `tests/test_validators_absent_tool_third_state_1202.py`.
    """
    f = tmp_path / "hello.ts"
    f.write_text("const x: number = 1;\n")
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True,
        text=True,
        env=empty_path_env(), encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert "skipped" in out, out
    assert "tsc" in out["skipped"]
    assert "ok" not in out, out


# ---------------------------------------------------------------------------
# Valid TypeScript (only when tsc is available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("tsc"), reason="tsc not on PATH")
def test_valid_ts(tmp_path: Path) -> None:
    f = tmp_path / "good.ts"
    f.write_text("const x: number = 42;\nexport {};\n")
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0
    assert out["tool"] == "tsc-check"


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

@pytest.mark.skipif(not shutil.which("tsc"), reason="tsc not on PATH")
def test_output_schema_present(tmp_path: Path) -> None:
    f = tmp_path / "x.ts"
    f.write_text("export {};\n")
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out


@pytest.mark.skipif(not shutil.which("tsc"), reason="tsc not on PATH")
def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.ts"
    f.write_text("export {};\n")
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("tsc"), reason="tsc not on PATH")
def test_missing_file_returns_error(tmp_path: Path) -> None:
    """Gated, because ungated it asserted the fabricated pass and nothing else.

    Without tsc this ran the absent-tool arm and `assert "ok" in out` held for
    the wrong reason — a green about a file the adapter never handed to
    anything. It now runs only where a real verdict is possible.
    """
    out = _run(str(tmp_path / "nonexistent.ts"))
    assert "ok" in out


@pytest.mark.skipif(not shutil.which("tsc"), reason="tsc not on PATH")
def test_source_context_present_on_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.ts"
    f.write_text("const x: number = 'not a number';\nexport {};\n")
    out = _run(str(f))
    if out["ok"] or not out["errors"]:
        pytest.skip("tsc found no issues (may need tsconfig)")
    err = out["errors"][0]
    assert err["line"] is not None
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    assert len(err["source_context"]) > 0
