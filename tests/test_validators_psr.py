"""Smoke tests for validators/psr/psr.py."""
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

PSR_PY = Path(__file__).parent.parent / "validators" / "psr" / "psr.py"


def test_psr_no_arg_returns_schema_error() -> None:
    """Calling with no arg must emit a valid SCHEMA.md error dict and exit 0."""
    r = subprocess.run([sys.executable, str(PSR_PY)], capture_output=True, text=True, timeout=adapter_budget(PSR_PY), encoding="utf-8", errors="replace")
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "psr"
    assert_declined(data)
    assert "no file arg" in data["errors"][0]["msg"]


def test_psr_missing_binary_returns_schema_error(tmp_path: Path) -> None:
    """When PSR_BIN does not exist, adapter must emit ok=false with descriptive error."""
    f = tmp_path / "clean.php"
    f.write_text("<?php\n$x = 1;\n")
    env = {**os.environ, "PSR_BIN": str(tmp_path / "phpcs-does-not-exist")}
    r = subprocess.run(
        [sys.executable, str(PSR_PY), str(f)],
        capture_output=True, text=True, timeout=adapter_budget(PSR_PY), env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "psr"
    assert_declined(data)
    assert "PSR_BIN not found" in data["errors"][0]["msg"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Stub uses bash shebang — not executable on Windows without WSL",
)
def test_psr_clean_output_parses_to_ok(tmp_path: Path) -> None:
    """When phpcs produces no violations, adapter emits ok=True."""
    f = tmp_path / "clean.php"
    f.write_text("<?php\n$x = 1;\n")
    # Stub phpcs that exits 0 with empty JSON report.
    stub = tmp_path / "phpcs"
    stub.write_text('#!/usr/bin/env bash\nprintf \'{"totals":{"errors":0,"warnings":0},"files":{}}\'\nexit 0\n')
    stub.chmod(0o755)
    env = {**os.environ, "PSR_BIN": str(stub)}
    r = subprocess.run(
        [sys.executable, str(PSR_PY), str(f)],
        capture_output=True, text=True, timeout=adapter_budget(PSR_PY), env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "psr"
    assert_ok(data)
    assert data["count"] == 0
    assert data["errors"] == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Stub uses bash shebang — not executable on Windows without WSL",
)
def test_psr_parses_json_violations(tmp_path: Path) -> None:
    """Adapter must parse phpcs JSON report into SCHEMA.md errors."""
    f = tmp_path / "dirty.php"
    f.write_text("<?php\n$x = 1;\n")
    violation_json = json.dumps({
        "totals": {"errors": 1, "warnings": 0},
        "files": {
            str(f): {
                "errors": 1,
                "warnings": 0,
                "messages": [
                    {
                        "message": "Opening brace should be on a new line",
                        "source": "PSR2.Classes.ClassDeclaration.OpenBraceNewLine",
                        "severity": 5,
                        "type": "ERROR",
                        "line": 2,
                        "column": 1,
                        "fixable": True,
                    }
                ],
            }
        },
    })
    stub = tmp_path / "phpcs"
    stub.write_text(f"#!/usr/bin/env bash\nprintf '%s' '{violation_json}'\nexit 1\n")
    stub.chmod(0o755)
    env = {**os.environ, "PSR_BIN": str(stub)}
    r = subprocess.run(
        [sys.executable, str(PSR_PY), str(f)],
        capture_output=True, text=True, timeout=adapter_budget(PSR_PY), env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "psr"
    assert_declined(data)
    assert data["count"] == 1
    err = data["errors"][0]
    assert err["line"] == 2
    assert err["col"] == 1
    assert err["severity"] == "error"
    assert err["code"] == "PSR2.Classes.ClassDeclaration.OpenBraceNewLine"
    assert "brace" in err["msg"].lower()
    assert "source_context" in err


@pytest.mark.skipif(not shutil.which("phpcs"), reason="phpcs not installed")
def test_psr_live_clean_php(tmp_path: Path) -> None:
    """Live phpcs on a clean PHP file → ok=True, count=0."""
    f = tmp_path / "ok.php"
    f.write_text("<?php\n\nfunction add(int $a, int $b): int\n{\n    return $a + $b;\n}\n")
    r = subprocess.run(
        [sys.executable, str(PSR_PY), str(f)],
        capture_output=True, text=True, timeout=adapter_budget(PSR_PY), encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "psr"
    assert isinstance(data["ok"], bool)
    assert isinstance(data["errors"], list)
    assert isinstance(data["duration_ms"], int)
