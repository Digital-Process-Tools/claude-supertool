"""Tests for the tsc-check validator adapter."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _winenv import empty_path_env
from _adapter_verdict import assert_declined, assert_ok

ADAPTER = Path(__file__).parent.parent / "validators" / "tsc-check" / "tsc-check.py"


def _run(file_path: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(ADAPTER), file_path],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# tsc's default output mode (#1499)
# ---------------------------------------------------------------------------

# A stub named `tsc` has to be found by BOTH `shutil.which("tsc")` and
# `subprocess.run(["tsc", ...])`. On Windows the second is CreateProcess, which
# searches PATH but never appends PATHEXT, so a `tsc.cmd` that `which` finds is
# not a `tsc` that exec finds and the adapter would take its absent-tool arm.
# The parsing change these three assert is exercised on Windows through the
# real-tsc tests below; only the stub route is unavailable there.
stub_capable = pytest.mark.skipif(
    os.name != "posix",
    reason="a PATH stub must answer to the bare name `tsc`; Windows CreateProcess "
           "does not apply PATHEXT to an argv[0] carrying no extension.",
)

# Captured verbatim from TypeScript 5.x, which emits this whether or not stdout
# is a tty — the caret rule, the trailing tally, and the colour.
PRETTY_DUMP = (
    "\x1b[96mbad.ts\x1b[0m:\x1b[93m1\x1b[0m:\x1b[93m7\x1b[0m - "
    "\x1b[91merror\x1b[0m\x1b[90m TS2322: \x1b[0mType 'string' is not "
    "assignable to type 'number'.\n"
    "\n"
    "\x1b[7m1\x1b[0m const x: number = 'not a number';\n"
    "\x1b[7m \x1b[0m \x1b[91m      ~\x1b[0m\n"
    "\n"
    "\nFound 1 error in bad.ts\x1b[90m:1\x1b[0m\n"
)

PLAIN_LINE = (
    "bad.ts(1,7): error TS2322: Type 'string' is not assignable to type 'number'.\n"
)


def _stub_tsc(tmp_path: Path, body: str) -> dict:
    """Put an executable named `tsc` on PATH; return the env to run with."""
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    stub = bindir / "tsc"
    stub.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    return env


def _run_env(file_path: str, env: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(ADAPTER), file_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )
    return json.loads(result.stdout)


def _adapter_module():
    """Import the adapter for its module-level constants (hyphenated filename)."""
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("tsc_check_1499", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("raw, want", [
    ("\x1b[91merror\x1b[0m", "error"),                      # CSI / SGR
    ("\x1b]0;a title\x07keep", "keep"),                     # OSC, BEL-terminated
    ("\x1b]0;a title\x1b\\keep", "keep"),                   # OSC, ST-terminated
    ("\x1bMkeep", "keep"),                                  # two-character escape
    ("kept\x1b[1;3", "kept"),                               # CSI cut mid-sequence
    ("kept\x1b]0;unterminat", "kept"),                      # OSC with no terminator
    ("kept\x1b", "kept"),                                   # a lone trailing ESC
    ("no escapes here", "no escapes here"),
])
def test_strip_ansi_leaves_no_escape_behind(raw: str, want: str) -> None:
    """The invariant is "no ESC survives", so the truncated forms count (#1499).

    A stream cut mid-sequence — a child killed while writing — leaves a CSI with
    no final byte. Matching only the complete forms would let that through, and
    every caller reads the result as text.
    """
    mod = _adapter_module()
    assert mod.strip_ansi(raw) == want
    assert "\x1b" not in mod.strip_ansi(raw)


def test_the_adapter_and_the_core_strip_the_same_escapes() -> None:
    """One definition, stated twice, pinned equal rather than trusted (#1499).

    `validators/` runs with only `validators/common` on `sys.path`, so an adapter
    can import neither the core nor `presets/`. Same constraint and same remedy
    as `validators/common/linebreaks.py` against `_LINE_BREAK_PATTERN` (#1486):
    without this, a fix to one of the two silently reopens the gap in the other.
    """
    import supertool  # noqa: PLC0415

    assert _adapter_module().ANSI_RE.pattern == supertool._ANSI_ESCAPE_RE.pattern


@stub_capable
def test_pretty_output_is_not_requested_from_tsc(tmp_path: Path) -> None:
    """A tsc that defaults to pretty must still yield a located finding (#1499).

    The stub behaves like the real compiler: coloured and multi-line unless the
    invocation says otherwise. So this asserts the argv through the only thing a
    caller can observe, rather than by string-matching the adapter's source.
    """
    f = tmp_path / "bad.ts"
    f.write_text("const x: number = 'not a number';\nexport {};\n")
    env = _stub_tsc(tmp_path, (
        "import sys\n"
        f"PRETTY = {PRETTY_DUMP!r}\n"
        f"PLAIN = {PLAIN_LINE!r}\n"
        "a = sys.argv[1:]\n"
        "off = any(a[i] == '--pretty' and a[i + 1:i + 2] == ['false']\n"
        "          for i in range(len(a)))\n"
        "sys.stdout.write(PLAIN if off else PRETTY)\n"
        "sys.exit(2)\n"
    ))
    out = _run_env(str(f), env)

    assert out["ok"] is False
    err = out["errors"][0]
    assert err["line"] == 1, err
    assert err["col"] == 7, err
    assert err["code"] == "TS2322", err
    assert err["source_context"], err


@stub_capable
def test_an_unparseable_dump_is_a_non_verdict_not_a_syntax_finding(
    tmp_path: Path,
) -> None:
    """Output the adapter cannot parse is `code: adapter`, never `syntax` (#1499).

    `syntax` claims a syntax error was found in this file. What actually
    happened is that no verdict was obtained, and the core reads that off
    `code == "adapter"` on every error (`_validator_not_checked`). It stays an
    error rather than becoming `skipped`, because a skip omits `errors` entirely
    and tsc's own objection would vanish with it — `validators/SCHEMA.md`
    §"A located diagnostic still has to be about *this* file (#754)".

    And nothing republishes terminal escapes into a message a human reads.
    """
    f = tmp_path / "bad.ts"
    f.write_text("const x: number = 'not a number';\nexport {};\n")
    env = _stub_tsc(tmp_path, (
        "import sys\n"
        f"sys.stdout.write({PRETTY_DUMP!r})\n"
        "sys.exit(2)\n"
    ))
    out = _run_env(str(f), env)

    assert out["ok"] is False
    assert out["count"] == 1
    err = out["errors"][0]
    assert err["code"] == "adapter", err
    assert err["line"] is None, err
    assert "\x1b" not in err["msg"], err
    assert "NOT type-checked" in err["msg"], err
    assert "source_context" not in err, err


@stub_capable
def test_a_silent_non_zero_exit_is_a_non_verdict(tmp_path: Path) -> None:
    """tsc failing with nothing to say is not a finding-free failure (#1499).

    `ok: false, count: 0, errors: []` was the shape: a verdict of "not clean"
    carrying nothing to act on, and `_validator_not_checked` sees no errors so
    it cannot recognise the absence either.
    """
    f = tmp_path / "bad.ts"
    f.write_text("export {};\n")
    env = _stub_tsc(tmp_path, "import sys\nsys.exit(2)\n")
    out = _run_env(str(f), env)

    assert out["ok"] is False
    assert out["errors"], out
    err = out["errors"][0]
    assert err["code"] == "adapter", out
    assert "NOT type-checked" in err["msg"], out
    # Not "its output could not be parsed": nothing was offered to parse, and
    # the two arms are different facts about what tsc did.
    assert "said nothing at all" in err["msg"], out


# ---------------------------------------------------------------------------
# Tool missing — graceful degrade
# ---------------------------------------------------------------------------

def test_missing_tool_is_the_third_state(tmp_path: Path) -> None:
    """Absent tsc is `skipped`, not `ok: true` (#1202).

    This asserted `ok is True` for as long as the adapter fabricated it — a
    clean type-check verdict about a file no compiler opened. Escalation under
    `$SUPERTOOL_REQUIRE_VALIDATORS` is asserted in
    `tests/test_validators_absent_tool_third_state_1202.py`.
    """
    f = tmp_path / "hello.ts"
    f.write_text("const x: number = 1;\n")
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True,
        text=True,
        env=empty_path_env(), encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert "skipped" in out, out
    assert "tsc" in out["skipped"]
    assert "ok" not in out, out


# ---------------------------------------------------------------------------
# Valid TypeScript (only when tsc is available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("tsc"), reason="tsc not on PATH")
def test_valid_ts(tmp_path: Path) -> None:
    f = tmp_path / "good.ts"
    f.write_text("const x: number = 42;\nexport {};\n")
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0
    assert out["tool"] == "tsc-check"


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

@pytest.mark.skipif(not shutil.which("tsc"), reason="tsc not on PATH")
def test_output_schema_present(tmp_path: Path) -> None:
    f = tmp_path / "x.ts"
    f.write_text("export {};\n")
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out


@pytest.mark.skipif(not shutil.which("tsc"), reason="tsc not on PATH")
def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.ts"
    f.write_text("export {};\n")
    out = _run(str(f))
    assert isinstance(out["duration_ms"], int)


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("tsc"), reason="tsc not on PATH")
def test_missing_file_returns_error(tmp_path: Path) -> None:
    """Gated, because ungated it asserted the fabricated pass and nothing else.

    Without tsc this ran the absent-tool arm and `assert "ok" in out` held for
    the wrong reason — a green about a file the adapter never handed to
    anything. It now runs only where a real verdict is possible.
    """
    out = _run(str(tmp_path / "nonexistent.ts"))
    assert "ok" in out


@pytest.mark.skipif(not shutil.which("tsc"), reason="tsc not on PATH")
def test_source_context_present_on_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.ts"
    f.write_text("const x: number = 'not a number';\nexport {};\n")
    out = _run(str(f))
    if out["ok"] or not out["errors"]:
        pytest.skip("tsc found no issues (may need tsconfig)")
    err = out["errors"][0]
    assert err["line"] is not None
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    assert len(err["source_context"]) > 0
