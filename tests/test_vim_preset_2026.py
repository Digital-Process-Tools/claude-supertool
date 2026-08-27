"""#2026 — `vim`'s documentation lives in `presets/vim.json`.

Same doc-only shape as `presets/lsp.json` (#2025): the entry is under the
manifest's `builtin-ops` section, so no `config["ops"]` sweep sees it, and the
op itself is untouched — it dispatches from core and its `@payload` route comes
from `_AT_FILE_BUILTIN_DEFAULTS`.

That last part is the one worth pinning. `vim`'s route derives its field names
from the `syntax` string when a config supplies one, and #770 is the case where
rewording that string silently deleted the route. Moving the string out of
`.supertool.json` is the same class of change, so the route is asserted here
with the documentation absent entirely — which is what a consumer repo that
does not list this preset actually has.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def vim_preset() -> dict:
    raw = json.loads(
        (REPO_ROOT / "presets" / "vim.json").read_text(encoding="utf-8"))
    assert not raw.get("ops"), (
        "presets/vim.json defines no ops — it documents a built-in, and an "
        "`ops` section here would put a cmd-less entry in front of four "
        "sweeps that rightly refuse it (#1231, #1269, #1287, #1350, #1384)")
    return raw.get("builtin-ops") or {}


def test_the_preset_documents_vim(vim_preset: dict) -> None:
    entry = vim_preset.get("vim") or {}
    for key in ("syntax", "description", "example"):
        assert entry.get(key), f"vim has no {key}"
    assert entry["syntax"] == "vim:::PATH:::SCRIPT", entry["syntax"]


def test_the_description_still_carries_the_macro_grammar(
        vim_preset: dict) -> None:
    """The move's whole cost is that this text becomes unreachable where the
    preset is not loaded. Trimming it on the way through would pay that cost
    twice, so assert it arrived whole."""
    desc = (vim_preset.get("vim") or {}).get("description", "")
    for token in ("NORMAL mode", "GREEDY", "AUTO-INDENT", "DEFAULT EDIT OP",
                  ":g/PAT/d", "Autocorrects"):
        assert token in desc, f"the grammar lost {token!r} in the move"


def test_the_entry_keeps_its_hint_flag(vim_preset: dict) -> None:
    """`hint: true` is what makes the description render in `ops-compact` and
    the example survive it. Dropping it here would look like a byte saving and
    would silently be a second change nobody asked for."""
    assert (vim_preset.get("vim") or {}).get("hint") is True


def test_vim_is_gone_from_the_project_config() -> None:
    raw = json.loads(
        (REPO_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    assert "vim" not in (raw.get("builtin-ops") or {}), (
        "documented twice — .supertool.json still carries the vim entry")


def test_the_payload_route_survives_the_preset_being_unloaded(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The #770 shape, asserted against the worst case rather than this repo's.

    With no config at all, `_build_at_file_registry` starts from
    `_AT_FILE_BUILTIN_DEFAULTS`, which is where `vim`'s fields come from. A
    consumer repo that never lists this preset still gets `vim:@-`.
    """
    monkeypatch.setattr(supertool, "_CONFIG", {})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY", {})
    monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY_BUILT", False)
    assert supertool._at_file_fields("vim") == ["path", "script"]


def test_the_payload_route_is_the_same_with_the_preset_loaded(
        shipped_config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: a syntax string that reaches the registry from a preset
    must derive the same fields it derived from `.supertool.json`, or the move
    changed the payload shape while looking like a documentation edit."""
    monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY", {})
    monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY_BUILT", False)
    assert supertool._at_file_fields("vim") == ["path", "script"]


def test_vim_is_still_classified_by_the_binary() -> None:
    assert supertool._OP_SAFETY_BUILTIN["vim"] == "writes"


def test_the_preset_declares_no_safety_class(vim_preset: dict) -> None:
    """`_op_safety_classes` skips any name in `_OP_SAFETY_BUILTIN`, so a
    `safety` key here is text that reads as authoritative and is never read."""
    assert not (vim_preset.get("vim") or {}).get("safety")


def test_the_shipped_index_names_it(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supertool, "_SHIPPED_PRESET_OPS", None)
    assert supertool._shipped_preset_ops().get("vim") == "vim"


def test_help_answers_for_vim_in_this_repo(shipped_config: dict) -> None:
    """This repository lists the preset, so the grammar stays one op away."""
    out = supertool.dispatch("help:vim")
    assert "vim:::PATH:::SCRIPT" in out
    assert "DEFAULT EDIT OP" in out
