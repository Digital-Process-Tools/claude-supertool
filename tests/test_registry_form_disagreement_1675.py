"""1675 (instance 2) — `help:NAME` and `registry:NAME` disagreed about whether
a declared *form* name exists. `grep-count` documents a spelling of `grep`
(`#1245`), not a dispatchable name: `help:grep-count` answers in full, with
`grep`'s own reference, while `registry:grep-count` said `ERROR: no op named
'grep-count' here` — the same sentence it gives a name nobody ever declared.

Both were arguably answering a different question honestly. What was missing
is either surface saying so. This pins the fix chosen for #1675: `registry`
now says a form name is a declared spelling of its parent rather than
claiming ignorance of it, and `help` discloses the same fact at the top of
its answer, so a reader who checks either one learns the truth regardless of
which they picked.
"""
from __future__ import annotations

import supertool


def test_registry_names_grep_count_as_a_form_of_grep() -> None:
    out = supertool.op_registry("grep-count")
    assert "ERROR: no op named" not in out
    assert "grep" in out
    assert "form" in out.lower()
    assert "registry:grep" in out


def test_registry_names_read_grep_as_a_form_of_read() -> None:
    out = supertool.op_registry("read-grep")
    assert "ERROR: no op named" not in out
    assert "form" in out.lower()
    assert "registry:read" in out


def test_help_grep_count_discloses_it_is_a_form_up_top() -> None:
    out = supertool.op_help("grep-count")
    # The parent's syntax is still the first line — the form's own entry.
    assert out.splitlines()[0].startswith("grep:")
    assert "form of" in out.lower() or "declared form" in out.lower()
    assert "grep" in out


def test_a_name_nobody_ever_declared_still_gets_the_plain_refusal() -> None:
    out = supertool.op_registry("totally-not-a-thing-2081")
    assert out.startswith("ERROR: no op named 'totally-not-a-thing-2081'")
