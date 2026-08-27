"""#2025 — the LSP op docs live in a preset, and a preset cannot shadow a built-in.

`workspace`, `resolve`, `diag`, `hover` and `rename` dispatch from core and are
documented in `presets/lsp.json` rather than in `.supertool.json`'s
`builtin-ops`. Only the documentation moved: every one of them is still a
built-in, still reached by the dispatcher's own branch, and still classified by
`_OP_SAFETY_BUILTIN` rather than by anything a preset writes.

`_shipped_preset_ops` used to skip any preset op whose name was in
`_BUILTIN_OPS`. That filter stood in for "a preset must not shadow a built-in",
a property the dispatcher already guarantees structurally — custom ops are only
reached on the fallthrough, after every built-in branch has declined. Left in
place it would have made these five invisible to the #614 hint, so an unknown-op
error would have reported an op that exists as one that does not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

REPO_ROOT = Path(__file__).resolve().parent.parent
MOVED = ("workspace", "resolve", "diag", "hover", "rename")


@pytest.fixture()
def lsp_preset() -> dict:
    raw = json.loads(
        (REPO_ROOT / "presets" / "lsp.json").read_text(encoding="utf-8"))
    assert not raw.get("ops"), (
        "presets/lsp.json defines no ops — it documents built-ins, and an "
        "`ops` section here would put cmd-less entries in front of four "
        "sweeps that rightly refuse them (#1231, #1269, #1287, #1350, #1384)")
    return raw.get("builtin-ops") or {}


def test_the_preset_documents_every_moved_op(lsp_preset: dict) -> None:
    missing = [name for name in MOVED if name not in lsp_preset]
    assert not missing, f"presets/lsp.json does not document {missing}"


@pytest.mark.parametrize("name", MOVED)
def test_each_entry_carries_the_three_documented_keys(
        lsp_preset: dict, name: str) -> None:
    """`ops` renders `syntax`, `ops:full` adds `description`, `help:OP` both
    plus `example`. An entry missing one degrades a surface silently."""
    entry = lsp_preset.get(name) or {}
    for key in ("syntax", "description", "example"):
        assert entry.get(key), f"{name} has no {key}"
    assert entry["syntax"].startswith(name), (
        f"{name}: syntax {entry['syntax']!r} does not name the op")


def test_the_moved_ops_are_gone_from_builtin_ops() -> None:
    raw = json.loads(
        (REPO_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    still_there = [n for n in MOVED if n in (raw.get("builtin-ops") or {})]
    assert not still_there, (
        f"documented twice — builtin-ops still carries {still_there}, so the "
        "SessionStart listing pays for them in every repo")


def test_the_preset_declares_no_safety_class(lsp_preset: dict) -> None:
    """`_OP_SAFETY_BUILTIN` is the one source for a built-in's class, and
    `_op_safety_classes` skips any name it already holds. A `safety` key here
    would be inert text that reads as authoritative — `rename` writes, and a
    preset saying otherwise would never be consulted to be caught."""
    declared = [n for n in MOVED if (lsp_preset.get(n) or {}).get("safety")]
    assert not declared, (
        f"{declared} declare a safety class the loader ignores; the class "
        "comes from _OP_SAFETY_BUILTIN")


@pytest.mark.parametrize("name,expected", [
    ("hover", "read-only"), ("resolve", "read-only"), ("diag", "read-only"),
    ("workspace", "read-only"), ("rename", "writes"),
])
def test_safety_still_comes_from_the_binary(name: str, expected: str) -> None:
    assert supertool._OP_SAFETY_BUILTIN[name] == expected


def _shadow_config() -> dict:
    """One `cmd`, installed under two names: the built-in one and a free one.

    The free name is the control. Without it a spawn that simply failed —
    `echo` is a shell builtin on Windows, not a binary — would satisfy the
    assertion below while proving nothing, which is the vacuous-pass shape
    this repo keeps filing.
    """
    cmd = "echo SHADOWED"
    return {"ops": {
        "hover": {"cmd": cmd, "syntax": "hover:SYMBOL:FILE"},
        "lsp-shadow-control": {"cmd": cmd, "syntax": "lsp-shadow-control:X"},
    }}


def test_the_control_op_really_does_emit_the_marker(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", _shadow_config())
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    out = supertool.dispatch("lsp-shadow-control:Thing")
    assert "SHADOWED" in out, (
        "the control op did not run, so the shadowing test below cannot "
        "distinguish 'built-in won' from 'nothing ran at all'")


def test_a_preset_entry_named_after_a_builtin_does_not_shadow_it(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The property the dropped `_BUILTIN_OPS` filter was standing in for.

    `dispatch` reaches `_resolve_custom_op` only on the fallthrough, after
    every built-in branch has declined, so a custom op named `hover` is never
    consulted. Asserted rather than assumed, because dropping the filter makes
    this the only thing holding the line.
    """
    monkeypatch.setattr(supertool, "_CONFIG", _shadow_config())
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    out = supertool.dispatch("hover:Thing:supertool.py")
    assert "SHADOWED" not in out, (
        "a preset op shadowed the built-in `hover` — the dispatcher stopped "
        "trying built-ins first")


def test_the_shipped_index_can_name_a_builtin_named_preset_op(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The #614 hint's index. It answers "this op exists in this binary, its
    preset is not loaded where you are standing" — and it cannot answer for an
    op it refuses to index."""
    monkeypatch.setattr(supertool, "_SHIPPED_PRESET_OPS", None)
    index = supertool._shipped_preset_ops()
    assert index.get("hover") == "lsp", (
        "hover is absent from the shipped preset index, so an unknown-op "
        "error cannot say where it lives")


def test_the_moved_ops_are_still_dispatchable_names() -> None:
    """Moving documentation must not move capability."""
    valid = set(supertool._valid_op_names())
    assert set(MOVED) <= valid, sorted(set(MOVED) - valid)


def test_the_preset_merges_into_the_builtin_ops_section(
        shipped_config: dict) -> None:
    """Where the entries land is the whole design, not bookkeeping.

    In `ops` they would face four sweeps that rightly demand a resolvable
    script, a safety class, a path-containment boundary and a `replaces`
    census row — none of which a built-in's documentation can supply.
    """
    builtin_docs = shipped_config.get("builtin-ops") or {}
    ops = shipped_config.get("ops") or {}
    for name in MOVED:
        assert name in builtin_docs, f"{name} did not merge into builtin-ops"
        # The key, not merely its presence. `_merge_op_def(entry, None)`
        # returns None, so a merge written with no project entry to override
        # put the name in the section mapped to nothing — and `help:hover`
        # answered "no documented help" while every membership assertion
        # stayed green. A section is not a document.
        assert isinstance(builtin_docs[name], dict), (
            f"{name} merged as {builtin_docs[name]!r}, not an entry")
        assert builtin_docs[name].get("syntax"), f"{name} merged without syntax"
        assert name not in ops, (
            f"{name} landed in `ops`, where a cmd-less entry is the #1356 stub")


@pytest.mark.parametrize("name", MOVED)
def test_help_answers_for_every_moved_op(shipped_config: dict,
                                         name: str) -> None:
    """The surface a reader actually uses, asserted end to end.

    The structural tests above all passed against a merge that mapped every
    name to `None`; this one did not.
    """
    out = supertool.dispatch(f"help:{name}")
    assert "no documented help" not in out, out
    assert name in out


def test_the_loader_stamps_a_doc_only_contribution(
        shipped_config: dict) -> None:
    """`tests/conftest.py` refuses a declared preset that contributed nothing
    (#1829). A doc-only preset contributes, and says so through the loader
    rather than by anyone re-walking the manifests."""
    stamped = shipped_config.get("_preset_doc_contributions") or {}
    assert sorted(stamped.get("lsp") or []) == sorted(MOVED), stamped


def test_a_project_entry_wins_over_the_presets_documentation(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same precedence `ops` already has: a repo that documents an op
    documents its own version. Without this a preset would silently overwrite
    a project's wording, which is the one direction nobody can debug."""
    cfg = {
        "presets": ["lsp"],
        "builtin-ops": {"hover": {"description": "project wording"}},
    }
    supertool._merge_presets(cfg, str(REPO_ROOT))
    entry = cfg["builtin-ops"]["hover"]
    assert entry["description"] == "project wording"
    assert "diag" in cfg["builtin-ops"], (
        "the project entry suppressed the rest of the preset's documentation")
    # Key-for-key, not wholesale. `builtin-ops` entries carry behaviour
    # overrides as well as prose, so a project that sets one key must not
    # delete the rest of the entry and leave the op undocumented (#1356).
    assert entry.get("syntax") == "hover:SYMBOL:FILE", (
        "the project's partial override replaced the preset entry wholesale, "
        "dropping the syntax line: that is the #1356 stub in builtin-ops")
