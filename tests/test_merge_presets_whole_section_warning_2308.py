"""#2308 self-review, round two (oss:auditor re-spawn): a preset manifest's
WHOLE `builtin-ops` section being a non-table -- not one op's entry inside
it, the section itself -- silently skipped the merge branch entirely, with
no `_preset_warnings` entry. Indistinguishable from a preset declaring no
`builtin-ops` section at all.

Would this test fail if the code did nothing? Yes -- before this follow-up,
a preset carrying `{"builtin-ops": "oops-a-string"}` merges silently:
`_preset_warnings` stays empty.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


def _preset_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                    builtin_ops) -> None:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir(parents=True)
    (preset_dir / "evil.json").write_text(
        json.dumps({"builtin-ops": builtin_ops}), encoding="utf-8")
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["evil"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)


def test_a_non_table_whole_section_is_recorded_as_a_preset_warning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _preset_config(tmp_path, monkeypatch, "oops-a-string")

    cfg = supertool._load_config()

    assert cfg.get("builtin-ops") is None
    warnings = cfg.get("_preset_warnings") or []
    assert any("builtin-ops" in w and "not a table" in w for w in warnings), warnings


def test_a_well_formed_section_gets_no_such_warning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Must-fire's pair: a genuinely well-shaped section trips nothing."""
    _preset_config(tmp_path, monkeypatch, {"read": {"max_lines": 3}})

    cfg = supertool._load_config()

    warnings = cfg.get("_preset_warnings") or []
    assert not any("the whole section was dropped" in w for w in warnings), warnings
