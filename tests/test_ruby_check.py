"""Tests for the ruby-check validator adapter."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _winenv import empty_path_env

ADAPTER = Path(__file__).parent.parent / "validators" / "ruby-check" / "ruby-check.py"


def _run(file_path: str) -> dict:
    # Windows runners occasionally yield empty stdout from the freshly-spawned
    # adapter (cold subprocess start); retry once, then fail with diagnostics
    # instead of a cryptic JSONDecodeError.
    for attempt in range(2):
        result = subprocess.run(
            [sys.executable, str(ADAPTER), file_path],
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            return json.loads(result.stdout)
    raise AssertionError(
        f"ruby-check adapter produced empty stdout (rc={result.returncode}); "
        f"stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Tool missing — graceful degrade
# ---------------------------------------------------------------------------

def test_missing_tool_graceful(tmp_path: Path) -> None:
    """When ruby is not on PATH, exit 0 with ok=True and a stderr warning."""
    f = tmp_path / "hello.rb"
    f.write_text('puts "hello"\n')
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True,
        text=True,
        env=empty_path_env(),
    )
    out = json.loads(result.stdout)
    assert out["ok"] is True
    assert out["count"] == 0
    assert "ruby" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Valid Ruby (only when ruby available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
def test_valid_ruby(tmp_path: Path) -> None:
    f = tmp_path / "good.rb"
    f.write_text('def hello\n  puts "hello"\nend\n')
    out = _run(str(f))
    assert out["ok"] is True
    assert out["count"] == 0
    assert out["tool"] == "ruby-check"


@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
def test_valid_ruby_class(tmp_path: Path) -> None:
    f = tmp_path / "cls.rb"
    f.write_text("class Foo\n  def bar\n    42\n  end\nend\n")
    out = _run(str(f))
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# Invalid Ruby (only when ruby available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
def test_invalid_ruby_syntax(tmp_path: Path) -> None:
    f = tmp_path / "bad.rb"
    f.write_text("def foo\n  puts 'unclosed\nend\n")
    out = _run(str(f))
    assert out["ok"] is False
    assert out["count"] >= 1
    assert len(out["errors"]) >= 1


@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
def test_invalid_ruby_error_has_line(tmp_path: Path) -> None:
    f = tmp_path / "bad.rb"
    f.write_text("class Foo\n  def bar\n    end\n")  # missing class end
    out = _run(str(f))
    assert out["ok"] is False
    err = out["errors"][0]
    assert err["line"] is not None
    assert err["severity"] == "error"
    assert err["code"] == "syntax"
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
    f = tmp_path / "x.rb"
    f.write_text('puts "hi"\n')
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out


def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.rb"
    f.write_text('puts "hi"\n')
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
def test_missing_file_returns_error(tmp_path: Path) -> None:
    out = _run(str(tmp_path / "nonexistent.rb"))
    assert out["ok"] is False


@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
def test_source_context_present_on_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.rb"
    f.write_text("class Foo\n  def bar\n    end\n")
    out = _run(str(f))
    assert out["ok"] is False
    err = out["errors"][0]
    assert err["line"] is not None
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    assert len(err["source_context"]) > 0
