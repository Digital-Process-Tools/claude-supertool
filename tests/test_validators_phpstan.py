"""Smoke tests for validators/phpstan/phpstan.py."""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from _adapter_budget import adapter_budget
from _adapter_verdict import assert_declined, assert_ok

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
            r = subprocess.run([sys.executable, str(PHPSTAN_PY), str(f)],
                               capture_output=True, text=True, timeout=adapter_budget(PHPSTAN_PY), encoding="utf-8", errors="replace")
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
    r = subprocess.run([sys.executable, str(PHPSTAN_PY)], capture_output=True, text=True, timeout=adapter_budget(PHPSTAN_PY), encoding="utf-8", errors="replace")
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpstan"
    assert_declined(data)
    assert "no file arg" in data["errors"][0]["msg"]


@pytest.mark.skipif(not _phpstan_emits_schema_json(), reason=_PHPSTAN_SKIP_REASON)
def test_phpstan_clean_php(tmp_path: Path) -> None:
    """Valid PHP with no errors → ok=True, count=0."""
    f = tmp_path / "ok.php"
    f.write_text("<?php\n$x = 1;\n")
    r = subprocess.run(
        [sys.executable, str(PHPSTAN_PY), str(f)],
        capture_output=True, text=True, timeout=adapter_budget(PHPSTAN_PY), encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpstan"
    assert_ok(data)
    assert data["count"] == 0
    assert isinstance(data["errors"], list)
    assert isinstance(data["duration_ms"], int)


@pytest.mark.skipif(not _phpstan_emits_schema_json(), reason=_PHPSTAN_SKIP_REASON)
def test_phpstan_reports_errors(tmp_path: Path) -> None:
    """PHP with obvious type errors → ok=False, count>0, errors populated."""
    f = tmp_path / "bad.php"
    f.write_text("<?php\nfunction foo(): int { return 'not an int'; }\n")
    r = subprocess.run(
        [sys.executable, str(PHPSTAN_PY), str(f)],
        capture_output=True, text=True, timeout=adapter_budget(PHPSTAN_PY), encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpstan"
    assert_declined(data)
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
        [sys.executable, str(PHPSTAN_PY), str(f)],
        capture_output=True, text=True, timeout=adapter_budget(PHPSTAN_PY),
        env=full_env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpstan"
    assert_declined(data)
    assert "PHPSTAN_BIN not found" in data["errors"][0]["msg"]
    assert "errors" in data


def test_php_missing_binary_emits_json(tmp_path: Path) -> None:
    """#2605 regression: if `php` itself cannot be resolved, the adapter
    must decline with a descriptive `adapter` error -- the same third
    state `test_phpstan_missing_binary_emits_json` already proves for
    `phpstan_bin` -- rather than ever reaching `subprocess.run(["php", ...])`
    with the bare, unresolved literal (#2605's own defect: `php` was never
    gated or resolved through `spawnable()`/`argv0()` at all).

    `PHPSTAN_BIN` is pointed at a real, absolute-path executable so the
    *earlier* gate (`spawnable(phpstan_bin)`) passes cleanly and this test
    exercises only the new `php` gate, not the pre-existing one.

    The dummy binary's own name is platform-gated rather than a bare
    extension-less `phpstan_dummy` on every OS: `shutil.which()`'s Windows
    branch applies PATHEXT filtering even to a dirname-bearing (absolute)
    path (`cpython/Lib/shutil.py`, the `sys.platform == "win32"` block runs
    unconditionally after the dirname branch) and only inserts a direct,
    no-extension match when the name already ends in a PATHEXT extension --
    so an extension-less file that plainly exists and is `chmod`'d
    executable is still invisible to `spawnable(phpstan_bin)` on Windows,
    and this test's *earlier* gate failed with "PHPSTAN_BIN not found"
    before ever reaching the `php` gate it means to exercise (observed on
    windows-latest/3.12, #2605 CI). A `.cmd` suffix is in the default
    PATHEXT list and is spawnable outright on Windows; POSIX needs no
    extension and keeps the executable-bit shim.

    `PATH` is pointed at a real, guaranteed-nonexistent directory rather
    than merely one with nothing copied into it, so `php` cannot resolve
    regardless of what else this runner happens to have installed
    (deterministic per the docstring above, not "probably absent").
    """
    f = tmp_path / "ok.php"
    f.write_text("<?php\n$x = 1;\n")
    if os.name == "nt":
        dummy_phpstan = tmp_path / "phpstan_dummy.cmd"
        dummy_phpstan.write_text("@echo off\r\n")
    else:
        dummy_phpstan = tmp_path / "phpstan_dummy"
        dummy_phpstan.write_text("#!/bin/sh\n:\n")
        dummy_phpstan.chmod(0o755)
    empty_path_dir = tmp_path / "empty-bin-2605-does-not-exist"
    env = {**os.environ,
           "PATH": str(empty_path_dir),
           "PHPSTAN_BIN": str(dummy_phpstan)}
    r = subprocess.run(
        [sys.executable, str(PHPSTAN_PY), str(f)],
        capture_output=True, text=True, timeout=adapter_budget(PHPSTAN_PY),
        env=env, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["tool"] == "phpstan"
    assert_declined(data)
    assert data["errors"][0]["msg"] == "php not found"
    assert "errors" in data


# ---------------------------------------------------------------------------
# Hermetic adapter coverage — fake `php` shim emitting canned phpstan JSON.
# Exercises the parse/aggregate path with no real phpstan/php, so coverage holds
# in envs where the behavioral tests above skip (global phpstan can't emit JSON).
# ---------------------------------------------------------------------------

def _run_adapter_with_fake_php(tmp_path: Path, php_stdout: str) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_php = bindir / "php"
    fake_php.write_text("#!/bin/sh\ncat <<'JSON'\n" + php_stdout + "\nJSON\n")
    fake_php.chmod(0o755)
    dummy_bin = bindir / "phpstan"
    dummy_bin.write_text("#!/bin/sh\n:\n")
    dummy_bin.chmod(0o755)
    target = tmp_path / "x.php"
    target.write_text("<?php\n$x = 1;\n")
    env = {**os.environ,
           "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
           "PHPSTAN_BIN": str(dummy_bin)}
    r = subprocess.run([sys.executable, str(PHPSTAN_PY), str(target)],
                       capture_output=True, text=True, timeout=adapter_budget(PHPSTAN_PY), env=env, encoding="utf-8", errors="replace")
    assert r.returncode == 0
    return json.loads(r.stdout.strip())


@pytest.mark.skipif(os.name == "nt", reason="POSIX /bin/sh shim")
def test_phpstan_clean_via_fake_php(tmp_path: Path) -> None:
    data = _run_adapter_with_fake_php(tmp_path, '{"totals": {"file_errors": 0}, "files": {}}')
    assert data["tool"] == "phpstan"
    assert_ok(data)
    assert data["count"] == 0
    assert data["errors"] == []
    assert isinstance(data["duration_ms"], int)


@pytest.mark.skipif(os.name == "nt", reason="POSIX /bin/sh shim")
def test_phpstan_errors_via_fake_php(tmp_path: Path) -> None:
    payload = json.dumps({
        "totals": {"file_errors": 1},
        "files": {"x.php": {"messages": [
            {"line": 2, "identifier": "return.type", "message": "bad return"}]}},
    })
    data = _run_adapter_with_fake_php(tmp_path, payload)
    assert_declined(data)
    assert data["count"] == 1
    assert len(data["errors"]) == 1
    err = data["errors"][0]
    assert err["line"] == 2
    assert err["code"] == "return.type"
    assert err["msg"] == "bad return"
    assert "source_context" in err


@pytest.mark.skipif(os.name == "nt", reason="POSIX /bin/sh shim")
def test_phpstan_non_json_output_via_fake_php(tmp_path: Path) -> None:
    data = _run_adapter_with_fake_php(tmp_path, "PHP Fatal error: boom")
    assert_declined(data)
    assert data["errors"][0]["code"] == "adapter"
    assert "not json" in data["errors"][0]["msg"]
