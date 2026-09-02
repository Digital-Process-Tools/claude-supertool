r"""A payload literal writes the shell single-quote escape idiom verbatim, and
nothing says so (#2114).

Reported by the fix/2096 lane: text meant to carry a plain apostrophe was
typed as the shell idiom that ends a single-quoted string, emits a literal
quote another way, and reopens it -- `'"'"'` (or, with the backslash
spelling, `'\''`). Inside a shell's OWN quoting either resolves to one
apostrophe. Inside a TOML triple-single-quoted literal -- which is what a
payload's `content =` or `new =` field IS -- nothing resolves them, so the
whole sequence lands on disk at its full length, the write reports success,
and every validator agrees, because the sequence is legal text in nearly
every language this repo edits.

Reproduced here for the first time (the reporting lane kept none): a plain
`paste:@` payload whose `content` field carries the idiom mid-word writes
those literal characters, with the receipt saying nothing about it.

This is a WARNING, not a refusal, and deliberately so -- unlike #834's
trailing-backslash case, there is no unambiguous second spelling to offer.
The same bytes are exactly what a payload correctly documenting the idiom
would ALSO write (this repository's own presets/_refname.py comment on
shlex.quote's escaping does), so refusing would
strand a legitimate write with no escape hatch. The two `NOT flagged`
negative controls below are the point: a warning that also fires on
ordinary apostrophes or short quote runs is a warning nobody reads.
"""
from pathlib import Path

import supertool

NL = chr(10)
Q3 = chr(39) * 3
D3 = chr(34) * 3
BS = chr(92)

# The two common spellings of the idiom: close, double-quote a literal quote,
# reopen -- and close, backslash-escape a literal quote, reopen.
DQ_FORM = "'" + chr(34) + "'" + chr(34) + "'"      # '"'"'
BS_FORM = "'" + BS + "''"                          # '\''


def _write_payload(tmp_path: Path, body: str, name: str = "p.toml") -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def _paste(tmp_path: Path, content: str, out_name: str = "out.txt"):
    target = tmp_path / out_name
    body = (
        "path = " + chr(34) + str(target) + chr(34) + NL
        + "content = " + content + NL
    )
    out = supertool.dispatch("paste:" + _write_payload(tmp_path, body))
    return out, target


def test_double_quote_form_is_warned(tmp_path: Path) -> None:
    content = Q3 + "Alice" + DQ_FORM + "s basket" + Q3
    out, target = _paste(tmp_path, content)
    on_disk = target.read_text(encoding="utf-8")
    assert DQ_FORM in on_disk, "the write is never blocked or rewritten: " + on_disk
    assert DQ_FORM in out, "the exact sequence is echoed back: " + out
    assert "content" in out, "the field is named: " + out
    assert "apostrophe" in out.lower() or "single-quote" in out.lower(), out


def test_backslash_form_is_warned(tmp_path: Path) -> None:
    content = Q3 + "don" + BS_FORM + "t stop" + Q3
    out, target = _paste(tmp_path, content)
    assert BS_FORM in target.read_text(encoding="utf-8")
    assert BS_FORM in out
    assert "apostrophe" in out.lower() or "single-quote" in out.lower(), out


def test_the_write_is_never_altered(tmp_path: Path) -> None:
    """The tool never guesses at intent -- collapsing the idiom to a bare
    apostrophe could be exactly wrong for a payload documenting the shell
    trick itself. Bytes on disk are asserted unchanged, whatever the
    receipt says about them."""
    content = Q3 + "Alice" + DQ_FORM + "s basket" + Q3
    _out, target = _paste(tmp_path, content)
    assert target.read_text(encoding="utf-8") == "Alice" + DQ_FORM + "s basket" + NL


def test_a_lone_quote_char_is_not_flagged(tmp_path: Path) -> None:
    """The ordinary, correct case -- a literal block needs no escaping for a
    single embedded quote at all. If this warns, nobody reads the warning."""
    content = Q3 + "Alice" + chr(39) + "s basket" + Q3
    out, _target = _paste(tmp_path, content)
    assert "apostrophe" not in out.lower() and "single-quote" not in out.lower(), out


def test_a_basic_block_is_not_flagged(tmp_path: Path) -> None:
    """A basic `\"\"\"` block processes its own escapes; the idiom's `'` and `"`
    characters there are ordinary content, not a payload author's escape
    reflex, and the false-positive population this guard must not create."""
    content = D3 + "Alice" + DQ_FORM + "s basket" + D3
    out, target = _paste(tmp_path, content)
    assert target.read_text(encoding="utf-8") == "Alice" + DQ_FORM + "s basket" + NL
    assert "apostrophe" not in out.lower() and "single-quote" not in out.lower(), out


def test_writing_the_pattern_on_purpose_still_writes_and_still_warns(tmp_path: Path) -> None:
    """The false-positive population named in the issue is real -- this
    repository's own presets/_refname.py comment on shlex.quote's escaping
    documents the idiom using these exact bytes. The guard cannot tell that
    case from a mistake, which is why it warns rather than refuses: the
    write always proceeds either way."""
    content = Q3 + "the idiom is " + DQ_FORM + Q3
    out, target = _paste(tmp_path, content)
    assert DQ_FORM in target.read_text(encoding="utf-8")
    assert "apostrophe" in out.lower() or "single-quote" in out.lower(), out


def test_a_wide_batch_is_capped_not_a_wall(tmp_path: Path) -> None:
    """A batch of many ops all carrying the idiom must not print one located
    block per op -- that is the "wall nobody finishes" the sibling doubled-
    backslash note (`_PAYLOAD_DBS_MAX_FIELDS`) was already built to avoid, and
    this note mirrors none of that capping without this test (review finding,
    2026-09-02): a 20-op batch printed all 20 findings verbatim."""
    ops = []
    for i in range(6):
        target = tmp_path / ("out" + str(i) + ".txt")
        ops.append(
            "[[ops]]" + NL
            + 'op = "paste"' + NL
            + "path = " + chr(34) + str(target) + chr(34) + NL
            + "content = " + Q3 + "x" + DQ_FORM + "y" + Q3 + NL
        )
    body = "".join(ops)
    out = supertool.dispatch("batch:" + _write_payload(tmp_path, body))
    located = out.count("occurrence")
    assert located <= 3, "at most a few located blocks should render: " + out
    assert "and 3 more" in out, "the remainder is named, not silently dropped: " + out
    for i in range(6):
        assert "ops[" + str(i) + "].content" in out, "every field is still named: " + out
