"""Tests for the yaml-check validator adapter."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "validators" / "yaml-check" / "yaml-check.py"

# Use a Python interpreter that has PyYAML installed, falling back to sys.executable.
# On this machine, python3.13 has PyYAML; python3.14 (pytest default) does not.
_PYTHON_WITH_YAML = shutil.which("python3.13") or sys.executable
_HAS_PYYAML = subprocess.run(
    [_PYTHON_WITH_YAML, "-c", "import yaml"],
    capture_output=True,
).returncode == 0


def _run(file_path: str, python: str = _PYTHON_WITH_YAML) -> tuple[dict, str]:
    result = subprocess.run(
        [python, str(ADAPTER), file_path],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    return json.loads(result.stdout), result.stderr


# ---------------------------------------------------------------------------
# Valid YAML
# ---------------------------------------------------------------------------

def test_valid_yaml_simple(tmp_path: Path) -> None:
    f = tmp_path / "good.yml"
    f.write_text("key: value\nnum: 42\n")
    out, _ = _run(str(f))
    assert out["ok"] is True
    assert out["count"] == 0
    assert out["errors"] == []
    assert out["tool"] == "yaml-check"


def test_valid_yaml_list(tmp_path: Path) -> None:
    f = tmp_path / "list.yml"
    f.write_text("- one\n- two\n- three\n")
    out, _ = _run(str(f))
    assert out["ok"] is True
    assert out["count"] == 0


def test_valid_yaml_nested(tmp_path: Path) -> None:
    f = tmp_path / "nested.yaml"
    f.write_text("stages:\n  - build\n  - test\njobs:\n  build:\n    script: make\n")
    out, _ = _run(str(f))
    assert out["ok"] is True


def test_valid_gitlab_ci_like(tmp_path: Path) -> None:
    f = tmp_path / ".gitlab-ci.yml"
    f.write_text(
        "image: php:8.3\nstages:\n  - test\nphpunit:\n  stage: test\n  script:\n    - phpunit\n"
    )
    out, _ = _run(str(f))
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# Invalid YAML (requires PyYAML)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_PYYAML, reason="PyYAML not available on this interpreter")
def test_invalid_yaml_returns_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.yml"
    f.write_text("key: [\nunclosed bracket\n")
    out, _ = _run(str(f))
    assert out["ok"] is False
    assert out["count"] == 1
    assert len(out["errors"]) == 1


@pytest.mark.skipif(not _HAS_PYYAML, reason="PyYAML not available on this interpreter")
def test_invalid_yaml_has_line_info(tmp_path: Path) -> None:
    f = tmp_path / "bad.yml"
    f.write_text("good: ok\nbad: [\nstill bad\n")
    out, _ = _run(str(f))
    assert out["ok"] is False
    err = out["errors"][0]
    assert err["line"] is not None
    assert err["severity"] == "error"
    assert err["code"] == "syntax"


@pytest.mark.skipif(not _HAS_PYYAML, reason="PyYAML not available on this interpreter")
def test_invalid_yaml_msg_populated(tmp_path: Path) -> None:
    f = tmp_path / "bad.yml"
    f.write_text("key: : invalid\n")
    out, _ = _run(str(f))
    assert out["ok"] is False
    assert out["errors"][0]["msg"]


# ---------------------------------------------------------------------------
# Missing file (requires PyYAML — without it the validator exits 0)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_PYYAML, reason="PyYAML not available on this interpreter")
def test_missing_file_returns_error(tmp_path: Path) -> None:
    out, _ = _run(str(tmp_path / "nonexistent.yml"))
    assert out["ok"] is False
    assert out["count"] == 1
    err = out["errors"][0]
    assert err["code"] == "adapter"
    assert "not found" in err["msg"]


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
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "adapter"


# ---------------------------------------------------------------------------
# PyYAML missing — graceful degrade
# ---------------------------------------------------------------------------

def test_pyyaml_missing_exits_ok_with_warning(tmp_path: Path) -> None:
    """When PyYAML is not installed, validator should exit 0 and warn on stderr."""
    f = tmp_path / "any.yml"
    f.write_text("key: value\n")
    # Patch builtins.__import__ to raise ImportError for 'yaml'
    shim = tmp_path / "yaml_missing_shim.py"
    shim.write_text(
        "import builtins\n"
        "_real = builtins.__import__\n"
        "def _mock(name, *a, **kw):\n"
        "    if name == 'yaml':\n"
        "        raise ImportError('mocked missing')\n"
        "    return _real(name, *a, **kw)\n"
        "builtins.__import__ = _mock\n"
        f"import runpy; runpy.run_path({str(ADAPTER)!r}, run_name='__main__')\n"
    )
    result = subprocess.run(
        [sys.executable, str(shim), str(f)],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert out["ok"] is True
    assert "PyYAML" in result.stderr or "pyyaml" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

def test_output_contains_required_fields(tmp_path: Path) -> None:
    f = tmp_path / "x.yml"
    f.write_text("a: 1\n")
    out, _ = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out


def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.yml"
    f.write_text("a: 1\n")
    out, _ = _run(str(f))
    assert isinstance(out["duration_ms"], int)


@pytest.mark.skipif(not _HAS_PYYAML, reason="PyYAML not available on this interpreter")
def test_source_context_present_on_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.yml"
    f.write_text("good: ok\nbad: [\nstill bad\n")
    out, _ = _run(str(f))
    assert out["ok"] is False
    err = out["errors"][0]
    assert err["line"] is not None
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    assert len(err["source_context"]) > 0
