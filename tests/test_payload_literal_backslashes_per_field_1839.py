r"""`literal_backslashes` accepts a per-field list, not just a bare `true` (#1839).

#1096 gave the payload one way to say "I meant two characters", and it was
payload-wide by design: the docstring at the time argued that a per-field key
inside an `[[ops]]` table READS as scoped to that op and could not be, because
the doubled-backslash scanner works off the raw source, where op boundaries
are a line-counting heuristic rather than a fact.

That argument is about a key placed INSIDE an op table, scoped by position.
It says nothing about a key at the TOP level whose value names field NAMES --
the scanner already carries the bare key (`new`, `content`, ...) per finding,
with no op-boundary heuristic involved, so filtering on it needs no scope
that could lie. `literal_backslashes = ["new"]` exempts every occurrence
whose bare key is `new`, in every op the payload carries, and leaves every
other field's occurrences subject to the refusal exactly as before.

What this does NOT solve, and was never asked to: two ops that both carry a
`new` field and disagree with EACH OTHER about that field's own intent still
share one key. That payload is still two payloads, per the original
reasoning, which continues to hold for that case.

Every test here that asserts the narrow form works is paired with one that
asserts the bare `true` form still applies to every field -- without that
pairing, an assertion that the list exempts the field it names would also
pass against an implementation that quietly widened any non-empty list into
"opt in to everything" (#1839's own warning about the shape of that bug).
"""
from pathlib import Path

import supertool

BS = chr(92)
NL = chr(10)
Q3 = chr(39) * 3


def _payload(tmp_path: Path, body: str) -> str:
    p = tmp_path / "p.toml"
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(BS, BS * 2) + chr(34)


WANTED = 'PAT = "' + BS * 2 + 'd+"'


def _batch_body(edit_target: Path, paste_target: Path, head: str) -> str:
    return (
        head
        + "[[ops]]" + NL
        + 'op = "edit"' + NL
        + "path = " + _toml_path(edit_target) + NL
        + "old = " + Q3 + 'PAT = "x"' + Q3 + NL
        + "new = " + Q3 + WANTED + Q3 + NL
        + "[[ops]]" + NL
        + 'op = "paste"' + NL
        + "path = " + _toml_path(paste_target) + NL
        + "content = " + Q3 + WANTED + Q3 + NL
    )


def test_a_listed_field_is_exempt_and_an_unlisted_field_still_refuses(
    tmp_path: Path,
) -> None:
    """The issue's own acceptance shape: `new` opts in, `content` does not, in
    one payload. The whole batch still refuses -- the parse-time refusal
    covers every op by construction -- but the refusal message must single out
    `content` and must NOT flag `new`, which is the only observable proof that
    the exemption was scoped to the field it named rather than to the payload."""
    edit_target = tmp_path / "e.py"
    edit_target.write_text('PAT = "x"' + NL, encoding="utf-8")
    paste_target = tmp_path / "p.py"
    body = _batch_body(
        edit_target, paste_target, "literal_backslashes = " + repr(["new"]).replace("'", '"') + NL
    )
    out = supertool.dispatch("batch:" + _payload(tmp_path, body))
    assert "ERROR" in out, out
    assert "ops[1].content" in out, "the unlisted field must be named: " + out
    assert "ops[0].new" not in out, "the listed field must not be flagged: " + out
    assert not paste_target.exists(), "a refused batch created a file"
    assert edit_target.read_text(encoding="utf-8") == 'PAT = "x"' + NL, (
        "a refused batch wrote to the edit target"
    )


def test_every_field_named_in_the_list_is_exempt(tmp_path: Path) -> None:
    """Both fields named -> both pass, in one payload, with no batch-wide
    refusal at all."""
    edit_target = tmp_path / "e.py"
    edit_target.write_text('PAT = "x"' + NL, encoding="utf-8")
    paste_target = tmp_path / "p.py"
    body = _batch_body(
        edit_target, paste_target,
        "literal_backslashes = " + repr(["new", "content"]).replace("'", '"') + NL,
    )
    out = supertool.dispatch("batch:" + _payload(tmp_path, body))
    assert "ERROR" not in out, out
    assert edit_target.read_text(encoding="utf-8") == WANTED + NL
    assert paste_target.read_text(encoding="utf-8") == WANTED + NL


def test_bare_true_still_applies_to_every_field_not_just_a_named_one(
    tmp_path: Path,
) -> None:
    """The control. Same two differently-named fields, `literal_backslashes =
    true` instead of a list -- must still exempt BOTH, unnarrowed by the new
    list-handling code path added to serve the case above."""
    edit_target = tmp_path / "e.py"
    edit_target.write_text('PAT = "x"' + NL, encoding="utf-8")
    paste_target = tmp_path / "p.py"
    body = _batch_body(edit_target, paste_target, "literal_backslashes = true" + NL)
    out = supertool.dispatch("batch:" + _payload(tmp_path, body))
    assert "ERROR" not in out, out
    assert edit_target.read_text(encoding="utf-8") == WANTED + NL
    assert paste_target.read_text(encoding="utf-8") == WANTED + NL


def test_field_name_matching_is_case_insensitive(tmp_path: Path) -> None:
    """The write-key check elsewhere in this file matches on `.lower()`
    (`_PAYLOAD_DBS_WRITE_KEYS`); the list form should not silently require
    exact case to line up with a lowercase field name."""
    target = tmp_path / "t.py"
    target.write_text('PAT = "x"' + NL, encoding="utf-8")
    body = (
        "literal_backslashes = " + repr(["New"]).replace("'", '"') + NL
        + "path = " + _toml_path(target) + NL
        + "old = " + Q3 + 'PAT = "x"' + Q3 + NL
        + "new = " + Q3 + WANTED + Q3 + NL
    )
    out = supertool.dispatch("edit:" + _payload(tmp_path, body))
    assert "ERROR" not in out, out
    assert target.read_text(encoding="utf-8") == WANTED + NL


def test_the_list_form_inside_an_ops_table_is_refused_not_ignored(
    tmp_path: Path,
) -> None:
    """The misplaced-key refusal (#1096) is value-agnostic -- it fires on the
    KEY being present inside `[[ops]]`, regardless of whether its value is a
    bool or a list. Pinned here because the list form is new and a value-type
    check could plausibly have been added ahead of it by accident."""
    target = tmp_path / "t.py"
    target.write_text('PAT = "x"' + NL, encoding="utf-8")
    body = (
        "[[ops]]" + NL
        + 'op = "edit"' + NL
        + "literal_backslashes = " + repr(["new"]).replace("'", '"') + NL
        + "path = " + _toml_path(target) + NL
        + "old = " + Q3 + 'PAT = "x"' + Q3 + NL
        + "new = " + Q3 + WANTED + Q3 + NL
    )
    out = supertool.dispatch("batch:" + _payload(tmp_path, body))
    assert "ERROR" in out, out
    assert "top level" in out.lower(), out
    assert target.read_text(encoding="utf-8") == 'PAT = "x"' + NL


def test_the_suggested_list_keeps_an_already_exempted_field(tmp_path: Path) -> None:
    """The suggested `literal_backslashes = [...]` is explicitly documented as
    something to PASTE, not adapt. If it were built only from the fields still
    refused, pasting it over an existing list-form exemption would silently
    drop the field the payload had already settled -- the caller follows the
    tool's own instruction and gets a payload that refuses on the very next
    submission, which is the round-trip this whole feature exists to remove.

    `new` is already exempted here (`literal_backslashes = ["new"]`); `content`
    is not, so the refusal fires -- but its suggested list must carry BOTH."""
    edit_target = tmp_path / "e.py"
    edit_target.write_text('PAT = "x"' + NL, encoding="utf-8")
    paste_target = tmp_path / "p.py"
    body = _batch_body(
        edit_target, paste_target,
        "literal_backslashes = " + repr(["new"]).replace("'", '"') + NL,
    )
    out = supertool.dispatch("batch:" + _payload(tmp_path, body))
    assert "ERROR" in out, out
    assert 'literal_backslashes = ["new", "content"]' in out, (
        "the suggested list dropped the field already exempted: " + out
    )
