"""Smoke tests for validators/phpmd/phpmd.py."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_budget import adapter_budget
from _adapter_verdict import assert_declined, assert_ok

PHPMD_PY = Path(__file__).parent.parent / "validators" / "phpmd" / "phpmd.py"


def test_phpmd_no_arg_returns_schema_error() -> None:
    """Calling with no arg must emit a valid SCHEMA.md error dict and exit 0."""
    r = subprocess.run([sys.executable, str(PHPMD_PY)], capture_output=True, text=True, timeout=adapter_budget(PHPMD_PY), encoding="utf-8", errors="replace")
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpmd"
    assert_declined(data)
    assert "no file arg" in data["errors"][0]["msg"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Stub uses bash shebang — not executable on Windows without WSL",
)
def test_phpmd_clean_output_parses_to_ok(tmp_path: Path) -> None:
    """When phpmd produces no output (no violations), adapter emits ok=True."""
    f = tmp_path / "clean.php"
    f.write_text("<?php\n$x = 1;\n")
    # Use a stub phpmd that exits 0 with no output.
    stub = tmp_path / "phpmd"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    stub.chmod(0o755)
    env = {**os.environ, "PHPMD_BIN": str(stub)}
    r = subprocess.run(
        [sys.executable, str(PHPMD_PY), str(f)],
        capture_output=True, text=True, timeout=adapter_budget(PHPMD_PY), env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpmd"
    assert_ok(data)
    assert data["count"] == 0
    assert data["errors"] == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Stub uses bash shebang — not executable on Windows without WSL",
)
def test_phpmd_parses_text_output(tmp_path: Path) -> None:
    """Adapter must parse phpmd text format into SCHEMA.md errors."""
    f = tmp_path / "dirty.php"
    f.write_text("<?php\n$x = 1;\n")
    # Stub phpmd that emits one text-format violation line (real format: file:line\tRule\tMsg).
    violation = f"{f}:3\tUnusedLocalVariable\tAvoid unused local variables."
    stub = tmp_path / "phpmd"
    stub.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' '{violation}'\nexit 2\n")
    stub.chmod(0o755)
    env = {**os.environ, "PHPMD_BIN": str(stub)}
    r = subprocess.run(
        [sys.executable, str(PHPMD_PY), str(f)],
        capture_output=True, text=True, timeout=adapter_budget(PHPMD_PY), env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpmd"
    assert_declined(data)
    assert data["count"] == 1
    err = data["errors"][0]
    assert err["line"] == 3
    assert err["code"] == "UnusedLocalVariable"
    assert "unused" in err["msg"].lower()
    assert "source_context" in err


@pytest.mark.skipif(not shutil.which("phpmd"), reason="phpmd not installed")
def test_phpmd_live_clean_php(tmp_path: Path) -> None:
    """Live phpmd on a clean PHP file → ok=True, count=0."""
    f = tmp_path / "ok.php"
    f.write_text("<?php\nfunction add(int $a, int $b): int { return $a + $b; }\n")
    r = subprocess.run(
        [sys.executable, str(PHPMD_PY), str(f)],
        capture_output=True, text=True, timeout=adapter_budget(PHPMD_PY), encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpmd"
    assert isinstance(data["ok"], bool)
    assert isinstance(data["errors"], list)
    assert isinstance(data["duration_ms"], int)
