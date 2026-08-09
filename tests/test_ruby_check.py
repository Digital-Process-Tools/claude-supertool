"""Tests for the ruby-check validator adapter."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_budget import adapter_budget
from _winenv import empty_path_env
from _adapter_verdict import assert_declined, assert_ok, verdict

ADAPTER = Path(__file__).parent.parent / "validators" / "ruby-check" / "ruby-check.py"

# In-process import (not via _run's subprocess spawn) so subprocess.run inside
# the adapter can be monkeypatched, to reproduce a broken/aliased "ruby" on
# PATH without depending on one actually existing.
_spec = importlib.util.spec_from_file_location("ruby_check_adapter", ADAPTER)
assert _spec is not None and _spec.loader is not None
ruby_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ruby_check)


def _run(file_path: str) -> dict:
    # Windows runners occasionally yield empty stdout from the freshly-spawned
    # adapter (cold subprocess start); retry once, then fail with diagnostics
    # instead of a cryptic JSONDecodeError.
    for _attempt in range(2):
        result = subprocess.run(
            [sys.executable, str(ADAPTER), file_path],
            capture_output=True,
            text=True,
            timeout=adapter_budget(ADAPTER), encoding="utf-8", errors="replace",
        )
        if result.stdout.strip():
            return verdict(result, adapter="ruby-check")
    raise AssertionError(
        f"ruby-check adapter produced empty stdout (rc={result.returncode}); "
        f"stderr={result.stderr!r}"
    )


def _assert_clean(out: dict) -> None:
    """Assert the adapter found nothing, and say what it found when it did.

    `assert out["ok"] is True` was the whole assertion, and on the one Windows
    leg where it fired it disclosed exactly nothing (#658): `assert False is
    True`. Triaging that needed two unrelated phplint tracebacks in the same
    report to guess at a cause, and the guess was never confirmed. The
    adapter always emits its reason in `errors` — this puts it where the
    reader is, so the next occurrence identifies itself instead of needing
    company.

    #716 wrote that rendering here, by hand, for this file. #725 then found
    the same bare assertion shipped in a file created by the same PR, so the
    rendering moved to `_adapter_verdict` where any adapter test can reach it
    — including the ones that do not exist yet. This wrapper stays for the
    file-specific wording.
    """
    assert_ok(out, context="a Ruby file with nothing wrong with it")


# ---------------------------------------------------------------------------
# Tool missing — graceful degrade
# ---------------------------------------------------------------------------

def test_missing_tool_is_the_third_state(tmp_path: Path) -> None:
    """Absent ruby is `skipped`, not `ok: true` (#1202).

    Escalation under `$SUPERTOOL_REQUIRE_VALIDATORS` is asserted in
    `tests/test_validators_absent_tool_third_state_1202.py`.
    """
    f = tmp_path / "hello.rb"
    f.write_text('puts "hello"\n')
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True,
        text=True,
        env=empty_path_env(), encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert "skipped" in out, out
    assert "ruby" in out["skipped"]
    assert "ok" not in out, out


# ---------------------------------------------------------------------------
# Valid Ruby (only when ruby available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
def test_valid_ruby(tmp_path: Path) -> None:
    f = tmp_path / "good.rb"
    f.write_text('def hello\n  puts "hello"\nend\n')
    out = _run(str(f))
    _assert_clean(out)
    assert out["count"] == 0
    assert out["tool"] == "ruby-check"


@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
def test_valid_ruby_class(tmp_path: Path) -> None:
    f = tmp_path / "cls.rb"
    f.write_text("class Foo\n  def bar\n    42\n  end\nend\n")
    out = _run(str(f))
    _assert_clean(out)


# ---------------------------------------------------------------------------
# Invalid Ruby (only when ruby available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
def test_invalid_ruby_syntax(tmp_path: Path) -> None:
    f = tmp_path / "bad.rb"
    f.write_text("def foo\n  puts 'unclosed\nend\n")
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] >= 1
    assert len(out["errors"]) >= 1


@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
def test_invalid_ruby_error_has_line(tmp_path: Path) -> None:
    f = tmp_path / "bad.rb"
    f.write_text("class Foo\n  def bar\n    end\n")  # missing class end
    out = _run(str(f))
    assert_declined(out)
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
        text=True, encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert_declined(out)
    assert out["errors"][0]["code"] == "adapter"


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
def test_output_contains_required_fields(tmp_path: Path) -> None:
    f = tmp_path / "x.rb"
    f.write_text('puts "hi"\n')
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out


@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
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
    assert_declined(out)


@pytest.mark.skipif(not shutil.which("ruby"), reason="ruby not on PATH")
def test_source_context_present_on_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.rb"
    f.write_text("class Foo\n  def bar\n    end\n")
    out = _run(str(f))
    assert_declined(out)
    err = out["errors"][0]
    assert err["line"] is not None
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    assert len(err["source_context"]) > 0


# ---------------------------------------------------------------------------
# "ruby" resolved but did not execute usefully — the checker-cannot-run class
# ---------------------------------------------------------------------------

def test_unexplained_nonzero_exit_with_empty_stderr_is_a_named_error(monkeypatch, tmp_path, capsys) -> None:
    """`shutil.which('ruby')` succeeding is not proof the spawned process runs
    like ruby -c. On a machine where the resolved name is a shim/alias that
    exits non-zero without printing anything (the class this repo has hit
    repeatedly for `python3` on Windows: #529/#559/#564/#572), the adapter
    must not fold that into `ok: False, count: 0, errors: []` — a "finding"
    that names nothing is the same defect `validators/phpstan/phpstan.py`
    (#263) and `validators/common/refusal.py` already exist to prevent one
    layer over: a checker that could not run must say so, never emit a
    finding-shaped receipt with no finding in it.
    """
    f = tmp_path / "good.rb"
    f.write_text('def hello\n  puts "hello"\nend\n')

    monkeypatch.setattr(ruby_check.shutil, "which", lambda name: "/usr/bin/ruby")
    monkeypatch.setattr(
        ruby_check.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["ruby", "-c", str(f)], returncode=1, stdout="", stderr=""
        ),
    )
    monkeypatch.setattr(sys, "argv", ["ruby-check.py", str(f)])

    ruby_check.main()
    out = json.loads(capsys.readouterr().out)

    assert_declined(out)
    assert out["count"] >= 1, "an unexplained non-zero exit must be a named error, not a silent finding of nothing"
    assert out["errors"], "errors must not be empty when ok is False"
    assert out["errors"][0]["code"] == "adapter"
    assert "1" in out["errors"][0]["msg"]
