"""#1773 — `help:OP` for a core op answered only inside this checkout.

`op_help` reads `builtin-ops` out of whatever `_load_config()` returned, and
that config is the one found by walking up from **cwd**. Preset ops carry their
documentation wherever they are installed, because the preset JSON travels with
the plugin. Builtin ops do not: their `builtin-ops` block lives in *this
repository's* `.supertool.json`, which is not a preset and is merged into
nobody else's config.

Measured 2026-08-16 from a `claude-oss` worktree — a plain consumer, three
presets, no `builtin-ops` of its own::

    $ supertool 'help:paste' 'help:read' 'help:grep' 'help:gh-pr'
    ERROR: op 'paste' has no documented help in .supertool.json. …
    ERROR: op 'read'  has no documented help in .supertool.json. …
    ERROR: op 'grep'  has no documented help in .supertool.json. …
    gh-pr:NUMBER_OR_BRANCH[:status|:full|:diff[:PATH]|:threads] …

The same four calls answer in full from the supertool checkout. So the split was
never documented/undocumented — it was **preset op / core op**, and the core ops
are the ones a new caller reaches for first. Every plugin install outside this
tree had a dead `help:` for `read`, `grep`, `glob`, `paste`, `edit`, `vim`,
`between` and `batch`.

The refusal misled twice on top of that: "has no documented help in
`.supertool.json`" reads as a gap the reader could fill locally, and
"see docs/operations" names a directory that does not exist next to a plugin
install. The docs were never missing; they were simply not reachable.

The fix is a lookup, not a merge: the shipped `.supertool.json` beside
`_supertool.py` answers when the local config has nothing to say about the name.
It is deliberately **not** merged into `ops`, because a consumer roster gaining
forty builtin descriptions is #1774 pointing the other way.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def consumer_tree(monkeypatch: pytest.MonkeyPatch):
    """A config with preset ops and no `builtin-ops` — the shape of a consumer.

    Built from the shipped github preset rather than invented, so the preset
    half of the assertion is about the same machinery a real install uses.
    """
    preset = json.loads(
        (REPO_ROOT / "presets" / "github.json").read_text(encoding="utf-8"))
    cfg = {"ops": dict(preset.get("ops") or {})}
    assert "builtin-ops" not in cfg
    monkeypatch.setattr(supertool, "_CONFIG", cfg)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", "/somewhere/else/.supertool.json")
    return cfg


def test_the_shipped_docs_are_where_the_lookup_expects_them() -> None:
    """A fallback pointed at a file that moved is a fallback that never fires."""
    shipped = supertool._shipped_config()
    assert isinstance(shipped.get("builtin-ops"), dict)
    for name in ("read", "grep", "paste", "edit", "batch"):
        assert name in shipped["builtin-ops"], name


@pytest.mark.parametrize("name", ["read", "grep", "paste", "edit", "vim", "batch"])
def test_a_core_op_answers_from_a_tree_that_documents_none(
        consumer_tree, name: str) -> None:
    """The four calls in the docstring, plus the two the same session needed."""
    out = supertool.op_help(name)
    assert not out.startswith("ERROR"), out
    assert name in out


def test_the_answer_says_where_it_came_from(consumer_tree) -> None:
    """A reader who asked their own tree and was answered by another one is
    owed the sentence: the entry describes the *binary*, and a project that
    overrides the op is not what they just read."""
    out = supertool.op_help("read")
    assert "shipped" in out.lower()


def test_a_preset_op_still_answers_from_the_local_config(consumer_tree) -> None:
    """The half that already worked, pinned so the fallback cannot shadow it."""
    out = supertool.op_help("gh-pr")
    assert not out.startswith("ERROR")
    assert "shipped" not in out.lower()


def test_the_local_entry_wins_over_the_shipped_one(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A project redefining a builtin documents its own version, and that is
    the one its caller must be shown."""
    cfg = {"builtin-ops": {"read": {"syntax": "read:PATH",
                                    "description": "LOCAL OVERRIDE"}}}
    monkeypatch.setattr(supertool, "_CONFIG", cfg)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", "/somewhere/else/.supertool.json")
    out = supertool.op_help("read")
    assert "LOCAL OVERRIDE" in out
    assert "shipped" not in out.lower()


def test_a_name_that_is_no_op_anywhere_is_still_refused(consumer_tree) -> None:
    """The fallback widens where an answer may come from, never what counts as
    a name. `write` is the spelling #1772 found in shipped prose; it must keep
    failing, because the creator is `paste`."""
    out = supertool.op_help("write")
    assert out.startswith("ERROR")
    assert "no help for op: write" in out


def test_the_refusal_no_longer_sends_the_reader_to_a_path_that_is_not_there(
        consumer_tree, monkeypatch: pytest.MonkeyPatch) -> None:
    """`docs/operations` sits next to this checkout and nowhere else.

    Reached by emptying the shipped block, which is the one state that can
    still produce this arm once the fallback exists — an install whose
    `.supertool.json` is missing or unreadable. The remedy printed there has to
    be true from anywhere, so it names `ops:roster` and the op's own error
    rather than a directory that ships with the source tree only.
    """
    monkeypatch.setattr(supertool, "_shipped_config", lambda: {})
    out = supertool.op_help("read")
    assert out.startswith("ERROR")
    assert "docs/operations" not in out
    assert "ops:roster" in out
