"""#1311 item 2 -- `help:OP` names `literal_backslashes` for a write-bound field.

#1400 already fixed the half of this that mattered most: `help:paste` prints
the `@-` route and its field names (`path`, `content`). What it never printed
is `literal_backslashes` (#1096) -- the escape hatch for the doubled-backslash
write refusal that fires on exactly those fields. An agent that hits the
refusal cold has to trip it once to learn the key exists; `help:` is supposed
to be the place that saves the round-trip, and for this key it did not.

Scoped to the write-bound fields the refusal itself is scoped to
(`_PAYLOAD_DBS_WRITE_KEYS` = new, content, message) -- an op whose payload
route carries none of those (a read op, or a preset op taking `body`/`title`)
has nothing this key would ever apply to, and must not claim it does.
"""
from __future__ import annotations

import supertool


def test_help_paste_names_literal_backslashes():
    """`content` is a write-bound field -- the key applies."""
    out = supertool.op_help("paste")
    assert "literal_backslashes" in out


def test_help_edit_names_literal_backslashes():
    """`new` is a write-bound field -- the key applies."""
    out = supertool.op_help("edit")
    assert "literal_backslashes" in out


def test_help_git_commit_names_literal_backslashes(with_preset_op):
    """`message` is a write-bound field -- the key applies.

    `git-commit` is a preset-manifest op, not a builtin, so it needs the
    `with_preset_op` opt-in (#1812) -- the autouse config blackout otherwise
    makes it not exist in any test, in this file included.
    """
    with_preset_op("git-commit", payload_route=True)
    out = supertool.op_help("git-commit")
    assert "literal_backslashes" in out


def test_help_read_op_does_not_claim_literal_backslashes():
    """`read`'s payload route carries no write-bound field -- must NOT claim it."""
    out = supertool.op_help("read")
    assert "literal_backslashes" not in out
