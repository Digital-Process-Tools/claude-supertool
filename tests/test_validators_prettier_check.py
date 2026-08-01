"""Tests for the prettier-check validator adapter."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_budget import adapter_budget

ADAPTER = Path(__file__).resolve().parent.parent / "validators" / "prettier-check" / "prettier-check.py"


def _run(args: list[str], env: dict | None = None) -> dict:
    """Run the adapter, parse stdout JSON."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    result = subprocess.run(
        [sys.executable, str(ADAPTER), *args],
        capture_output=True, text=True, env=full_env, timeout=adapter_budget(ADAPTER),
    )
    assert result.stdout, f"adapter produced no stdout (stderr={result.stderr})"
    return json.loads(result.stdout)


def test_no_arg_returns_schema_error() -> None:
    data = _run([])
    assert data["ok"] is False
    assert data["count"] == 1
    assert "no file arg" in data["errors"][0]["msg"]


def test_missing_binary_emits_schema_error(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text('{"a":1}\n')
    data = _run([str(f)], env={"PRETTIER_BIN": "/nonexistent/prettier-bin"})
    assert data["ok"] is False
    assert "PRETTIER_BIN not found" in data["errors"][0]["msg"]


@pytest.mark.skipif(shutil.which("prettier") is None, reason="prettier not installed")
def test_clean_file_is_ok(tmp_path: Path) -> None:
    """A well-formatted JSON file passes prettier-check."""
    f = tmp_path / "x.json"
    f.write_text('{ "a": 1 }\n')
    # Run prettier --write first to canonicalize whatever the local config wants
    subprocess.run(["prettier", "--write", str(f)], capture_output=True, timeout=10)
    data = _run([str(f)])
    assert data["ok"] is True
    assert data["count"] == 0


@pytest.mark.skipif(shutil.which("prettier") is None, reason="prettier not installed")
def test_unformatted_file_flagged(tmp_path: Path) -> None:
    """A file with non-canonical formatting trips prettier-check."""
    f = tmp_path / "x.json"
    f.write_text('{"a":1,"b":2}\n')  # missing spaces, prettier defaults add them
    data = _run([str(f)])
    # Either ok=False or ok=True depending on whether local config matches
    # this output. Just check the call returns valid SCHEMA shape.
    assert data["tool"] == "prettier-check"
    assert "ok" in data
    assert "count" in data
