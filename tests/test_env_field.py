"""Tests for optional `env` field on validator/formatter specs (merges into subprocess env)."""
from __future__ import annotations

import json
from pathlib import Path

import supertool


def _set_validators(cfg: dict) -> None:
    supertool._CONFIG = {"validators": cfg}
    supertool._CONFIG_CHECKED = True


def _set_formatters(fmt: dict) -> None:
    supertool._CONFIG = {"formatters": fmt}
    supertool._CONFIG_CHECKED = True


# ---------------------------------------------------------------------------
# Validator env merge
# ---------------------------------------------------------------------------

def test_validator_env_field_passed_to_subprocess() -> None:
    """env block on validator spec must reach the adapter subprocess."""
    # Adapter reads MY_TEST_VAR from env and embeds it in JSON output.
    cmd = (
        'printf \'{"tool":"t","file":"x","ok":true,"count":0,'
        '"errors":[],"duration_ms":1,"captured":"\'"$MY_TEST_VAR"\'"}\'  '
    )
    spec = {
        "cmd": cmd,
        "timeout": 5,
        "env": {"MY_TEST_VAR": "hello_from_env"},
    }
    out = supertool._validator_run_one("t", spec, "x")
    assert out.get("captured") == "hello_from_env"


def test_validator_env_field_absent_does_not_break() -> None:
    """Spec without env field still works (no KeyError, no crash)."""
    payload = json.dumps(
        {"tool": "t", "file": "x", "ok": True, "count": 0, "errors": [], "duration_ms": 1}
    ).replace("'", "'\\''")
    spec = {"cmd": f"printf '%s' '{payload}'", "timeout": 5}
    out = supertool._validator_run_one("t", spec, "x")
    assert out["ok"] is True


def test_validator_env_field_overrides_parent_env(monkeypatch) -> None:
    """env spec value must shadow an identically named var in os.environ."""
    monkeypatch.setenv("MY_OVERRIDE_VAR", "original")
    cmd = (
        'printf \'{"tool":"t","file":"x","ok":true,"count":0,'
        '"errors":[],"duration_ms":1,"captured":"\'"$MY_OVERRIDE_VAR"\'"}\'  '
    )
    spec = {
        "cmd": cmd,
        "timeout": 5,
        "env": {"MY_OVERRIDE_VAR": "overridden"},
    }
    out = supertool._validator_run_one("t", spec, "x")
    assert out.get("captured") == "overridden"


# ---------------------------------------------------------------------------
# Formatter env merge
# ---------------------------------------------------------------------------

def test_formatter_env_field_passed_to_subprocess(tmp_path: Path) -> None:
    """env block on formatter spec must reach the formatter subprocess.

    Uses a Python adapter (no shell redirect) since cmd templates are argv-form.
    """
    sentinel = tmp_path / "env_capture.txt"
    adapter = tmp_path / "capture_env.py"
    adapter.write_text(
        "import os, pathlib, sys\n"
        f"pathlib.Path({str(sentinel)!r}).write_text(os.environ.get('MY_FMT_VAR', ''))\n"
    )
    spec = {
        "cmd": f"python3 {adapter}",
        "timeout": 5,
        "env": {"MY_FMT_VAR": "fmt_env_value"},
    }
    result = supertool._formatter_run_one("fmt", spec, "any.php")
    assert result["ok"] is True
    assert sentinel.read_text() == "fmt_env_value"


def test_formatter_env_field_absent_does_not_break(tmp_path: Path) -> None:
    """Formatter spec without env field still runs correctly."""
    spec = {"cmd": "true", "timeout": 5}
    result = supertool._formatter_run_one("fmt", spec, "any.php")
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Shell env-prefix lift (KEY=VAL cmd args) — argv-form compat (#145 regression)
# ---------------------------------------------------------------------------

def test_extract_env_prefix_basic() -> None:
    env, cmd = supertool._extract_env_prefix("FOO=bar BAZ=qux python3 script.py")
    assert env == {"FOO": "bar", "BAZ": "qux"}
    assert cmd == "python3 script.py"


def test_extract_env_prefix_no_prefix() -> None:
    env, cmd = supertool._extract_env_prefix("python3 script.py")
    assert env == {}
    assert cmd == "python3 script.py"


def test_extract_env_prefix_quoted_value() -> None:
    env, cmd = supertool._extract_env_prefix("KEY='one two' python3 script.py")
    assert env == {"KEY": "one two"}
    assert "python3 script.py" in cmd


def test_validator_env_prefix_reaches_child(tmp_path: Path) -> None:
    """Closes the regression: shipped DVSI cmd templates use `KEY=VAL python3 ...`
    shell env-prefix syntax. Argv-form must lift that into env=.
    """
    adapter = tmp_path / "capture.py"
    payload = '{"tool":"t","file":"x","ok":true,"count":0,"errors":[],"duration_ms":1,"working_dir":"' + str(tmp_path) + '"}'
    adapter.write_text(
        "import os, sys\n"
        f"if os.environ.get('MCP_PHPSTAN_WORKING_DIR') == {str(tmp_path)!r}:\n"
        f"    sys.stdout.write({payload!r})\n"
        "else:\n"
        "    sys.stdout.write('{\"tool\":\"t\",\"file\":\"x\",\"ok\":false,\"count\":1,\"errors\":[{\"line\":null,\"col\":null,\"severity\":\"error\",\"code\":\"x\",\"msg\":\"env not set\"}],\"duration_ms\":1}')\n"
    )
    cmd = f"MCP_PHPSTAN_WORKING_DIR={tmp_path} python3 {adapter}"
    spec = {"cmd": cmd, "timeout": 5, "cache": False}
    out = supertool._validator_run_one("t", spec, "any.php")
    assert out.get("ok") is True, f"env-prefix did not reach child: {out}"
