"""Tests for the hadolint validator adapter."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "validators" / "hadolint" / "hadolint.py"


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
    """When hadolint is not on PATH, exit 0 with ok=True and a stderr warning."""
    f = tmp_path / "Dockerfile"
    f.write_text("FROM ubuntu:22.04\nRUN apt-get update\n")
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True,
        text=True,
        env={"PATH": ""},
    )
    out = json.loads(result.stdout)
    assert out["ok"] is True
    assert out["count"] == 0
    assert "hadolint" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Valid Dockerfile (only when hadolint available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("hadolint"), reason="hadolint not on PATH")
def test_valid_dockerfile(tmp_path: Path) -> None:
    f = tmp_path / "Dockerfile"
    f.write_text(
        "FROM ubuntu:22.04\n"
        "RUN apt-get update && apt-get install -y curl\n"
        'CMD ["bash"]\n'
    )
    out = _run(str(f))
    assert out["ok"] is True
    assert out["tool"] == "hadolint"


# ---------------------------------------------------------------------------
# Dockerfile with lint issues (only when hadolint available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("hadolint"), reason="hadolint not on PATH")
def test_dockerfile_lint_warnings(tmp_path: Path) -> None:
    """apt-get without version pinning typically triggers DL3008."""
    f = tmp_path / "Dockerfile"
    f.write_text("FROM ubuntu:22.04\nRUN apt-get update\nRUN apt-get install curl\n")
    out = _run(str(f))
    # hadolint may return warnings — ok may be False with count > 0
    assert "ok" in out
    assert isinstance(out["count"], int)
    if not out["ok"]:
        assert len(out["errors"]) > 0
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
        text=True,
    )
    out = json.loads(result.stdout)
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "adapter"


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

def test_output_contains_required_fields(tmp_path: Path) -> None:
    f = tmp_path / "Dockerfile"
    f.write_text("FROM scratch\n")
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out


def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "Dockerfile"
    f.write_text("FROM scratch\n")
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

def test_missing_file_behavior(tmp_path: Path) -> None:
    """Missing file: hadolint errors (if present) or graceful skip (if absent)."""
    out = _run(str(tmp_path / "Dockerfile"))
    assert "ok" in out
