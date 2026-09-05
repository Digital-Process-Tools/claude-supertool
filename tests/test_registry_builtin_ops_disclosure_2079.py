"""#2079 -- `registry:OP` structurally could not disclose a `builtin-ops`
override for a built-in op.

`_merge_presets` (#2025) writes a preset manifest's `builtin-ops` entries into
the same merged dict `_get_op_int`, `_get_op_bool` and `_grep_extensions`
read for a built-in's runtime behaviour (`read.max_lines`, `grep.extensions`).
Before this fix, `registry:read` refused outright for ANY built-in --
`ERROR: 'read' is a built-in, not a preset or project op, so it has no
registry entry` -- so a preset or project re-tuning a built-in project-wide
had nothing in the tool that disclosed it.

Would this test fail if the code did nothing? Yes -- at 7bb823f8,
`supertool.op_registry("read")` for a config carrying `builtin-ops.read.
max_lines: 3` returns that same ERROR line, naming no override at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


def _evil_preset_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                        builtin_ops: dict) -> None:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    (preset_dir / "evil.json").write_text(
        json.dumps({"builtin-ops": builtin_ops}), encoding="utf-8")
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["evil"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)


def test_registry_discloses_a_builtin_runtime_override(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _evil_preset_config(tmp_path, monkeypatch,
                        {"read": {"max_lines": 3}})

    out = supertool.op_registry("read")

    assert "ERROR" not in out
    assert "max_lines" in out
    assert "3" in out
    assert "evil" in out
    assert "built-in" in out


def test_registry_still_refuses_a_built_in_with_no_override(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A built-in nobody's config re-tunes keeps the plain refusal."""
    (tmp_path / ".supertool.json").write_text(
        json.dumps({}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)

    out = supertool.op_registry("read")

    assert out.startswith("ERROR: 'read' is a built-in")


def test_doc_only_builtin_ops_entry_is_not_read_as_an_override(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A shipped `builtin-ops` doc entry (description/syntax/example, the
    shape #1675 already ships for `vim`/`workspace`) discloses as
    documentation, never as a runtime override nobody actually set."""
    _evil_preset_config(tmp_path, monkeypatch, {
        "read": {"description": "reads a file", "syntax": "read:PATH"},
    })

    out = supertool.op_registry("read")

    assert "no runtime-affecting keys" in out
    assert "description" in out and "syntax" in out


def test_registry_builtin_ops_entry_returns_none_for_untouched_op(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _evil_preset_config(tmp_path, monkeypatch,
                        {"read": {"max_lines": 3}})

    entry, contributors = supertool._registry_builtin_ops_entry(
        supertool._load_config(), "grep")

    assert entry is None
    assert contributors == ()
