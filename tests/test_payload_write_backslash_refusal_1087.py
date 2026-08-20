r"""A payload that would WRITE a doubled backslash is refused, not warned (#1087).

#1027 made this a note: the detector fired, the bytes landed, and the receipt
said so. The half that already worked was `old` -- a doubled anchor cannot
match, so the runner reports a skip and nothing reaches disk. The half that did
not was `new` / `content`: the anchor matches, the write lands, every validator
passes (two backslashes are legal in every language this repo edits), and the
author finds out from behaviour, usually a CI round later.

Same detector, same evidence, opposite consequence. This file pins the write
side to the safe one.

The reason #1027 chose a warning was real and is answered rather than
overruled: refusing an arbitrary-offset pattern strands the author who
genuinely means two characters. So the refusal is only reachable because
`literal_backslashes` (#1096) exists to say so, and every test here that
refuses has a sibling in test_payload_literal_backslashes_optin_1096.py that
lands the same bytes with the flag set. A suppressible refusal records the
decision in the payload; an unsuppressible warning makes it on the author's
behalf.

`old` keeps the note. It is not write-bound, and the no-match diagnostic is
already the loudest thing in the set.
"""
from pathlib import Path

import supertool

BS = chr(92)
NL = chr(10)
Q3 = chr(39) * 3
D3 = chr(34) * 3


def _payload(tmp_path: Path, body: str, name: str = "p.toml") -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(BS, BS * 2) + chr(34)


def _target(tmp_path: Path, body: str, name: str = "t.py") -> Path:
    t = tmp_path / name
    t.write_text(body, encoding="utf-8")
    return t


def _edit_body(target: Path, new_field: str) -> str:
    return (
        "path = " + _toml_path(target) + NL
        + "old = " + Q3 + 'PAT = "x"' + Q3 + NL
        + new_field + NL
    )


LITERAL_DOUBLE = "new = " + Q3 + 'PAT = "' + BS * 2 + 'd+"' + Q3


def test_a_doubled_backslash_in_new_refuses_and_writes_nothing(tmp_path: Path) -> None:
    """The issue. The file must be byte-identical afterwards -- a refusal that
    still wrote would be the reported defect with a louder receipt."""
    before = 'PAT = "x"' + NL
    target = _target(tmp_path, before)
    out = supertool.dispatch("edit:" + _payload(tmp_path, _edit_body(target, LITERAL_DOUBLE)))
    assert "ERROR" in out, out
    assert "refused" in out.lower(), out
    assert target.read_text(encoding="utf-8") == before, "the write landed anyway"


def test_the_refusal_names_the_field_and_the_way_out(tmp_path: Path) -> None:
    """A refusal the caller cannot act on is a wall. It has to name which field
    carried the pair and the one key that says `I meant two`."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    out = supertool.dispatch("edit:" + _payload(tmp_path, _edit_body(target, LITERAL_DOUBLE)))
    assert "new" in out, out
    assert "literal_backslashes" in out, out


def test_paste_content_refuses_too(tmp_path: Path) -> None:
    """`paste` is where the report came from, and a created file has no prior
    bytes to compare against -- nothing downstream can catch it."""
    target = tmp_path / "created.py"
    body = (
        "path = " + _toml_path(target) + NL
        + "content = " + Q3 + 'PAT = "' + BS * 2 + 'd+"' + Q3 + NL
    )
    out = supertool.dispatch("paste:" + _payload(tmp_path, body))
    assert "ERROR" in out, out
    assert not target.exists(), "a refused paste created the file"


def test_a_doubled_backslash_in_old_is_still_only_a_note(tmp_path: Path) -> None:
    """`old` is not write-bound: the anchor cannot match, the runner reports the
    skip, and nothing reaches disk. Refusing it would buy nothing and would cost
    the author a second round-trip on a call that was already safe."""
    target = _target(tmp_path, 'PAT = "' + BS + 'd+"' + NL)
    body = (
        "path = " + _toml_path(target) + NL
        + "old = " + Q3 + 'PAT = "' + BS * 2 + 'd+"' + Q3 + NL
        + "new = " + Q3 + 'PAT = "y"' + Q3 + NL
    )
    out = supertool.dispatch("edit:" + _payload(tmp_path, body))
    assert "payload refused" not in out.lower(), out
    assert "literal block" in out.lower(), out
    assert "1 skipped" in out, "the anchor should have missed: " + out


def test_a_batch_sub_op_write_field_refuses_the_whole_payload(tmp_path: Path) -> None:
    """The payload parses once, before any sub-op runs, so the refusal covers a
    batch by construction. Asserting it anyway: the reported run was nine ops,
    and a guard that only read the single-op route would have missed all nine."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    body = (
        "[[ops]]" + NL
        + 'op = "edit"' + NL
        + "path = " + _toml_path(target) + NL
        + "old = " + Q3 + 'PAT = "x"' + Q3 + NL
        + "new = " + Q3 + 'PAT = "' + BS * 2 + 'd+"' + Q3 + NL
    )
    out = supertool.dispatch("batch:" + _payload(tmp_path, body))
    assert "ERROR" in out, out
    assert target.read_text(encoding="utf-8") == 'PAT = "x"' + NL


def test_a_batch_note_says_which_op_each_field_belongs_to(tmp_path: Path) -> None:
    """The narrower half of #1087. Six ops in the payload and a bare `new` names
    the field but not the op, so the reader re-derives by hand which of six was
    affected -- the one thing the tool already knew."""
    n = supertool._PAYLOAD_DBS_MAX_FIELDS + 1
    chunks = []
    for i in range(n):
        t = _target(tmp_path, 'PAT = "x"' + NL, "f" + str(i) + ".py")
        chunks.append(
            "[[ops]]" + NL
            + 'op = "edit"' + NL
            + "path = " + _toml_path(t) + NL
            + "old = " + Q3 + 'A' + BS * 2 + 'd' + Q3 + NL
            + "new = " + Q3 + "B" + Q3 + NL
        )
    out = supertool.dispatch("batch:" + _payload(tmp_path, "".join(chunks)))
    assert "ops[0]" in out, out
    assert "ops[" + str(n - 1) + "]" in out, (
        "the elided op is not named: " + out
    )


def test_a_basic_block_pair_is_still_never_touched(tmp_path: Path) -> None:
    """A basic block spells one backslash with two, which is the correct and
    only spelling there. A refusal that fired here would refuse its own remedy."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    basic = "new = " + D3 + 'PAT = "' + BS * 4 + 'd+"' + D3
    out = supertool.dispatch("edit:" + _payload(tmp_path, _edit_body(target, basic)))
    assert "ERROR" not in out, out
    assert target.read_text(encoding="utf-8") == 'PAT = "' + BS * 2 + 'd+"' + NL


# Fields of a mutating op that are NOT written to the file, each excluded on
# purpose. Anything not here and not in the write set is a new field nobody
# classified.
_NOT_WRITTEN = {
    "path",    # a target, and on Windows it spells a separator with exactly two
    "old",     # an anchor: a doubled one cannot match and the runner says so
    "start",   # replace_lines line numbers
    "end",
    "script",  # vim: an instruction language, not bytes -- the tool cannot say
               # from the payload what the file ends up holding
}


def test_every_mutating_field_is_classified_as_written_or_not() -> None:
    """The absence this change could produce, guarded at the point it appears.

    The refusal reads an ALLOWLIST of write-bound field names. An op that gains
    a new content field later would not be added to it by anything, and the
    failure mode is silence: the field falls back to the post-write note, which
    is exactly the defect #1087 was filed about, re-introduced for one op and
    invisible everywhere.

    Nothing else checks this -- it is a check that should exist rather than a
    line in the diff -- so it is asserted against the op registry, which is
    generated from the op syntax strings and therefore moves when an op does.
    """
    supertool._build_at_file_registry()
    unclassified = {}
    for op in supertool._OP_TARGETS:
        for field, _req, _rest in supertool._AT_FILE_REGISTRY.get(op) or []:
            if field in supertool._PAYLOAD_DBS_WRITE_KEYS or field in _NOT_WRITTEN:
                continue
            unclassified.setdefault(field, []).append(op)
    assert not unclassified, (
        "new payload field(s) on a mutating op, classified by nobody: "
        + repr(unclassified)
        + " -- add each to _PAYLOAD_DBS_WRITE_KEYS if its bytes reach the file, "
        "or to _NOT_WRITTEN here with the reason it does not"
    )


def test_a_run_of_four_is_refused_too(tmp_path: Path) -> None:
    """Reversed by #1860, and the reversal is the point of the test.

    This asserted the opposite until #1860, on the ground that "four was
    counted, not produced by reflex". Four was produced by reflex, twice, and
    the mechanism the original rationale could not have had is that THIS
    REFUSAL manufactures it: four is what a caller writes immediately after
    reading the two-backslash refusal and doubling again to escape the escape.
    The guard's own premise -- the run reaches disk at full length, passes
    every validator, and is wrong only in string contents -- was always true
    of four.

    The pairing that keeps this honest is in
    test_receipt_arity_and_identity_1855_1860.py: odd runs must still WRITE, so
    the widening cannot have become "refuse every backslash"."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    before = target.read_text(encoding="utf-8")
    quad = "new = " + Q3 + 'PAT = "' + BS * 4 + 'd+"' + Q3
    out = supertool.dispatch("edit:" + _payload(tmp_path, _edit_body(target, quad)))
    assert "ERROR" in out, out
    assert target.read_text(encoding="utf-8") == before, "the write landed anyway"
