r"""The shell single-quote escape idiom in a write-bound field escalates from a
warning to a refusal, matching #834/#1087's even-backslash-run shape (#2243).

#2114 made this a WARNING and gave a real reason at the time: there was no way
to say "I meant the idiom itself", so refusing would strand the correct write
(this repository's own `presets/_refname.py` comment on `shlex.quote`) with no
escape hatch. That reason held only while `literal_backslashes` (#1096) did not
also cover this idiom. It is the same declaration either way -- "this field's
odd-looking punctuation is intentional, do not second-guess it" -- so reusing
the one key rather than adding a second is the fix, not a new flag.

A triage sweep proposed this issue and #2169 as one cluster, on cross-reference
evidence rather than a reading of the guard's own source. They are NOT one
mechanism: #2169's subject (`_payload_double_backslash_refusal`) was already a
refusal before this issue was filed, and had already grown position-reporting
and the `literal_backslashes` remedy text under #1808/#1814/#1819/#1839 -- there
was nothing left to fix there. This issue's subject
(`_payload_shell_quote_escape_note`) was a soft warning that let the write
proceed either way, which is the actual defect: the two rhyme in shape (a
payload-content heuristic distinguishing genuine punctuation from an escape
reflex) but live in two separate functions with two separate call sites, and
only this one needed a behavioural change.

Absorbs `tests/test_payload_shell_quote_escape_2114.py` (retired, not merely
deleted -- its 7 cases either restated what this file already covers under
the new refusal shape, or are folded in below): the two `NOT flagged`
negative controls, the basic-string-block exemption, and the wide-batch cap
all still apply, only against `ERROR` + nothing-on-disk rather than a warning
+ an unchanged write. #2114's own reasoning for why this started as a warning
-- no escape hatch existed yet -- is preserved above and in `_supertool.py`'s
own `_payload_shell_quote_escape_refusal` docstring, so the "why a warning
first" history is not lost, only superseded.
"""
from pathlib import Path

import supertool

NL = chr(10)
Q3 = chr(39) * 3
D3 = chr(34) * 3
BS = chr(92)

DQ_FORM = "'" + chr(34) + "'" + chr(34) + "'"      # '"'"'
BS_FORM = "'" + BS + "''"                          # '\''


def _toml_path(target: Path) -> str:
    r"""A payload `path =` as a basic string, with separators escaped (Windows
    backslash-in-basic-string trap -- see test_payload_shell_quote_escape_2114.py's
    own `_toml_path` for the CI leg this guards)."""
    return chr(34) + str(target).replace(BS, BS * 2) + chr(34)


def _write_payload(tmp_path: Path, body: str, name: str = "p.toml") -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def _paste_payload(tmp_path: Path, content: str, extra: str = "",
                    out_name: str = "out.txt") -> str:
    target = tmp_path / out_name
    body = (
        "path = " + _toml_path(target) + NL
        + "content = " + content + NL
        + extra
    )
    return "paste:" + _write_payload(tmp_path, body)


def test_double_quote_form_is_refused(tmp_path: Path) -> None:
    content = Q3 + "Alice" + DQ_FORM + "s basket" + Q3
    op = _paste_payload(tmp_path, content)
    out = supertool.dispatch(op)
    assert "ERROR" in out
    assert "apostrophe" in out.lower() or "single-quote" in out.lower(), out


def test_backslash_form_is_refused(tmp_path: Path) -> None:
    content = Q3 + "don" + BS_FORM + "t stop" + Q3
    op = _paste_payload(tmp_path, content)
    out = supertool.dispatch(op)
    assert "ERROR" in out
    assert "apostrophe" in out.lower() or "single-quote" in out.lower(), out


def test_nothing_reaches_disk_on_refusal(tmp_path: Path) -> None:
    """Unlike #2114's warning, the write must not happen at all -- fires at
    parse time, before any op runs, the same guarantee #1087 gives the
    doubled-backslash case."""
    content = Q3 + "Alice" + DQ_FORM + "s basket" + Q3
    target = tmp_path / "out.txt"
    op = _paste_payload(tmp_path, content)
    out = supertool.dispatch(op)
    assert "ERROR" in out
    assert not target.exists(), "the refusal must stop the write, not merely warn about it: " + out


def test_refusal_names_the_remedy(tmp_path: Path) -> None:
    """The refusal must name `literal_backslashes` and that it can be scoped
    to one field, not just say the write was refused -- the same complaint
    #2169 raised about the sibling even-backslash-run refusal."""
    content = Q3 + "the idiom is " + DQ_FORM + Q3
    op = _paste_payload(tmp_path, content)
    out = supertool.dispatch(op)
    assert "ERROR" in out
    assert "literal_backslashes" in out
    assert "content" in out


def test_refusal_names_the_position(tmp_path: Path) -> None:
    """A count alone ('N occurrences') is not enough to find the field in a
    long payload -- the refusal must say a payload line number."""
    content = Q3 + "line one" + NL + "line two has " + DQ_FORM + " in it" + Q3
    op = _paste_payload(tmp_path, content)
    out = supertool.dispatch(op)
    assert "ERROR" in out
    assert "line" in out.lower()
    assert "3" in out, "the idiom sits on the payload's 3rd line: " + out


def test_exempted_whole_payload_writes_unrefused(tmp_path: Path) -> None:
    """`literal_backslashes = true` is the escape hatch #2114's warning-only
    design said did not exist -- it now covers this idiom too, reusing the
    one key rather than adding a second."""
    content = Q3 + "Alice" + DQ_FORM + "s basket" + Q3
    op = _paste_payload(tmp_path, content, extra="literal_backslashes = true" + NL)
    supertool.dispatch(op)
    target = tmp_path / "out.txt"
    assert target.read_text(encoding="utf-8") == "Alice" + DQ_FORM + "s basket" + NL


def test_exempted_by_field_name_writes_unrefused(tmp_path: Path) -> None:
    content = Q3 + "Alice" + DQ_FORM + "s basket" + Q3
    extra = 'literal_backslashes = ["content"]' + NL
    op = _paste_payload(tmp_path, content, extra=extra)
    supertool.dispatch(op)
    target = tmp_path / "out.txt"
    assert target.read_text(encoding="utf-8") == "Alice" + DQ_FORM + "s basket" + NL


def test_a_lone_quote_char_is_not_refused(tmp_path: Path) -> None:
    """The ordinary, correct case -- a literal block needs no escaping for a
    single embedded quote at all. Must-not-fire paired with every must-fire
    case above."""
    content = Q3 + "Alice" + chr(39) + "s basket" + Q3
    op = _paste_payload(tmp_path, content)
    supertool.dispatch(op)
    target = tmp_path / "out.txt"
    assert target.read_text(encoding="utf-8") == "Alice" + chr(39) + "s basket" + NL


def test_old_field_is_not_refused(tmp_path: Path) -> None:
    """`old` is an anchor -- a doubled idiom there cannot match and the runner
    reports a skip, so refusing it would cost a round-trip on a call that was
    already safe. Same scoping `_PAYLOAD_DBS_WRITE_KEYS` already gives the
    backslash refusal."""
    target = tmp_path / "anchor.txt"
    target.write_text("Alice" + chr(39) + "s basket" + NL, encoding="utf-8")
    body = (
        "path = " + _toml_path(target) + NL
        + "old = " + Q3 + "Alice" + DQ_FORM + "s basket" + Q3 + NL
        + "new = " + Q3 + "Bob" + chr(39) + "s basket" + Q3 + NL
    )
    op = "edit:" + _write_payload(tmp_path, body)
    out = supertool.dispatch(op)
    assert "no match" in out.lower() or "0 replacement" in out.lower() or "skip" in out.lower(), out


def test_a_basic_block_is_not_flagged(tmp_path: Path) -> None:
    """A basic `\"\"\"` block processes its own escapes; the idiom's `'` and `"`
    characters there are ordinary content, not a payload author's escape
    reflex -- the false-positive population this guard must not create.
    Carried over from test_payload_shell_quote_escape_2114.py."""
    content = D3 + "Alice" + DQ_FORM + "s basket" + D3
    op = _paste_payload(tmp_path, content)
    out = supertool.dispatch(op)
    target = tmp_path / "out.txt"
    assert "ERROR" not in out
    assert target.read_text(encoding="utf-8") == "Alice" + DQ_FORM + "s basket" + NL


def test_a_wide_batch_is_capped_not_a_wall(tmp_path: Path) -> None:
    """A batch of many ops all carrying the idiom must not print one located
    block per op -- the "wall nobody finishes" `_PAYLOAD_DBS_MAX_FIELDS`
    already caps for the sibling doubled-backslash refusal. Carried over
    from test_payload_shell_quote_escape_2114.py, against the refusal shape:
    nothing in the batch is written, and the remainder is named, not
    silently dropped."""
    ops = []
    for i in range(6):
        target = tmp_path / ("out" + str(i) + ".txt")
        ops.append(
            "[[ops]]" + NL
            + 'op = "paste"' + NL
            + "path = " + _toml_path(target) + NL
            + "content = " + Q3 + "x" + DQ_FORM + "y" + Q3 + NL
        )
    body = "".join(ops)
    op = "batch:" + _write_payload(tmp_path, body)
    out = supertool.dispatch(op)
    assert "ERROR" in out
    # Count LOCATED-BLOCK lines specifically (each names its field with the
    # arrow marker), not every use of the word "occurrence" -- the refusal's
    # fixed remedy text ("decide per occurrence", "own occurrences refused")
    # also contains the word, which a bare substring count would over-count.
    located = sum(1 for line in out.splitlines() if "content` --" in line)
    assert located <= 3, "at most a few located blocks should render: " + out
    assert "and 3 more" in out, "the remainder is named, not silently dropped: " + out
    for i in range(3):
        assert "ops[" + str(i) + "].content" in out, "the shown fields are named: " + out
    for i in range(6):
        target = tmp_path / ("out" + str(i) + ".txt")
        assert not target.exists(), "nothing in a refused batch should land: " + out
