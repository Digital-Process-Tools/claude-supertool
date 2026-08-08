r"""A `'''` payload field carrying `\\` lands two characters and reports `edited` (#1027).

The write path's own silent-success case. TOML triple-single-quoted literals
process no escapes, so `\\d` in a payload reaches the file as backslash-
backslash-d. When the doubling is in `old` the anchor cannot match and the
runner says so — that half is already safe (#380 even names the cause). When it
is in `new`, the anchor matches, the bytes land, `edited` prints, and the
validators agree, because doubled backslashes are legal in nearly every language
this repo edits. Nothing anywhere says the write was wrong.

The guard never rewrites. Collapsing `\\` to `\` would guess at intent and some
payloads genuinely want two characters, so the tests below assert that nothing
is silently corrected.

**The severity moved in #1087 and this file moved with it.** The write-bound
fields — `new` and `content` — are REFUSED now, not noted: the note fired after
the bytes had landed, and every validator passed them, because two backslashes
are legal in every language this repo edits.

The objection recorded here originally — that refusing a pattern with no fixed
position strands the author who genuinely means two characters — was correct,
and it is answered rather than overruled. `literal_backslashes = true` (#1096)
is the spelling that says so, which is what makes the refusal suppressible; a
suppressible refusal records the decision in the payload, an unsuppressible
warning makes it on the author's behalf. The refusal, the flag and the
bytes-on-disk assertion live in test_payload_write_backslash_refusal_1087.py
and test_payload_literal_backslashes_optin_1096.py.

`old` keeps the note, and that is the half this file still pins: it is never
written to a file, a doubled anchor cannot match, and the runner reports the
skip.

Boundaries, each pinned by a negative test — a warning that fires on the correct
spelling is a warning authors learn to skip, which is the same as not having one:

* Basic `\"\"\"` blocks are never flagged. There `\\` is the correct and only
  spelling of one backslash; flagging it would flag the fix.
* Runs of three or more are never flagged. A longer run was counted, not doubled
  by reflex.

Second, from the same report: `batch:` prints `[result] N ops run, M writes,
K skipped` below a long validators block, so the load-bearing count is what a
`tail` misses. It is now printed at the top as well.
"""
from pathlib import Path

import supertool

BS = chr(92)
NL = chr(10)
Q3 = chr(39) * 3
D3 = chr(34) * 3


def _write_payload(tmp_path: Path, body: str, name: str = "p.toml") -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def _toml_path(target: Path) -> str:
    """A payload `path =` as a basic string, with separators escaped.

    Windows absolute paths are full of backslashes; a basic string is where a
    wanted backslash is spelled with two, so this is also the spelling the guard
    under test must NOT flag.
    """
    return chr(34) + str(target).replace(BS, BS * 2) + chr(34)


def _target(tmp_path: Path, body: str, name: str = "t.py") -> Path:
    t = tmp_path / name
    t.write_text(body, encoding="utf-8")
    return t


# The reported shape: the anchor matches, and `new` doubles a backslash.
def _edit_payload(tmp_path: Path, target: Path, new_field: str) -> str:
    body = (
        "path = " + _toml_path(target) + NL
        + "old = " + Q3 + 'PAT = "x"' + Q3 + NL
        + new_field + NL
    )
    return _write_payload(tmp_path, body)


LITERAL_DOUBLE = "new = " + Q3 + 'PAT = "' + BS * 2 + 'd+"' + Q3
BASIC_DOUBLE = "new = " + D3 + 'PAT = "' + BS * 4 + 'd+"' + D3
LITERAL_SINGLE = "new = " + Q3 + 'PAT = "' + BS + 'd+"' + Q3
LITERAL_QUAD = "new = " + Q3 + 'PAT = "' + BS * 4 + 'd+"' + Q3


def test_a_doubled_backslash_in_a_literal_new_is_named(tmp_path: Path) -> None:
    """The whole issue: before #1027 the receipt said nothing at all about the
    two characters it had just written. It now names the field and quotes the
    sequence back — as a refusal since #1087, but the naming is what this test
    is about and it is asserted without depending on the severity."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    out = supertool.dispatch("edit:" + _edit_payload(tmp_path, target, LITERAL_DOUBLE))
    assert BS * 2 in out, "the offending sequence is quoted back: " + out
    assert "new" in out, "the field is named: " + out
    assert "literal" in out.lower(), out


def test_the_tool_never_silently_rewrites_a_pair(tmp_path: Path) -> None:
    """A guard in the write path that silently corrected would be worse than the
    bug. Whichever severity it uses, the one thing it must not do is put
    different bytes on disk from the ones the payload carried — so the file is
    asserted unchanged rather than corrected."""
    before = 'PAT = "x"' + NL
    target = _target(tmp_path, before)
    supertool.dispatch("edit:" + _edit_payload(tmp_path, target, LITERAL_DOUBLE))
    on_disk = target.read_text(encoding="utf-8")
    assert on_disk in (before, 'PAT = "' + BS * 2 + 'd+"' + NL), on_disk


def test_a_basic_block_pair_is_not_flagged(tmp_path: Path) -> None:
    """`\\\\` inside a basic block is ONE backslash in the value — the correct
    spelling. Flagging it would fire the guard on its own remedy."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    out = supertool.dispatch("edit:" + _edit_payload(tmp_path, target, BASIC_DOUBLE))
    assert target.read_text(encoding="utf-8") == 'PAT = "' + BS * 2 + 'd+"' + NL
    assert "literal block" not in out.lower(), out


def test_a_single_backslash_in_a_literal_is_not_flagged(tmp_path: Path) -> None:
    """The common, correct payload. If this warns, nobody reads the warning."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    out = supertool.dispatch("edit:" + _edit_payload(tmp_path, target, LITERAL_SINGLE))
    assert target.read_text(encoding="utf-8") == 'PAT = "' + BS + 'd+"' + NL
    assert "literal block" not in out.lower(), out


def test_a_run_of_four_is_not_flagged(tmp_path: Path) -> None:
    """Four backslashes were counted, not doubled by escape reflex. Warning on a
    deliberate run is how the signal gets spent."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    out = supertool.dispatch("edit:" + _edit_payload(tmp_path, target, LITERAL_QUAD))
    assert "literal block" not in out.lower(), out


def test_a_doubled_backslash_in_old_is_still_named(tmp_path: Path) -> None:
    """`old` is the half that already fails loudly, but it fails only *after* the
    anchor misses. Naming it at parse time is the same fact, one call earlier."""
    target = _target(tmp_path, 'PAT = "' + BS + 'd+"' + NL)
    body = (
        "path = " + _toml_path(target) + NL
        + "old = " + Q3 + 'PAT = "' + BS * 2 + 'd+"' + Q3 + NL
        + "new = " + Q3 + 'PAT = "y"' + Q3 + NL
    )
    out = supertool.dispatch("edit:" + _write_payload(tmp_path, body))
    assert "old" in out
    # Not merely "some message mentions backslashes" — #380's miss diagnostic
    # already does that, and it only speaks once the anchor has failed. The
    # parse-time guard has its own words.
    assert "literal block" in out.lower(), out


def test_a_batch_sub_op_field_is_covered(tmp_path: Path) -> None:
    """The reported run was a nine-op batch. A guard that only reads the single-op
    route would have missed every one of them. The payload parses once, before
    any sub-op runs, so the batch is covered by the same scan."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    body = (
        "[[ops]]" + NL
        + 'op = "edit"' + NL
        + "path = " + _toml_path(target) + NL
        + "old = " + Q3 + 'PAT = "' + BS * 2 + 'd+"' + Q3 + NL
        + "new = " + Q3 + 'PAT = "y"' + Q3 + NL
    )
    out = supertool.dispatch("batch:" + _write_payload(tmp_path, body))
    assert "literal" in out.lower(), out
    assert "ops[0].old" in out, "the note names which op it means: " + out


# --- the batch result line, at the top as well as the bottom -----------------


def _two_op_batch(tmp_path: Path) -> str:
    """One op that writes, one whose anchor cannot match."""
    good = _target(tmp_path, "A = 1" + NL, "good.py")
    bad = _target(tmp_path, "B = 2" + NL, "bad.py")
    body = (
        "[[ops]]" + NL
        + 'op = "edit"' + NL
        + "path = " + _toml_path(good) + NL
        + "old = " + Q3 + "A = 1" + Q3 + NL
        + "new = " + Q3 + "A = 2" + Q3 + NL
        + NL
        + "[[ops]]" + NL
        + 'op = "edit"' + NL
        + "path = " + _toml_path(bad) + NL
        + "old = " + Q3 + "NOT PRESENT" + Q3 + NL
        + "new = " + Q3 + "X" + Q3 + NL
    )
    return _write_payload(tmp_path, body, "batch.toml")


def test_the_batch_count_is_printed_before_the_ops_run_out(tmp_path: Path) -> None:
    """`[result]` sat below the validators block, which is where `tail` lands and
    reads `git-status : ok` as success. The count now leads as well."""
    out = supertool.dispatch("batch:" + _two_op_batch(tmp_path))
    head = out.split("edited", 1)[0]
    assert "[result]" in head, "the count is not above the first op result:" + NL + out
    assert "1 skipped" in head, head


def test_the_batch_count_still_ends_the_output(tmp_path: Path) -> None:
    """Leading it must not move it. `| tail -1` is the documented read and #381's
    branch line has to stay last."""
    out = supertool.dispatch("batch:" + _two_op_batch(tmp_path))
    assert out.count("[result]") == 2, out
    assert "1 skipped" in out.rstrip().rsplit("[result]", 1)[-1], out


# --- the note must not outlive the call that raised it ----------------------


def _grep_payload(tmp_path: Path, target: Path) -> str:
    body = (
        "pattern = " + Q3 + BS * 2 + "d+" + Q3 + NL
        + "path = " + _toml_path(target) + NL
    )
    return _write_payload(tmp_path, body, "g.toml")


def test_a_read_op_payload_raises_no_note(tmp_path: Path) -> None:
    """The note is about the write path. A `grep` pattern is a regex, not file
    content -- nothing lands, and a doubled backslash there is a different
    question with a different answer. Raising it would be noise on an op that
    cannot misfile a byte."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    out = supertool.dispatch("grep:" + _grep_payload(tmp_path, target))
    assert "literal block" not in out.lower(), out


def test_the_note_queue_is_empty_when_the_call_returns(tmp_path: Path) -> None:
    """The invariant, asserted at the source rather than through a symptom.

    A note parked in a module global and drained at the end of `dispatch` leaks
    through every early `return` between the two -- and it then prints attached
    to whatever op runs next, which is a claim about a payload that op never
    had. That is this repository's own defect wearing the fix's clothes, so the
    queue is checked directly instead of hoping the next call happens to show
    it."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    supertool.dispatch("grep:" + _grep_payload(tmp_path, target))
    assert supertool._PAYLOAD_WARNINGS == []
    supertool.dispatch("edit:" + _edit_payload(tmp_path, target, LITERAL_DOUBLE))
    assert supertool._PAYLOAD_WARNINGS == []


def test_the_note_does_not_attach_itself_to_the_next_op(tmp_path: Path) -> None:
    """The symptom the invariant above prevents, kept as its own test because it
    is the thing a reader would actually see."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    supertool.dispatch("grep:" + _grep_payload(tmp_path, target))
    out = supertool.dispatch("read:" + str(target))
    assert "literal block" not in out.lower(), out


def test_a_clean_single_op_gains_no_second_count(tmp_path: Path) -> None:
    """Scoped to `batch:`. The single-op receipt is three lines and its footer is
    already adjacent to them; a duplicate there is noise, not a signal."""
    target = _target(tmp_path, "A = 1" + NL)
    body = (
        "path = " + _toml_path(target) + NL
        + "old = " + Q3 + "A = 1" + Q3 + NL
        + "new = " + Q3 + "A = 2" + Q3 + NL
    )
    out = supertool.dispatch("edit:" + _write_payload(tmp_path, body))
    assert out.count("[result]") == 1, out
