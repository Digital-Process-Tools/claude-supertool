"""Smoke tests for formatters/prettier-write/prettier-write.py."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "formatters" / "prettier-write" / "prettier-write.py"


def test_no_arg_returns_schema_error() -> None:
    r = subprocess.run(["python3", str(ADAPTER)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "prettier-write"
    assert data["ok"] is False
    assert "no file arg" in data["errors"][0]["msg"]


def test_missing_binary_returns_schema_error(tmp_path: Path) -> None:
    f = tmp_path / "x.js"
    f.write_text("const x=1\n")
    env = {**os.environ, "PRETTIER_BIN": "prettier-that-does-not-exist-xyz"}
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "prettier-write"
    assert data["ok"] is False
    assert "not found" in data["errors"][0]["msg"]
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


def test_clean_file_ok_noop_via_stub(tmp_path: Path) -> None:
    """Stub prettier that exits 0 without touching the file → ok=True, metrics 0/0."""
    f = tmp_path / "x.json"
    content = '{"a": 1}\n'
    f.write_text(content)

    stub = tmp_path / "prettier"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    stub.chmod(0o755)

    env = {**os.environ, "PRETTIER_BIN": str(stub)}
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "prettier-write"
    assert data["ok"] is True
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


@pytest.mark.skipif(not shutil.which("prettier"), reason="prettier not installed")
def test_live_clean_file_ok(tmp_path: Path) -> None:
    """Live prettier on an already-formatted file → ok=True."""
    f = tmp_path / "x.json"
    # Write content that prettier will not change (already formatted)
    f.write_text('{\n  "a": 1\n}\n')
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "prettier-write"
    assert data["ok"] is True


def test_file_needing_format_via_stub(tmp_path: Path) -> None:
    f = tmp_path / "x.js"
    f.write_text("const x=1\n")

    # Stub that adds a line so metrics are non-zero
    stub = tmp_path / "prettier"
    stub.write_text(
        f"#!/usr/bin/env bash\nprintf 'const x = 1\\nconst y = 2\\n' > {f}\nexit 0\n"
    )
    stub.chmod(0o755)

    env = {**os.environ, "PRETTIER_BIN": str(stub)}
    r = subprocess.run(
        ["python3", str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "prettier-write"
    assert data["ok"] is True
    total_changes = data["metrics"]["lines_added"] + data["metrics"]["lines_removed"]
    assert total_changes > 0
