"""#2308 -- `_merge_presets` accepted a non-table `builtin-ops.<op>` entry
with no `_preset_warnings` record, so `_get_op_int`/`_get_op_bool`/
`_grep_file_includes` fell back to their default silently -- indistinguishable
from the key never having been set at all.

Would this test fail if the code did nothing? Yes -- at 05de660a, merging a
preset carrying `builtin-ops.read = "not-a-dict-oops"` leaves
`config["_preset_warnings"]` empty.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


def _preset_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                    builtin_ops: dict) -> None:
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


def test_non_table_builtin_ops_entry_is_recorded_as_a_preset_warning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _preset_config(tmp_path, monkeypatch, {"read": "not-a-dict-oops"})

    cfg = supertool._load_config()

    warnings = cfg.get("_preset_warnings") or []
    assert any("read" in w and "not a table" in w.lower()
               or "builtin-ops" in w and "read" in w for w in warnings), warnings


def test_well_formed_builtin_ops_entry_gets_no_warning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Must-fire's pair: a genuinely well-shaped entry must not trip it."""
    _preset_config(tmp_path, monkeypatch, {"read": {"max_lines": 3}})

    cfg = supertool._load_config()

    warnings = cfg.get("_preset_warnings") or []
    assert not any("builtin-ops" in w for w in warnings), warnings
