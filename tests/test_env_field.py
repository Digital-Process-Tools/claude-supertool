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
    """env block on formatter spec must reach the formatter subprocess."""
    sentinel = tmp_path / "env_capture.txt"
    # Formatter writes the env var to a file (touch isn't a write, use printf).
    cmd = f"printf '%s' \"$MY_FMT_VAR\" > {sentinel}"
    spec = {
        "cmd": cmd,
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
