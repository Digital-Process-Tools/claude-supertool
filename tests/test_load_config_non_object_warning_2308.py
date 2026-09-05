"""#2308 self-review follow-up: `_load_config()` had the identical silent
absence as #2306/#2308 one layer higher -- a `.supertool.json` that parses
as valid JSON but is not an object (a list, string, or number) coerced to
`{}` with nothing recorded in `_CONFIG_WARNINGS`, indistinguishable from no
config file existing at all above cwd.

Would this test fail if the code did nothing? Yes -- before this follow-up,
`_CONFIG_WARNINGS` stays empty for a `.supertool.json` holding `[1, 2, 3]`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


def _fresh_config(monkeypatch):
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    monkeypatch.setattr(supertool, "_CONFIG_WARNINGS", [])


def test_a_non_object_config_is_recorded_as_a_warning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".supertool.json").write_text(
        json.dumps([1, 2, 3]), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _fresh_config(monkeypatch)

    cfg = supertool._load_config()

    assert cfg.get("presets") is None
    assert any("does not hold a JSON object" in w
               for w in supertool._CONFIG_WARNINGS), supertool._CONFIG_WARNINGS


def test_a_well_formed_config_is_not_announced(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Must-fire's pair: a genuinely well-formed config trips nothing."""
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": []}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _fresh_config(monkeypatch)

    supertool._load_config()

    assert not any("does not hold a JSON object" in w
                   for w in supertool._CONFIG_WARNINGS), supertool._CONFIG_WARNINGS
