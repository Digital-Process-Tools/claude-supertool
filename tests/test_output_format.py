from __future__ import annotations

import json
from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_output_format — output format examples from .supertool.json
# ---------------------------------------------------------------------------

def test_output_format_from_config(tmp_path: Path, monkeypatch) -> None:
    """output-format key in config is output verbatim."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "output-format": "--- read:foo ---\n     1→hello"
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_output_format()
    assert "--- read:foo ---" in out
    assert "1→hello" in out


def test_output_format_no_config(tmp_path: Path, monkeypatch) -> None:
    """No config → fallback message."""
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_output_format()
    assert "No output-format configured" in out


def test_output_format_dispatch(tmp_path: Path, monkeypatch) -> None:
    """dispatch('output-format') routes to op_output_format."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "output-format": "--- example ---\nsome output"
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.dispatch("output-format")
    assert "--- output-format ---" not in out
    assert "--- example ---" in out
