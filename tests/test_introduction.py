from __future__ import annotations

import json
from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_introduction — project-specific intro text from .supertool.json, an
# env override (SUPERTOOL_INTRODUCTION), or a shipped default (#2342)
# ---------------------------------------------------------------------------

def test_introduction_from_config(tmp_path: Path, monkeypatch) -> None:
    """introduction key in config is output verbatim."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "introduction": "supertool batches ops into one call.\nPack 6-7 per call."
    }))
    monkeypatch.delenv("SUPERTOOL_INTRODUCTION", raising=False)
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_introduction()
    assert "supertool batches ops into one call." in out
    assert "Pack 6-7 per call." in out


def test_introduction_no_config_falls_back_to_shipped_default(tmp_path: Path, monkeypatch) -> None:
    """No .supertool.json at all -> the shipped default, not a permanent
    "No ... configured" non-answer (#2342)."""
    monkeypatch.delenv("SUPERTOOL_INTRODUCTION", raising=False)
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_introduction()
    assert "No introduction configured" not in out
    assert out.strip()
    assert "project root" in out


def test_introduction_missing_key_falls_back_to_shipped_default(tmp_path: Path, monkeypatch) -> None:
    """Config file exists but carries no introduction key -> the same
    shipped default as no config at all (#2342)."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({"compact": True}))
    monkeypatch.delenv("SUPERTOOL_INTRODUCTION", raising=False)
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_introduction()
    assert "No introduction configured" not in out
    assert "project root" in out


def test_introduction_disabled_in_config_prints_not_configured(tmp_path: Path, monkeypatch) -> None:
    """Deliberately disabling the key (any of _DISABLE_VALUES) is the one
    path that still reaches the old "not configured" line (#2342)."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({"introduction": "none"}))
    monkeypatch.delenv("SUPERTOOL_INTRODUCTION", raising=False)
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_introduction()
    assert "No introduction configured" in out


def test_introduction_env_override_wins_over_config(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({"introduction": "from config"}))
    monkeypatch.setenv("SUPERTOOL_INTRODUCTION", "from env")
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_introduction()
    assert "from env" in out
    assert "from config" not in out


def test_introduction_env_can_disable_the_shipped_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_INTRODUCTION", "off")
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
    monkeypatch.delenv("SUPERTOOL_INTRODUCTION", raising=False)
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.dispatch("introduction")
    assert "--- introduction ---" not in out
    assert "Hello LLM, this is supertool." in out


def test_introduction_default_has_no_batching_exhortation() -> None:
    """A downstream A/B test (claude-oss scripts/batch_hint.py, #490) found
    a batching exhortation in an always-loaded surface counterproductive --
    6% MORE single-op calls in the treatment arm, not fewer. The shipped
    default must not carry it (#2342)."""
    assert "6-7" not in supertool._DEFAULT_INTRODUCTION
    assert "wastes money" not in supertool._DEFAULT_INTRODUCTION
    assert "is normal" not in supertool._DEFAULT_INTRODUCTION


def test_introduction_from_config_is_stripped_like_the_coauthor_convention(tmp_path: Path, monkeypatch) -> None:
    """`_onboarding_text` claims the same convention as `_DEFAULT_COAUTHOR`
    (presets/git/commit.py), which always returns the stripped value.
    A hand-edited config value with a trailing newline must not stack with
    the "\n\n" op_introduction already appends (self-review finding, #2342)."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "introduction": "  from config with padding  \n"
    }))
    monkeypatch.delenv("SUPERTOOL_INTRODUCTION", raising=False)
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_introduction()
    assert out == "from config with padding\n\n"
