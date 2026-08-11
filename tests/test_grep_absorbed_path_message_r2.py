"""#1417's refusal states a cause that is false when the caller typed `.`.

`_parse_grep_args` produces `path == "."` from two different inputs — an empty
PATH slot (`grep:foo:docs:`) and a literal one (`grep:foo:docs:.`) — and the
refusal explains itself with "an empty slot defaults to it" in both. For the
second caller that sentence is simply untrue, and it is untrue in the one place
a reader goes to find out what they typed wrong.

`docs/operations/search.md` already says it correctly ("when the path resolves
to `.`"). The message is brought to that, rather than the parser being widened
to carry the origin: the 6-tuple `_parse_grep_args` returns is unpacked at
eleven sites, and a seventh field costs all of them to distinguish two readings
that produce the same refusal for the same reason.

Would these pass if the code did nothing? No — the string asserted absent is
in the message at 9f07c5e, for both inputs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import supertool

BOTH_SPELLINGS = ["grep:foo:docs:", "grep:foo:docs:."]


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("foo" + chr(10), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("call", BOTH_SPELLINGS)
def test_both_spellings_are_still_declined(tree, call):
    # Anti-vacuity: the wording assertions below say nothing unless the
    # refusal fires on both inputs.
    assert "#1417" in supertool.dispatch(call), call


@pytest.mark.parametrize("call", BOTH_SPELLINGS)
def test_the_refusal_does_not_claim_an_origin_it_cannot_know(tree, call):
    out = supertool.dispatch(call)
    assert "an empty slot defaults to it" not in out, out


@pytest.mark.parametrize("call", BOTH_SPELLINGS)
def test_it_names_both_readings_of_the_slot(tree, call):
    out = supertool.dispatch(call)
    assert "empty" in out and "a literal" in out, out
