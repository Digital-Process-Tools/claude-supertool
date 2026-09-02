"""Smoke tests for formatters/ruff-format/ruff-format.py (#2085)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import shlex
import sys
from pathlib import Path

import pytest
from _adapter_verdict import assert_declined, assert_ok

ADAPTER = Path(__file__).parent.parent / "formatters" / "ruff-format" / "ruff-format.py"


def _python_stub(tmp_path: Path, name: str, body: str) -> str:
    """Create a Python stub file and return a `python <path>` command line
    suitable for RUFF_BIN (the adapter shlex-splits the env var).
    """
    stub = tmp_path / f"{name}.py"
    stub.write_text(body)
    return f"{shlex.quote(sys.executable)} {shlex.quote(stub.as_posix())}"


def test_no_arg_returns_schema_error() -> None:
    r = subprocess.run([sys.executable, str(ADAPTER)], capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_declined(data)
    assert "no file arg" in data["errors"][0]["msg"]


def test_missing_binary_returns_schema_error(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x=1\n")
    env = {**os.environ, "RUFF_BIN": "ruff-that-does-not-exist-xyz"}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_declined(data)
    assert "not found" in data["errors"][0]["msg"]
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


def test_clean_file_ok_noop_via_stub(tmp_path: Path) -> None:
    """Stub ruff that exits 0 without touching the file -> ok=True, metrics 0/0."""
    f = tmp_path / "x.py"
    content = "x = 1\n"
    f.write_text(content)

    bin_cmd = _python_stub(tmp_path, "stub_exit0", "import sys; sys.exit(0)\n")
    env = {**os.environ, "RUFF_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_ok(data)
    assert data["metrics"]["lines_added"] == 0
    assert data["metrics"]["lines_removed"] == 0


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
def test_live_clean_file_ok(tmp_path: Path) -> None:
    """Live ruff on an already-formatted file -> ok=True."""
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_ok(data)


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
def test_live_file_needing_format_gets_reformatted(tmp_path: Path) -> None:
    """A badly-spaced file is actually rewritten, and metrics say so."""
    f = tmp_path / "x.py"
    f.write_text("x=1\ny  =   2\n")
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_ok(data)
    total_changes = data["metrics"]["lines_added"] + data["metrics"]["lines_removed"]
    assert total_changes > 0
    assert f.read_text(encoding="utf-8") != "x=1\ny  =   2\n"


def test_file_needing_format_via_stub(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x=1\n")

    body = (
        "import sys, pathlib\n"
        f"pathlib.Path(r'{f.as_posix()}').write_text('x = 1\\ny = 2\\n')\n"
        "sys.exit(0)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_add_line", body)
    env = {**os.environ, "RUFF_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_ok(data)
    total_changes = data["metrics"]["lines_added"] + data["metrics"]["lines_removed"]
    assert total_changes > 0


def test_syntax_error_file_reports_failure_not_ok(tmp_path: Path) -> None:
    """A file ruff cannot parse must fail loudly, never a silent ok=True no-op --
    the bar every 'would this test still pass if the code did nothing' check
    needs: ok=True with 0/0 metrics is EXACTLY what a no-op stub also returns,
    so this is the one case that tells a real failure from an adapter that
    never actually asked the tool anything.
    """
    f = tmp_path / "x.py"
    f.write_text("def f(:\n    pass\n")

    body = (
        "import sys\n"
        "sys.stderr.write('error: failed to parse\\n')\n"
        "sys.exit(2)\n"
    )
    bin_cmd = _python_stub(tmp_path, "stub_parse_error", body)
    env = {**os.environ, "RUFF_BIN": bin_cmd}
    r = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True, text=True, timeout=10, env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "ruff-format"
    assert_declined(data)
    assert data["count"] == 1
    assert "failed to parse" in data["errors"][0]["msg"]
