"""Smoke tests for formatters/prettier-write/prettier-write.py."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import shlex
import sys
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "formatters" / "prettier-write" / "prettier-write.py"


def _python_stub(tmp_path: Path, name: str, body: str) -> str:
    """Create a Python stub file and return a `python <path>` command line
    suitable for PRETTIER_BIN (the adapter shlex-splits the env var).
    """
    stub = tmp_path / f"{name}.py"
    stub.write_text(body)
    return f"{shlex.quote(sys.executable)} {shlex.quote(stub.as_posix())}"


def test_no_arg_returns_schema_error() -> None:
    r = subprocess.run([sys.executable, str(ADAPTER)], capture_output=True, text=True, timeout=10)
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
        [sys.executable, str(ADAPTER), str(f)],
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

    bin_cmd = _python_stub(tmp_path, "stub_exit0", "import sys; sys.exit(0)\n")
    env = {**os.environ, "PRETTIER_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
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
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "prettier-write"
    assert data["ok"] is True


def test_file_needing_format_via_stub(tmp_path: Path) -> None:
    f = tmp_path / "x.js"
    f.write_text("const x=1\n")

    body = (
        "import sys, pathlib\n"
        f"pathlib.Path(r'{f.as_posix()}').write_text('const x = 1\\nconst y = 2\\n')\n"
        "sys.exit(0)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_add_line", body)
    env = {**os.environ, "PRETTIER_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "prettier-write"
    assert data["ok"] is True
    total_changes = data["metrics"]["lines_added"] + data["metrics"]["lines_removed"]
    assert total_changes > 0
