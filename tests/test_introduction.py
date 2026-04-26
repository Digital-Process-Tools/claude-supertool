from __future__ import annotations

import json
from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_introduction — project-specific intro text from .supertool.json
# ---------------------------------------------------------------------------

def test_introduction_from_config(tmp_path: Path, monkeypatch) -> None:
    """introduction key in config is output verbatim."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "introduction": "supertool batches ops into one call.\nPack 6-7 per call."
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_introduction()
    assert "supertool batches ops into one call." in out
    assert "Pack 6-7 per call." in out


def test_introduction_no_config(tmp_path: Path, monkeypatch) -> None:
    """No .supertool.json → fallback message."""
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_introduction()
    assert "No introduction configured" in out


def test_introduction_missing_key(tmp_path: Path, monkeypatch) -> None:
    """Config exists but no introduction key → fallback message."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({"compact": True}))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_introduction()
    assert "No introduction configured" in out


def test_introduction_dispatch(tmp_path: Path, monkeypatch) -> None:
    """dispatch('introduction') routes to op_introduction."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "introduction": "Hello LLM, this is supertool."
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.dispatch("introduction")
    assert "--- introduction ---" not in out
    assert "Hello LLM, this is supertool." in out
