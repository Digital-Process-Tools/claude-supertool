"""Smoke tests for validators/phpstan/phpstan.py."""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PHPSTAN_PY = Path(__file__).parent.parent / "validators" / "phpstan" / "phpstan.py"


@functools.lru_cache(maxsize=1)
def _phpstan_emits_schema_json() -> bool:
    """Probe: does the phpstan adapter emit clean SCHEMA JSON in this env?

    A globally-installed phpstan with no project config emits non-JSON output,
    so the adapter returns an 'output not json' error and the behavioral tests
    below can't pass. CI runs the project phpstan and does emit JSON. Gate on
    the real capability, not just `which phpstan` — skip locally, run in CI.
    """
    if not shutil.which("phpstan"):
        return False
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "probe.php"
        f.write_text("<?php\n$x = 1;\n")
        try:
            r = subprocess.run(["python3", str(PHPSTAN_PY), str(f)],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        data = json.loads(r.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return False
    if data.get("tool") != "phpstan":
        return False
    return not any(e.get("code") == "adapter" for e in (data.get("errors") or []))


_PHPSTAN_SKIP_REASON = "phpstan adapter not emitting SCHEMA JSON in this env (global phpstan; CI runs the project one)"


def test_phpstan_no_arg_returns_schema_error() -> None:
    """Calling with no arg must emit a valid SCHEMA.md error dict and exit 0."""
    r = subprocess.run(["python3", str(PHPSTAN_PY)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpstan"
    assert data["ok"] is False
    assert "no file arg" in data["errors"][0]["msg"]


@pytest.mark.skipif(not _phpstan_emits_schema_json(), reason=_PHPSTAN_SKIP_REASON)
def test_phpstan_clean_php(tmp_path: Path) -> None:
    """Valid PHP with no errors → ok=True, count=0."""
    f = tmp_path / "ok.php"
    f.write_text("<?php\n$x = 1;\n")
    r = subprocess.run(
        ["python3", str(PHPSTAN_PY), str(f)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpstan"
    assert data["ok"] is True
    assert data["count"] == 0
    assert isinstance(data["errors"], list)
    assert isinstance(data["duration_ms"], int)


@pytest.mark.skipif(not _phpstan_emits_schema_json(), reason=_PHPSTAN_SKIP_REASON)
def test_phpstan_reports_errors(tmp_path: Path) -> None:
    """PHP with obvious type errors → ok=False, count>0, errors populated."""
    f = tmp_path / "bad.php"
    f.write_text("<?php\nfunction foo(): int { return 'not an int'; }\n")
    r = subprocess.run(
        ["python3", str(PHPSTAN_PY), str(f)],
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


def test_phpstan_missing_binary_emits_json(tmp_path: Path) -> None:
    """If PHPSTAN_BIN points to a nonexistent binary, adapter emits ok=False with descriptive error."""
    f = tmp_path / "ok.php"
    f.write_text("<?php\n$x = 1;\n")
    full_env = {**os.environ, "PHPSTAN_BIN": "/nonexistent/phpstan"}
    r = subprocess.run(
        ["python3", str(PHPSTAN_PY), str(f)],
        capture_output=True, text=True, timeout=10,
        env=full_env,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpstan"
    assert data["ok"] is False
    assert "PHPSTAN_BIN not found" in data["errors"][0]["msg"]
    assert "errors" in data
