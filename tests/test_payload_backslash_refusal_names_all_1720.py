r"""#1720 - the @payload backslash refusal names only the first offending field.

Filed live by the #1715 implementation agent: a payload with several fields
carrying a doubled-backslash mistake cost one round-trip per field, because
the refusal named the first offender and stopped.

**Part one - the doubled-backslash write refusal itself
(`_payload_double_backslash_refusal`).** By the time this issue was filed the
message already read `field (2x)` for a single field, but named only one
field when several were wrong. #1808/#1814/#1819/#1839 widened that render to
locate every occurrence in every offending field (capped and disclosed, never
silently truncated). This file pins that it holds for the case #1720 itself
describes: several WRITE-bound fields (`new`, `content`) each carrying a
doubled backslash in the same payload.

**Part two - the family the issue asked to be checked.**
`_payload_literal_backslashes_misplaced` (#1096) had the same defect one
level up: `literal_backslashes` set inside more than one `[[ops]]` entry
named only the first index and silently dropped the rest. Fixed here
alongside the file this docstring lives in.
"""
from pathlib import Path

import pytest

import supertool

BS = chr(92)
NL = chr(10)
Q = chr(34)
Q3 = chr(39) * 3


def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "p.toml"
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def test_the_write_refusal_names_every_offending_field(tmp_path: Path) -> None:
    """Three offending fields -- `content` and `new` twice -- all named at once.

    A payload with three offenders used to cost three round-trips: fix the
    first named field, resend, learn about the next. This asserts the whole
    set is visible from the first refusal.
    """
    body = (
        "path = " + Q + "notes.txt" + Q + NL
        + "content = " + Q3 + "a" + BS * 2 + "b" + Q3 + NL
        + "old = " + Q + "x" + Q + NL
        + "new = " + Q3 + "c" + BS * 2 + "d" + Q3 + NL
    )
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(_write(tmp_path, body))
    msg = str(excinfo.value)
    assert "`content`" in msg, msg
    assert "`new`" in msg, msg


def test_literal_backslashes_misplaced_names_every_offending_index(
    tmp_path: Path,
) -> None:
    """`literal_backslashes` mis-set inside two `[[ops]]` entries -- both named.

    A templated batch is the likely way to make this mistake more than once,
    and a message naming only `ops[0]` sends the caller back for a second
    round-trip to learn about `ops[2]`.
    """
    body = (
        "[[ops]]" + NL
        + "op = " + Q + "edit" + Q + NL
        + "path = " + Q + "x.txt" + Q + NL
        + "old = " + Q + "a" + Q + NL
        + "new = " + Q + "b" + Q + NL
        + "literal_backslashes = true" + NL
        + "[[ops]]" + NL
        + "op = " + Q + "edit" + Q + NL
        + "path = " + Q + "y.txt" + Q + NL
        + "old = " + Q + "a" + Q + NL
        + "new = " + Q + "b" + Q + NL
        + "[[ops]]" + NL
        + "op = " + Q + "edit" + Q + NL
        + "path = " + Q + "z.txt" + Q + NL
        + "old = " + Q + "a" + Q + NL
        + "new = " + Q + "b" + Q + NL
        + "literal_backslashes = true" + NL
    )
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(_write(tmp_path, body))
    msg = str(excinfo.value)
    assert "ops[0]" in msg, msg
    assert "ops[2]" in msg, msg


def test_literal_backslashes_misplaced_single_index_reads_naturally(
    tmp_path: Path,
) -> None:
    """The single-offender case keeps its original, un-pluralised phrasing.

    Reviewer finding (self-review, #1720): the first cut of this fix built
    the single-index sentence with no comma before "where it does nothing",
    a run-on this test's earlier substring-only assertions did not catch.
    """
    body = (
        "[[ops]]" + NL
        + "op = " + Q + "edit" + Q + NL
        + "path = " + Q + "x.txt" + Q + NL
        + "old = " + Q + "a" + Q + NL
        + "new = " + Q + "b" + Q + NL
        + "literal_backslashes = true" + NL
    )
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(_write(tmp_path, body))
    msg = str(excinfo.value)
    assert "ops[0]" in msg, msg
    assert "and `ops[" not in msg, msg
    assert "`ops[0]`, where it does nothing" in msg, msg
