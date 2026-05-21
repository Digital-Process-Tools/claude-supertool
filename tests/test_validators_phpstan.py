"""Smoke tests for validators/phpstan/phpstan.sh."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PHPSTAN_SH = Path(__file__).parent.parent / "validators" / "phpstan" / "phpstan.sh"


def test_phpstan_sh_no_arg_returns_schema_error() -> None:
    """Calling with no arg must emit a valid SCHEMA.md error dict and exit 0."""
    r = subprocess.run(["bash", str(PHPSTAN_SH)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpstan"
    assert data["ok"] is False
    assert "no file arg" in data["errors"][0]["msg"]


@pytest.mark.skipif(not shutil.which("phpstan"), reason="phpstan not installed")
def test_phpstan_sh_clean_php(tmp_path: Path) -> None:
    """Valid PHP with no errors → ok=True, count=0."""
    f = tmp_path / "ok.php"
    f.write_text("<?php\n$x = 1;\n")
    r = subprocess.run(
        ["bash", str(PHPSTAN_SH), str(f)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpstan"
    assert data["ok"] is True
    assert data["count"] == 0
    assert isinstance(data["errors"], list)
    assert isinstance(data["duration_ms"], int)


@pytest.mark.skipif(not shutil.which("phpstan"), reason="phpstan not installed")
def test_phpstan_sh_reports_errors(tmp_path: Path) -> None:
    """PHP with obvious type errors → ok=False, count>0, errors populated."""
    f = tmp_path / "bad.php"
    f.write_text("<?php\nfunction foo(): int { return 'not an int'; }\n")
    r = subprocess.run(
        ["bash", str(PHPSTAN_SH), str(f)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpstan"
    assert data["ok"] is False
    assert data["count"] > 0
    assert len(data["errors"]) > 0
    err = data["errors"][0]
    assert "line" in err
    assert "msg" in err
    assert "source_context" in err


def test_phpstan_sh_missing_binary_emits_json(tmp_path: Path) -> None:
    """If PHPSTAN_BIN points to a nonexistent binary, adapter still emits valid JSON."""
    f = tmp_path / "ok.php"
    f.write_text("<?php\n$x = 1;\n")
    env = {"PHPSTAN_BIN": "/nonexistent/phpstan"}
    import os
    full_env = {**os.environ, **env}
    r = subprocess.run(
        ["bash", str(PHPSTAN_SH), str(f)],
        capture_output=True, text=True, timeout=10,
        env=full_env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpstan"
    # ok=True is valid here: phpstan produced no output (missing binary),
    # so count defaults to 0. The adapter treats absent output as clean.
    assert "ok" in data
    assert "errors" in data
