"""Tests for the mypy validator adapter."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _winenv import empty_path_env
from _adapter_verdict import assert_declined, assert_ok

ADAPTER = Path(__file__).parent.parent / "validators" / "mypy" / "mypy.py"


def _run(file_path: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(ADAPTER), file_path],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Tool missing — graceful degrade
# ---------------------------------------------------------------------------

def test_missing_tool_is_the_third_state(tmp_path: Path) -> None:
    """Absent mypy is `skipped`, not `ok: true` (#1202)."""
    f = tmp_path / "hello.py"
    f.write_text("x: int = 1\n")
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True,
        text=True,
        env=empty_path_env(), encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert "skipped" in out, out
    assert "mypy" in out["skipped"]
    assert "ok" not in out, out


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

@pytest.mark.skipif(not shutil.which("mypy"), reason="mypy not on PATH")
def test_output_schema_present(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x: int = 1\n")
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out
    assert out["tool"] == "mypy"


@pytest.mark.skipif(not shutil.which("mypy"), reason="mypy not on PATH")
def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x: int = 1\n")
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)


# ---------------------------------------------------------------------------
# Real mypy runs (only when mypy is available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("mypy"), reason="mypy not on PATH")
def test_clean_py(tmp_path: Path) -> None:
    f = tmp_path / "good.py"
    f.write_text("x: int = 42\nprint(x)\n")
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0


@pytest.mark.skipif(not shutil.which("mypy"), reason="mypy not on PATH")
def test_type_error_reported(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    f.write_text("x: int = 'not a number'\nprint(x)\n")
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] >= 1
    err = out["errors"][0]
    assert err["line"] is not None and err["line"] >= 1
    assert err["col"] is not None and err["col"] >= 1
    assert err["severity"] == "error"
    assert err["code"] == "assignment"
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    assert len(err["source_context"]) > 0


@pytest.mark.skipif(not shutil.which("mypy"), reason="mypy not on PATH")
def test_multiple_errors_all_reported(tmp_path: Path) -> None:
    f = tmp_path / "twoerr.py"
    f.write_text("x: int = 'a'\ny: str = 5\n")
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 2


@pytest.mark.skipif(not shutil.which("mypy"), reason="mypy not on PATH")
def test_no_cache_dir_written(tmp_path: Path) -> None:
    """mypy defaults to writing .mypy_cache into its cwd — disabled (#669)."""
    f = tmp_path / "good.py"
    f.write_text("x: int = 42\n")
    _run(str(f))
    assert not (Path.cwd() / ".mypy_cache").exists()
    assert not (tmp_path / ".mypy_cache").exists()


@pytest.mark.skipif(not shutil.which("mypy"), reason="mypy not on PATH")
def test_nonexistent_file_is_declined_not_crashed(tmp_path: Path) -> None:
    """mypy's own 'cannot read file' text on stdout is not JSON — the adapter
    must not crash trying to json.loads() it; it must report an adapter-level
    finding instead."""
    missing = tmp_path / "does_not_exist.py"
    out = _run(str(missing))
    assert_declined(out)
    assert out["errors"][0]["code"] == "adapter"


# ---------------------------------------------------------------------------
# `--` separator ahead of the file argument (#2375)
# ---------------------------------------------------------------------------

def _adapter_module():
    """The adapter as a module, to inspect the argv it builds without
    needing mypy installed — mirrors ruff's own `_adapter_module` helper
    (test_validators_ruff.py), used there for the same reason: the arm under
    test is about the argv this adapter constructs, not about mypy's own
    output."""
    spec = importlib.util.spec_from_file_location("mypy_adapter_2375", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_flag_shaped_filename_gets_a_separator_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`file` is argv[1], so mypy needs a `--` before it, same as every
    sibling adapter (ruff, actionlint, git-status, new-file-lint). Without
    the separator, a flag-shaped path is read by mypy's own option parser
    instead of being treated as a positional file argument — the `splices`
    class: the callee's parser decides what the value means, not this
    adapter.

    `subprocess.run` is captured rather than exercised for real, the same
    way ruff's own `_Probe`-based tests avoid needing ruff installed —
    mypy is not on PATH in this environment either.
    """
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class _Result:
            stdout = ""
            stderr = ""
            returncode = 0

        return _Result()

    mod = _adapter_module()
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/mypy")
    monkeypatch.setattr(mod.sys, "argv", ["mypy.py", "--python-version=3.9"])
    mod.main()

    cmd = captured["cmd"]
    assert "--python-version=3.9" in cmd, cmd
    idx = cmd.index("--python-version=3.9")
    assert cmd[idx - 1] == "--", (
        "the file argument must be immediately preceded by a `--` "
        f"separator so mypy's own option parser cannot consume it: {cmd}")
