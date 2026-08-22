"""Tests for the actionlint validator adapter (#1798)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _winenv import empty_path_env
from _adapter_verdict import assert_declined, assert_ok

ADAPTER = Path(__file__).parent.parent / "validators" / "actionlint" / "actionlint.py"


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
    """Absent actionlint is `skipped`, not `ok: true` (#1202, #1798).

    actionlint is not installed on the machine this adapter was written on —
    the absent path is the FIRST thing anyone who installs this validator
    hits, not an edge case. Escalation under `$SUPERTOOL_REQUIRE_VALIDATORS`
    is asserted in `tests/test_validators_absent_tool_third_state_1202.py`.
    """
    f = tmp_path / "test.yml"
    f.write_text("on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
                  "    steps:\n      - run: echo hi\n")
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True,
        text=True,
        env=empty_path_env(), encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert "skipped" in out, out
    assert "actionlint" in out["skipped"]
    assert "ok" not in out, out
    assert "count" not in out, out
    assert "errors" not in out, out


# ---------------------------------------------------------------------------
# Valid workflow (only when actionlint available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("actionlint"), reason="actionlint not on PATH")
def test_valid_workflow(tmp_path: Path) -> None:
    f = tmp_path / "test.yml"
    f.write_text(
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: echo hi\n"
    )
    out = _run(str(f))
    assert_ok(out)
    assert out["tool"] == "actionlint"


# ---------------------------------------------------------------------------
# Invalid workflow (only when actionlint available) — must FIRE
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("actionlint"), reason="actionlint not on PATH")
def test_invalid_workflow_reports_findings(tmp_path: Path) -> None:
    """Unknown key + bad runs-on label — actionlint's bread and butter."""
    f = tmp_path / "test.yml"
    f.write_text(
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: not-a-real-runner-label\n"
        "    steps:\n"
        "      - run: echo hi\n"
        "        bogus-key: true\n"
    )
    out = _run(str(f))
    assert out["ok"] is False
    assert out["count"] > 0
    err = out["errors"][0]
    assert err["line"] is not None
    assert err["code"]
    assert err["msg"]


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

@pytest.mark.skipif(not shutil.which("actionlint"), reason="actionlint not on PATH")
def test_output_contains_required_fields(tmp_path: Path) -> None:
    f = tmp_path / "test.yml"
    f.write_text("on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
                  "    steps:\n      - run: echo hi\n")
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out


@pytest.mark.skipif(not shutil.which("actionlint"), reason="actionlint not on PATH")
def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "test.yml"
    f.write_text("on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
                  "    steps:\n      - run: echo hi\n")
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)


@pytest.mark.skipif(not shutil.which("actionlint"), reason="actionlint not on PATH")
def test_source_context_present_on_error(tmp_path: Path) -> None:
    f = tmp_path / "test.yml"
    f.write_text(
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: not-a-real-runner-label\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )
    out = _run(str(f))
    if out["ok"] or not out["errors"]:
        pytest.skip("actionlint found no issues with this workflow")
    err = out["errors"][0]
    assert err["line"] is not None
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    assert len(err["source_context"]) > 0
