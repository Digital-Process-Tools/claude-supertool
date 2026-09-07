"""Tests for the pyright validator adapter."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _winenv import empty_path_env
from _adapter_verdict import assert_declined, assert_ok

ADAPTER = Path(__file__).parent.parent / "validators" / "pyright" / "pyright.py"


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
    """Absent pyright is `skipped`, not `ok: true` (#1202).

    Escalation under `$SUPERTOOL_REQUIRE_VALIDATORS` is asserted in
    `tests/test_validators_absent_tool_third_state_1202.py`.
    """
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
    assert "pyright" in out["skipped"]
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

@pytest.mark.skipif(not shutil.which("pyright"), reason="pyright not on PATH")
def test_output_schema_present(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x: int = 1\n")
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out
    assert out["tool"] == "pyright"


@pytest.mark.skipif(not shutil.which("pyright"), reason="pyright not on PATH")
def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x: int = 1\n")
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)


# ---------------------------------------------------------------------------
# Real pyright runs (only when pyright is available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("pyright"), reason="pyright not on PATH")
def test_clean_py(tmp_path: Path) -> None:
    f = tmp_path / "good.py"
    f.write_text("x: int = 42\nprint(x)\n")
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0


@pytest.mark.skipif(not shutil.which("pyright"), reason="pyright not on PATH")
def test_type_error_reported(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    # int annotation but assigned a str — pyright catches it.
    f.write_text("x: int = 'not a number'\nprint(x)\n")
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] >= 1
    err = out["errors"][0]
    # pyright 0-indexed range.start converted to 1-indexed line/col.
    assert err["line"] is not None and err["line"] >= 1
    assert err["col"] is not None and err["col"] >= 1
    assert err["severity"] in ("error", "warning")
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    assert len(err["source_context"]) > 0


@pytest.mark.skipif(not shutil.which("pyright"), reason="pyright not on PATH")
def test_info_and_hint_severities_dropped(tmp_path: Path) -> None:
    """Only 'error' and 'warning' diagnostics should appear in the output —
    pyright's 'information' / 'hint' levels are filtered out by the adapter."""
    f = tmp_path / "good.py"
    f.write_text("x: int = 42\nprint(x)\n")
    out = _run(str(f))
    for err in out["errors"]:
        assert err["severity"] in ("error", "warning")


# ---------------------------------------------------------------------------
# Flag-shaped filename (#2379) — pyright does not honor `--` at all
# ---------------------------------------------------------------------------

def _adapter_module():
    """The adapter as a module, to inspect the argv/target it builds without
    needing pyright installed — mirrors mypy's own `_adapter_module` helper
    (test_mypy.py, #2375)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("pyright_adapter_2379", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dashdash_separator_does_not_protect_pyright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike mypy, pyright's own CLI does not honor `--` as an
    end-of-options marker at all — `pyright --outputjson -- --outputjson`
    still errors `Unexpected option outputjson` (exit 4), measured against a
    real installed pyright 2.x/1.1.409 binary. So this adapter must NOT rely
    on a `--` separator the way mypy.py does; it must contain the target
    before it ever reaches pyright's argv, the same shape tsc-check.py uses
    (docs/validators.md, "tsc-check — a program verdict")."""
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
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/pyright")
    monkeypatch.setattr(mod.sys, "argv", ["pyright.py", "--outputjson"])
    mod.main()

    cmd = captured["cmd"]
    assert "--" not in cmd, (
        f"pyright does not honor `--` as a separator; relying on one is "
        f"not a fix: {cmd}")
    # cmd[0] is the binary, cmd[1] is the deliberate `--outputjson` flag this
    # adapter always passes — the target is cmd[2:]. It must not appear bare
    # as the flag-shaped string itself; it must be contained (e.g. prefixed
    # with `./`) so pyright's own option parser cannot read it as a flag.
    target_args = cmd[2:]
    assert "--outputjson" not in target_args, (
        f"the flag-shaped filename must be contained (e.g. relative-"
        f"prefixed) before being handed to pyright, not passed bare: {cmd}")
    assert target_args, f"the target is missing from the command: {cmd}"


@pytest.mark.skipif(not shutil.which("pyright"), reason="pyright not on PATH")
def test_flag_shaped_filename_is_type_checked_not_misparsed(
    tmp_path: Path,
) -> None:
    """A real file named like a pyright option (e.g. `--outputjson`), passed
    as a RELATIVE path (an absolute path is already unambiguous — the bug is
    specifically about a bare flag-shaped relative operand), must still be
    type-checked, not misparsed as an option by pyright's own CLI (#2379,
    the same missing-boundary shape as #2375's mypy fix — but `--` does not
    work here, so this must use a different containment)."""
    f = tmp_path / "--outputjson"
    f.write_text("x: int = 'not a number'\nprint(x)\n")
    result = subprocess.run(
        [sys.executable, str(ADAPTER), "--outputjson"],
        capture_output=True, text=True, cwd=str(tmp_path),
        encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert "skipped" not in out, out
    assert_declined(out)
    assert out["count"] >= 1, (
        "pyright must have actually type-checked the flag-shaped file and "
        f"reported the type error in it: {out}")
    err = out["errors"][0]
    assert err["code"] != "adapter", (
        "the target was misparsed as a pyright option instead of being "
        f"type-checked — this is the adapter-level 'Unexpected option' "
        f"crash, not a real type-check finding: {out}")
    assert err["line"] is not None and err["line"] >= 1, (
        f"a real pyright finding carries a line number; a null line means "
        f"pyright never actually looked at the file's contents: {out}")
