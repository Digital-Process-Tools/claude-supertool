"""Every doubled-backslash occurrence is located, not just the first (#1808, #1814, #1819).

The gate is not what these three issues are about. Every refusal reported was
correct and at least two caught real payload bugs. What they are about is the
cost of answering one: today a refusal names the field, a total count and the
head of ONE line, and the retry is the whole payload. Four full re-sends in one
agent run, one on a 14 KB test file and one on a 6 KB pull-request payload.

Measured before this file was written, against a payload whose ``content`` field
carries three offending lines::

    `content` (3x): A = re.compile(r'BSBSd+')

One line of three, no payload line number, no column, no occurrence index, and
the excerpt is the head of the line rather than the neighbourhood of the pair --
so on a long line the reported text does not contain the offending bytes at all.

What the three ask for, and what is pinned here:

* #1814 -- every offending line, together, with line numbers. The scan has
  already parsed the whole payload when it reports one.
* #1808 -- the OCCURRENCE with context either side, a line and column and a
  1-based index out of the total, and a statement of which direction the fix
  goes: "needs literal_backslashes" and "has doubled backslashes that wanted to
  be single" are opposite failures that read identically today.
* #1819 -- the payload's own line number, same fact as #1808's.

Two things #1814 claimed that the code did not do, checked rather than assumed
and recorded here so the next reader does not re-derive them:

* Across FIELDS the refusal already reported all of them, joined with "; ".
  ``test_every_offending_field_is_still_named`` pins that; it passed before this
  change. The short-circuit was inside one field, on ``re.search`` for the first
  hit, and that is what moved.
* #1819's third ask -- an odd literal-block delimiter count refused at parse
  time -- already shipped as ``_toml_delimiter_hint`` (#394).
  ``test_an_odd_delimiter_run_is_already_explained`` pins the behaviour that
  already existed, so it is not re-implemented and not silently dropped.

Every "must not fire" case below has a "must fire" partner in the same fixture:
a harness that cannot see a refusal at all would otherwise pass the whole
negative half.
"""
from pathlib import Path

import supertool

BS = chr(92)
NL = chr(10)
Q3 = chr(39) * 3
D3 = chr(34) * 3
PAIR = BS * 2


def _payload(tmp_path: Path, body: str, name: str = "p.toml") -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(BS, PAIR) + chr(34)


def _paste_body(target: Path, content: str, head: str = "") -> str:
    return (
        head
        + "path = " + _toml_path(target) + NL
        + "content = " + Q3 + NL + content + NL + Q3 + NL
    )


THREE_LINES = NL.join([
    "import re",
    "A = re.compile(r'" + PAIR + "d+')",
    "filler that is long enough to push the next hit off any head excerpt",
    "B = 'x' * 40 + '" + PAIR + "n' + 'y' * 40",
    "more filler",
    "C = '" + PAIR + "302'",
])


# A neutral extension on purpose. These fixtures are about the payload gate,
# which runs before any op does; a `.py` target routes the same bytes through
# the language validators too, and `test_the_optin_still_suppresses...` then
# failed for a rolled-back write rather than for the refusal it was asserting
# about. A negative case with a positive control fails loudly there; one
# without would have passed.
TARGET = "created.txt"


def _refuse(tmp_path: Path, content: str, head: str = "") -> str:
    target = tmp_path / TARGET
    out = supertool.dispatch(
        "paste:" + _payload(tmp_path, _paste_body(target, content, head)))
    assert not target.exists(), "a refused paste created the file: " + out
    return out


# --- must fire -------------------------------------------------------------


def test_every_offending_line_in_one_field_is_reported(tmp_path: Path) -> None:
    """#1814. Three offending lines in one field cost three full re-sends,
    because each refusal named only the first. The payload was parsed once; all
    three were in hand at the moment it reported one."""
    out = _refuse(tmp_path, THREE_LINES)
    # content opens on payload line 2, so the body lines are 3..8.
    for line_no in (4, 6, 8):
        assert "line " + str(line_no) in out, (
            "offending payload line " + str(line_no) + " not reported: " + out)


def test_each_occurrence_carries_an_index_out_of_the_total(tmp_path: Path) -> None:
    """#1808. `(3x)` says how many without saying which. An index makes the
    list countable against the total, so a reader knows none was elided."""
    out = _refuse(tmp_path, THREE_LINES)
    assert "1/3" in out, out
    assert "3/3" in out, out


def test_each_occurrence_carries_a_payload_line_and_column(tmp_path: Path) -> None:
    """#1819 and #1808. The column is what the line number cannot give on a
    line holding more than one pair."""
    out = _refuse(tmp_path, THREE_LINES)
    assert "column" in out, out


def test_the_excerpt_is_centred_on_the_pair_not_the_head_of_the_line(
        tmp_path: Path) -> None:
    """#1808's own report: a shell printf format is frequently 200 characters
    into a long line, and 40 characters of the head of that line does not
    contain the offending bytes at all.

    The must-fire partner of the negative below: if the excerpt logic silently
    produced an empty string this would fail rather than pass."""
    line = "X = '" + "a" * 200 + PAIR + "d" + "b" * 200 + "'"
    out = _refuse(tmp_path, line)
    assert PAIR + "d" in out, (
        "the reported excerpt does not contain the offending bytes: " + out)
    assert "b" * 8 in out, "no context on the right of the pair: " + out


def test_the_caret_line_points_at_the_pair(tmp_path: Path) -> None:
    """A column number the reader has to count to is a column number they will
    count wrong. The caret is checked against the excerpt it sits under rather
    than against a hardcoded offset."""
    out = _refuse(tmp_path, "A = re.compile(r'" + PAIR + "d+')")
    lines = out.splitlines()
    carets = [i for i, ln in enumerate(lines) if ln.strip() == "^^"]
    assert carets, "no caret line under any excerpt: " + out
    for i in carets:
        excerpt = lines[i - 1]
        col = lines[i].index("^")
        assert excerpt[col:col + 2] == PAIR, (
            "caret at column " + str(col) + " does not sit on the pair:" + NL
            + excerpt + NL + lines[i])


def test_the_two_directions_are_named_as_opposite(tmp_path: Path) -> None:
    """#1808. "needs literal_backslashes = true" and "has doubled backslashes
    that JSON wanted single" are opposite fixes, and the message read the same
    for both. The tool cannot know which was meant; it can say that it cannot,
    which is the difference between a reader deciding and a reader guessing."""
    out = _refuse(tmp_path, THREE_LINES)
    assert "OPPOSITE" in out, out
    assert "literal_backslashes" in out, out


def test_occurrences_beyond_the_cap_are_named_by_line_not_counted(
        tmp_path: Path) -> None:
    """#1087's precedent, applied one level down. `and 3 further occurrences`
    withholds exactly the fact the reader needs and sends them back to the
    payload to re-derive it by hand."""
    n = supertool._PAYLOAD_DBS_MAX_OCCURRENCES + 2
    body = NL.join("v" + str(i) + " = '" + PAIR + "d'" for i in range(n))
    out = _refuse(tmp_path, body)
    last = 3 + n - 1          # content opens on payload line 2; body starts at 3
    assert "1/" + str(n) in out, out
    tail = [ln for ln in out.splitlines() if "more, at payload line" in ln]
    assert tail, "nothing said the remaining occurrences existed: " + out
    assert str(last) in tail[0], (
        "the elided occurrence's line number is not named: " + tail[0])


def test_a_caret_that_cannot_be_placed_says_so_instead_of_pointing(
        tmp_path: Path) -> None:
    """The third state, and the reason it exists.

    `_flat_field` may `repr()` its argument (#886), which is not
    offset-preserving, so a caret computed on the unflattened excerpt would
    point confidently at the wrong character of the flattened one. U+2028 is a
    line separator by `str.splitlines()` but not by `chr(10)`, so it survives
    inside the excerpt -- the exact mechanism of #1583, one level down.

    Its must-fire partner is `test_the_caret_line_points_at_the_pair`: without
    that one, an implementation that never drew a caret at all would pass this."""
    line = "A = '" + chr(8232) + "x' + '" + PAIR + "d'"
    out = _refuse(tmp_path, line)
    assert "payload line" in out, out
    assert "no caret" in out, (
        "the caret was drawn on a flattened excerpt, or dropped in silence: "
        + out)
    assert chr(8232) not in out, "the separator reached the report: " + out


def test_every_offending_field_is_still_named(tmp_path: Path) -> None:
    """Pins behaviour that already existed and #1814 believed absent: ACROSS
    fields the refusal already reported all of them. Checked rather than
    assumed -- and pinned, because the fix for the within-field short-circuit
    rewrites the same render site."""
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    body = (
        "[[ops]]" + NL + 'op = "paste"' + NL
        + "path = " + _toml_path(a) + NL
        + "content = " + Q3 + "x = '" + PAIR + "d'" + Q3 + NL
        + "[[ops]]" + NL + 'op = "paste"' + NL
        + "path = " + _toml_path(b) + NL
        + "content = " + Q3 + "y = '" + PAIR + "n'" + Q3 + NL
    )
    out = supertool.dispatch("batch:" + _payload(tmp_path, body))
    assert "ops[0].content" in out, out
    assert "ops[1].content" in out, out
    assert not a.exists() and not b.exists(), out


def test_the_message_is_plain_ascii_safe(tmp_path: Path) -> None:
    """cp1252 is the Windows console default and cannot encode an ellipsis or a
    box-drawing glyph; a print that raises UnicodeEncodeError kills the process
    at the report rather than at the work. The elision marker added here is
    ASCII by construction, so this asserts the whole occurrence block rather
    than one character. `mark()` passes an unmapped glyph through unchanged, so
    plain mode is not a guarantee on its own."""
    out = _refuse(tmp_path, THREE_LINES)
    block = [ln for ln in out.splitlines() if "payload line" in ln or "^^" in ln]
    assert block, out
    for ln in block:
        ln.encode("cp1252")


# --- must not fire ---------------------------------------------------------


def test_a_clean_payload_still_writes(tmp_path: Path) -> None:
    """The positive control for every negative in this file. If the fixture
    could not write at all, each `no refusal` assertion below would pass for
    the wrong reason."""
    target = tmp_path / "created.txt"
    out = supertool.dispatch(
        "paste:" + _payload(tmp_path, _paste_body(target, "A = 1")))
    assert "ERROR" not in out, out
    assert target.read_text(encoding="utf-8").strip() == "A = 1"


def test_the_optin_still_suppresses_the_whole_refusal(tmp_path: Path) -> None:
    """#1096's way out is not narrowed by locating the occurrences better."""
    target = tmp_path / "created.txt"
    body = _paste_body(target, THREE_LINES, head="literal_backslashes = true" + NL)
    out = supertool.dispatch("paste:" + _payload(tmp_path, body))
    assert "ERROR" not in out, out
    assert PAIR + "d+" in target.read_text(encoding="utf-8")


def test_a_basic_block_is_still_never_located(tmp_path: Path) -> None:
    """In a basic block a doubled backslash IS one backslash and is the only
    correct spelling. A locator that fired here would locate the remedy."""
    target = tmp_path / "created.txt"
    body = (
        "path = " + _toml_path(target) + NL
        + "content = " + D3 + "A = '" + BS * 4 + "d'" + D3 + NL
    )
    out = supertool.dispatch("paste:" + _payload(tmp_path, body))
    assert "ERROR" not in out, out
    assert "payload line" not in out, out


def test_an_odd_delimiter_run_is_already_explained(tmp_path: Path) -> None:
    """#1819's third ask, pinned rather than rebuilt. A literal block whose
    content quotes the delimiter cannot be pasted, and the reporter read the
    downstream TOML syntax error as unrecoverable. `_toml_delimiter_hint`
    (#394) already names the cause and both escapes; this asserts it, so the
    ask is answered by evidence rather than by a second implementation."""
    target = tmp_path / "created.md"
    body = (
        "path = " + _toml_path(target) + NL
        + "content = " + Q3 + NL + "quoting the delimiter: " + Q3 + NL
        + "done" + NL + Q3 + NL
    )
    out = supertool.dispatch("paste:" + _payload(tmp_path, body))
    assert "ERROR" in out, out
    assert "odd number of " + Q3 + " runs" in out, out
    assert not target.exists(), out
