"""Tests for the html-check validator adapter (#833)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_verdict import assert_ok, assert_declined
from _winenv import empty_path_env

ADAPTER = Path(__file__).parent.parent / "validators" / "html-check" / "html-check.py"


def _run(file_path: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(ADAPTER), file_path],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    assert result.stdout.strip(), f"no stdout; stderr={result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# The gap #833 exists to close: broken inline JS in <script> must be caught
# ---------------------------------------------------------------------------

def test_broken_inline_script_is_a_finding(tmp_path: Path) -> None:
    f = tmp_path / "dashboard.html"
    f.write_text(
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head><title>x</title></head>\n"
        "<body>\n"
        "<script>\n"
        "const b = (;\n"
        "</script>\n"
        "</body>\n"
        "</html>\n"
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] >= 1
    err = out["errors"][0]
    assert err["line"] == 6, f"expected the error pinned to source line 6, got {err}"


def test_valid_inline_script_is_ok(tmp_path: Path) -> None:
    f = tmp_path / "dashboard.html"
    f.write_text(
        "<!DOCTYPE html>\n"
        "<html><body>\n"
        "<script>\n"
        "const a = 1;\n"
        "console.log(a);\n"
        "</script>\n"
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0


def test_html_with_no_script_is_ok(tmp_path: Path) -> None:
    f = tmp_path / "plain.html"
    f.write_text("<html><body><p>hello</p></body></html>\n")
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0


# ---------------------------------------------------------------------------
# Scope: external and non-JS <script> blocks must be left alone
# ---------------------------------------------------------------------------

def test_external_script_src_is_not_checked(tmp_path: Path) -> None:
    """A src= script has no inline body — broken syntax elsewhere must not leak in."""
    f = tmp_path / "external.html"
    f.write_text(
        "<html><body>\n"
        '<script src="/app.js">this is not js and must be ignored anyway(;</script>\n'
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0


def test_json_type_script_is_not_checked(tmp_path: Path) -> None:
    """type=application/json payloads are data, not JS — must not be run through node."""
    f = tmp_path / "data.html"
    f.write_text(
        "<html><body>\n"
        '<script type="application/json">{not: valid, json,,,}</script>\n'
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0


def test_data_src_attribute_is_not_mistaken_for_src(tmp_path: Path) -> None:
    """data-src (consent-management scripts like OneTrust) is not `src` —

    a `\b`-based attribute match fires on the hyphen and would misread a
    still-inline, still-real script as external and skip it.
    """
    f = tmp_path / "consent.html"
    f.write_text(
        "<html><body>\n"
        '<script data-src="/deferred.js">const b = (;</script>\n'
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1


def test_data_type_attribute_is_not_mistaken_for_type(tmp_path: Path) -> None:
    f = tmp_path / "consent.html"
    f.write_text(
        "<html><body>\n"
        '<script data-type="application/json">const b = (;</script>\n'
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1


def test_two_script_blocks_are_paired_and_checked_independently(tmp_path: Path) -> None:
    """Non-greedy `.*?` scanning must not let the first tag's search swallow
    the second, real inline block as part of the first one's body."""
    f = tmp_path / "two.html"
    f.write_text(
        "<html><body>\n"
        "<script>const a = 1;</script>\n"
        "<script>\n"
        "const b = (;\n"
        "</script>\n"
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1
    assert out["errors"][0]["line"] == 4


# ---------------------------------------------------------------------------
# The end tag a browser accepts and this adapter must not walk past
# ---------------------------------------------------------------------------

def test_end_tag_carrying_attributes_still_closes_the_block(tmp_path: Path) -> None:
    """`</script bar>` closes a script for a browser, so it must here too.

    An HTML end tag may carry whitespace and junk attributes before its `>`;
    parsers ignore the junk and close the element. A pattern that only allows
    whitespace finds no closing tag at all, `.*?` finds nothing to pair with,
    the block is never extracted, and the file is reported `ok` with the broken
    JS still in it -- the silent gap #833 exists to close, reappearing through
    the one regex that decides what gets looked at.

    Reported by CodeQL (py/bad-tag-filter, high) against the end-tag half of
    the old single SCRIPT_TAG pattern -- SCRIPT_CLOSE here -- and it is
    the same defect the adapter's `skipped` state was written for: an absence
    produced by the tool, read as an absence in the world.
    """
    f = tmp_path / "attrs.html"
    f.write_text(
        "<html><body>\n"
        "<script>\n"
        "const b = (;\n"
        "</script bar>\n"
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1
    assert out["errors"][0]["line"] == 3


def test_end_tag_junk_does_not_swallow_the_next_block(tmp_path: Path) -> None:
    """The failure's other half: pairing across a missed close.

    With the first block's end tag unrecognised, the non-greedy scan runs on to
    the *next* `</script>` and hands node everything in between -- markup and
    all -- so a page whose JS is fine gets a syntax error pinned to a line that
    holds HTML. Under-detection and false-positive out of one bug.
    """
    f = tmp_path / "pair.html"
    f.write_text(
        "<html><body>\n"
        "<script>\n"
        "const a = 1;\n"
        "</script bar>\n"
        "<p>not javascript</p>\n"
        "<script>\n"
        "const c = 2;\n"
        "</script>\n"
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0


def test_close_inside_a_js_string_is_a_close_because_the_tokenizer_says_so(tmp_path: Path) -> None:
    """`</script ...>` inside a JS string ends the script. It really does.

    This looks like a false positive and is not one. HTML's script-data state
    ends at `</script` followed by whitespace, a slash, or `>`; it has no
    concept of JS string literals, which is why the only way to carry that
    text in a script is to escape the slash. Measured against the stdlib
    tokenizer -- where it is spec-conformant, see #1236 -- the page below emits the data
    `const s = "` and then an end tag -- so the script a browser actually runs
    is a truncated, unterminated string, and reporting a syntax error is the
    correct answer rather than an over-match. Pinned because it is the first
    objection a reader will raise against the `[^>]*` in SCRIPT_CLOSE.
    """
    f = tmp_path / "strclose.html"
    f.write_text(
        "<html><body>\n"
        "<script>\n"
        'const s = "</script data-x>";\n'
        "const z = 1;\n"
        "</script>\n"
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1


def test_scriptfoo_inside_a_js_string_is_not_a_close(tmp_path: Path) -> None:
    """The other side of the word boundary: `</scriptfoo>` is a different tag.

    No whitespace, slash or `>` follows `</script`, so the tokenizer stays in
    script data and so must this pattern. Dropping the boundary anchor would
    cut the block here, hand node an unterminated string, and invent an error
    in a page that is fine.
    """
    f = tmp_path / "scriptfoo.html"
    f.write_text(
        "<html><body>\n"
        "<script>\n"
        'const s = "</scriptfoo>";\n'
        "const z = 1;\n"
        "</script>\n"
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0


# ---------------------------------------------------------------------------
# A strict HTML parser would reject a page a browser renders fine — this
# adapter must not (judgment call: script-extraction, not well-formedness).
# ---------------------------------------------------------------------------

def test_unclosed_void_tags_do_not_fail_the_check(tmp_path: Path) -> None:
    f = tmp_path / "loose.html"
    f.write_text(
        "<html><body>\n"
        '<img src="x.png">\n'
        "<br>\n"
        "<script>const a = 1;</script>\n"
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_ok(out)


# ---------------------------------------------------------------------------
# Tool absent — third state, never a silent ok (per SCHEMA.md)
# ---------------------------------------------------------------------------

def test_missing_node_is_skipped_not_ok(tmp_path: Path) -> None:
    """node absent must reach `skipped`, never a silent `ok`.

    The env comes from `_winenv.empty_path_env()` and not from a hand-built
    `{"PATH": ...}`. On Windows a bare dict does not mean "this environment
    with PATH replaced", it means "an environment holding nothing but PATH":
    SYSTEMROOT and WINDIR go with it, the child interpreter cannot resolve the
    system DLLs it needs to start, and it writes no stdout at all. The
    assertion below then dies in `json.loads("")` with a JSONDecodeError that
    names neither node nor this adapter -- which is exactly what the
    windows-latest 3.9 and 3.10 legs reported, while every POSIX leg passed,
    because on POSIX none of those names exist and the two envs are identical.

    The helper carries the same story and nine sibling adapter tests already
    use it; this one hand-rolled the dict and paid for it a third time.
    """
    f = tmp_path / "dashboard.html"
    f.write_text("<html><body><script>const a = 1;</script></body></html>\n")
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        env=empty_path_env(),
    )
    assert result.stdout.strip(), (
        "the adapter wrote nothing at all -- that is the spawn failing, not a "
        f"verdict. rc={result.returncode} stderr={result.stderr!r}"
    )
    out = json.loads(result.stdout)
    assert "skipped" in out, f"expected the third state, got {out}"
    assert "ok" not in out
    assert "count" not in out
    assert "errors" not in out
    assert "node" in out["skipped"].lower()


# ---------------------------------------------------------------------------
# #1153 -- the same class on the *opening* tag: `>` inside an attribute value
# ---------------------------------------------------------------------------

def test_gt_in_a_quoted_attribute_does_not_hide_the_real_type(tmp_path: Path) -> None:
    """`<script data-tpl="a > b" type="application/json">` is JSON, not JS.

    The opening pattern stops at the first `>` in the source, which for a
    quoted attribute value is not the end of the tag. Every attribute after
    that point becomes invisible: the `type` naming this block as data is
    never seen, the block is treated as JavaScript, and node reports a syntax
    error in a JSON payload this validator has no business reading.
    """
    f = tmp_path / "json.html"
    f.write_text(
        '<html><body>\n'
        '<script data-tpl="a > b" type="application/json">{"k": 1}</script>\n'
        '</body></html>\n'
    )
    assert_ok(_run(str(f)))


def test_gt_in_a_quoted_attribute_does_not_hide_src(tmp_path: Path) -> None:
    """Same truncation, other attribute: an external script has no inline JS.

    The block carries no body a browser would run, and the adapter hands node
    the tail of the start tag -- `b" src="x.js">` -- which is a syntax error
    about markup, pinned to a line holding markup.
    """
    f = tmp_path / "ext.html"
    f.write_text(
        '<html><body>\n'
        '<script data-tpl="a > b" src="x.js"></script>\n'
        '</body></html>\n'
    )
    assert_ok(_run(str(f)))


def test_src_text_inside_an_attribute_value_does_not_skip_the_block(tmp_path: Path) -> None:
    """The silent half, and the reason this is not a cosmetic bug.

    Truncated attributes are not merely short -- they end *inside* a quoted
    value, so the value's own text is read as attribute syntax. A value
    containing ` src=` makes the raw-text `src=` pattern fire, the block is
    dropped as external, and a file with broken inline JS is reported `ok`. An
    absence produced by the tool, read as an absence in the world.
    """
    f = tmp_path / "fakesrc.html"
    f.write_text(
        '<html><body>\n'
        '<script data-tpl="foo src=1 > b">\n'
        'const x = {;\n'
        '</script>\n'
        '</body></html>\n'
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1


def test_type_text_inside_an_attribute_value_does_not_skip_the_block(tmp_path: Path) -> None:
    """Same silent skip through the raw-text `type=` pattern, not `src=`.

    That pattern anchors on whitespace, so the value's text has to carry a space
    before `type=` to be mistaken for the attribute -- which is why this reads
    `x type=json` and not `type=json`.
    """
    f = tmp_path / "faketype.html"
    f.write_text(
        '<html><body>\n'
        "<script data-tpl='x type=json > b'>\n"
        'const x = {;\n'
        '</script>\n'
        '</body></html>\n'
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1
    assert out["errors"][0]["line"] == 3


def test_src_text_inside_a_value_skips_the_block_with_no_gt_involved(tmp_path: Path) -> None:
    """The same silent skip, reachable without any `>` -- so it is its own bug.

    The raw-text `src=` and `type=` patterns scan the attribute *text*, not
    parsed attributes, so a quoted value whose contents read like ` src=`
    or ` type=` is indistinguishable from the real attribute. Delimiting the
    tag correctly does not help: this file's tag ends exactly where it should
    and the block is still dropped, `ok`, with broken JS in it.

    It is why the `>` case is silent rather than merely mis-parsed, and it is
    what the whitespace anchoring in those two patterns cannot reach -- an
    anchor tells `data-src=` from `src=`, not an attribute from a string.
    """
    f = tmp_path / "novaluegt.html"
    f.write_text(
        '<html><body>\n'
        '<script data-tpl="foo src=1 bar">\n'
        'const x = {;\n'
        '</script>\n'
        '</body></html>\n'
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1
    assert out["errors"][0]["line"] == 3


def test_gt_in_a_quoted_attribute_does_not_shift_the_body(tmp_path: Path) -> None:
    """The body starts after the tag, so the finding lands on the JS line.

    With the tag cut short the body begins mid-attribute, node's first
    complaint is about `b">` on the tag's own line, and the reported line
    points at markup instead of at the broken statement.
    """
    f = tmp_path / "shift.html"
    f.write_text(
        '<html><body>\n'
        '<script data-tpl="a > b">\n'
        'const x = {;\n'
        '</script>\n'
        '</body></html>\n'
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1
    assert out["errors"][0]["line"] == 3, f"expected line 3, got {out['errors'][0]}"


def test_quote_inside_an_unquoted_value_still_ends_the_tag(tmp_path: Path) -> None:
    """A quote only opens a value directly after `=`; elsewhere it is a char.

    `<script data-x=a"b>` is a parse error a browser accepts: the value is
    `a"b` and the tag ends at the `>`. Reading that quote as the start of a
    quoted value would run to the next quote in the file and lose the block --
    trading the reported bug for a quieter one.
    """
    f = tmp_path / "unquoted.html"
    f.write_text(
        '<html><body>\n'
        '<script data-x=a"b>\n'
        'const x = {;\n'
        '</script>\n'
        '</body></html>\n'
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1
    assert out["errors"][0]["line"] == 3


def test_unterminated_attribute_value_is_skipped_not_ok(tmp_path: Path) -> None:
    """Where the tag genuinely cannot be delimited, say so -- do not guess.

    A quoted value with no closing quote is EOF-in-tag: the tokenizer drops
    the tag entirely, so nothing here tells us where the script body starts,
    or whether there is one. The regex answered anyway, cutting the tag at a
    `>` that is inside the value.

    This is the third state, not a finding: no claim is being made about the
    file's JavaScript, and `ok` would be a claim.
    """
    f = tmp_path / "unterminated.html"
    f.write_text(
        '<html><body>\n'
        '<script data-x="oops>\n'
        'const x = {;\n'
        '</script>\n'
        '</body></html>\n'
    )
    out = _run(str(f))
    assert "skipped" in out, f"expected the third state, got {out}"
    assert "ok" not in out
    assert "count" not in out
    assert "errors" not in out
    assert "2" in out["skipped"], f"the reason must name the tag's line: {out['skipped']}"


def test_truncated_tag_with_no_quote_says_so_and_does_not_invent_a_quote(tmp_path: Path) -> None:
    """The refusal names the cause it actually found.

    A start tag can fail to close two ways -- a quoted value with no closing
    quote, and a file that simply stops mid-tag. Both reach the same refusal,
    and a message that reports the first when it met the second sends the
    reader looking for a quote that is not there. An error naming the wrong
    thing is the shape of defect this file exists to remove, one layer out.
    """
    f = tmp_path / "truncated.html"
    f.write_text('<html><body>\n<script foo')
    out = _run(str(f))
    assert "skipped" in out, f"expected the third state, got {out}"
    reason = out["skipped"]
    assert "quote" not in reason.lower(), f"there is no quote in this file: {reason}"
    assert "2" in reason, reason


def test_one_undelimited_tag_refuses_the_whole_file(tmp_path: Path) -> None:
    """The refusal is per file, and it outranks a finding it already has.

    This file holds a real syntax error in a block whose tag is perfectly
    well-formed, and *then* a tag that cannot be delimited. Reporting the
    finding alone would be a verdict about a file two thirds of which was
    never read -- `ok: false, count: 1` says "here is what is wrong with this
    file", and the honest answer is "I could not read this file".

    The line-3 finding IS lost by refusing, and that is the price of the rule
    rather than a free move (#1195) -- for a file whose refusal is permanent it
    is lost on every run, not deferred to the next one. Nothing is claimed
    either, which is what makes the trade the right way round.
    """
    f = tmp_path / "mixed.html"
    f.write_text(
        '<html><body>\n'
        '<script>\n'
        'const a = {;\n'
        '</script>\n'
        '<script data-x="oops>\n'
        '</body></html>\n'
    )
    out = _run(str(f))
    assert "skipped" in out, f"expected the third state, got {out}"
    assert "count" not in out
    assert "5" in out["skipped"], f"the reason must name the undelimited tag: {out['skipped']}"


# ---------------------------------------------------------------------------
# #1185 -- the third state reached by a new cause: a block whose *body*
# runs to EOF with no close (the verdict is `skipped`, as everywhere else)
# ---------------------------------------------------------------------------

def test_block_that_never_closes_is_skipped_not_ok(tmp_path: Path) -> None:
    """A `<script>` with no `</script>` anywhere after it must not report `ok`.

    `UndelimitedTag` covers a malformed *start* tag only. A well-formed start
    tag whose body runs to EOF took the other branch, where a missing close
    meant "not a block": the body was dropped and the file reported clean.
    The same broken JS inside a closed block is a finding, so the verdict
    turned on whether a closing tag exists rather than on the JavaScript --
    an absence produced by the adapter, read as an absence in the world.
    """
    f = tmp_path / "eof.html"
    f.write_text("<html><script>\nalert(1\n")
    out = _run(str(f))
    assert "skipped" in out, f"expected the third state, got {out}"
    assert "ok" not in out
    assert "count" not in out
    assert "errors" not in out
    assert "line 1" in out["skipped"], f"the reason must name the block's line: {out['skipped']}"


def test_closed_form_of_the_same_file_is_still_a_finding(tmp_path: Path) -> None:
    """The control from #1185: identical JS, closed, stays a finding.

    Pinned next to the refusal because the fix must not buy the third state
    by making the second one quieter.
    """
    f = tmp_path / "closed.html"
    f.write_text("<html><script>\nalert(1\n</script></html>\n")
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1
    assert out["errors"][0]["line"] == 2


def test_one_unclosed_block_refuses_the_whole_file(tmp_path: Path) -> None:
    """Per file, and it outranks a finding already in hand.

    Same rule the undelimited *start* tag follows. After an unclosed
    `<script>` every byte to EOF is script data for the tokenizer, so a
    later well-formed-looking block is not a block at all -- reporting the
    earlier finding alone would be a verdict about a file that stops being
    readable at line 5.
    """
    f = tmp_path / "mixed_eof.html"
    f.write_text(
        "<html><body>\n"
        "<script>\n"
        "const a = {;\n"
        "</script>\n"
        "<script>\n"
        "const b = 1;\n"
    )
    out = _run(str(f))
    assert "skipped" in out, f"expected the third state, got {out}"
    assert "count" not in out
    assert "line 5" in out["skipped"], f"the reason must name the unclosed block: {out['skipped']}"


def test_unclosed_external_script_refuses_too(tmp_path: Path) -> None:
    """`src=` does not rescue it: attributes say nothing about where it ends.

    The attribute filters run on a block whose extent is known. Here it is
    not -- everything after the tag is script data to EOF -- so there is no
    block to classify as external, and deciding by attribute would be
    guessing at the very point the adapter cannot read.
    """
    f = tmp_path / "extonly.html"
    f.write_text('<html><body>\n<script src="x.js">\n')
    out = _run(str(f))
    assert "skipped" in out, f"expected the third state, got {out}"
    assert "line 2" in out["skipped"], out["skipped"]


def test_script_hyphen_custom_element_is_not_a_script_block(tmp_path: Path) -> None:
    """`<script-foo>` is a different tag, and the word boundary matched it.

    A word boundary after the tag name fires on the hyphen, so the custom
    element's children were extracted and handed to node -- a syntax error
    reported against markup in a page whose JavaScript is fine. A tag name
    ends at whitespace, a slash or `>`, and nowhere else.
    """
    f = tmp_path / "custom.html"
    f.write_text(
        '<html><body>\n'
        '<script-foo bar>\n'
        '<p>not javascript</p>\n'
        '</script-foo>\n'
        '</body></html>\n'
    )
    assert_ok(_run(str(f)))


# ---------------------------------------------------------------------------
# No argument / schema shape
# ---------------------------------------------------------------------------

def test_no_arg_returns_error() -> None:
    result = subprocess.run(
        [sys.executable, str(ADAPTER)],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert_declined(out)
    assert out["errors"][0]["code"] == "adapter"


def test_output_contains_required_fields(tmp_path: Path) -> None:
    f = tmp_path / "plain.html"
    f.write_text("<html><body><p>hi</p></body></html>\n")
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out

# ---------------------------------------------------------------------------
# #1182 -- the end tag is walked too, because `>` hides in a quoted value at
# both ends of the block. `[^>]*` stopped at the first `>` in the source.
# ---------------------------------------------------------------------------

def _load_adapter():
    """Import html-check.py as a module. The hyphen rules out a plain import."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_html_check_adapter", ADAPTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_data_per_block(markup: str) -> list:
    """What *this machine's* `html.parser` says each script block's data is.

    Deliberately no longer the assertion's reference. `html.parser` is a second
    implementation with its own version skew, and reading it live imported that
    skew into our CI matrix -- see `SPEC_END_TAG_FORMS` below for the whole
    story. It is kept as a corroborating witness, consulted only where the
    stdlib is new enough to be spec-conformant.
    """
    from html.parser import HTMLParser

    class _Reader(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.bodies = []
            self._open = False

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self._open = True

        def handle_endtag(self, tag):
            if tag == "script":
                self._open = False

        def handle_data(self, data):
            if self._open:
                self.bodies.append(data)

    reader = _Reader()
    reader.feed(markup)
    reader.close()
    return reader.bodies


# The expected answers are FROZEN here, against the WHATWG spec, and are not
# read out of `html.parser` at run time. That is a correction (#1236), and the
# reason is worth the paragraph.
#
# These seven forms were originally asserted against whatever `html.parser` the
# runner happened to ship. Five of them then went red on 8 of 20 CI legs --
# every macOS leg and windows 3.9 -- while ubuntu stayed green, which is the
# reverse of this repo's usual platform story and was nothing to do with the
# platform. CPython rewrote `HTMLParser.parse_endtag` to follow the spec's "End
# tag open state" and backported it into later patch releases of the older
# branches. Measured here, on this machine:
#
#   3.11.11  `</script bar>`  -> no end tag, and the script body is DROPPED
#   3.13.2   `</script bar>`  -> the same
#   3.14.6   `</script bar>`  -> end tag, body `const z = 1;`
#
# The split is by stdlib patch level, not by OS, and the two 3.9 legs prove it
# outright: ubuntu 3.9 is 3.9.25 and passed, windows 3.9 is 3.9.13 (read off
# the pythonLocation in job #93294247016) and failed. Same minor version, two
# patch levels, opposite results. A live oracle therefore makes the assertion a
# function of the runner image, and it would keep breaking on image updates
# with no change to our code.
#
# **Which one is right matters more than the red, and it is not the old
# stdlib.** WHATWG 13.2.5.17 "Script data end tag name state": on whitespace,
# switch to before-attribute-name state; on `/`, to self-closing start tag
# state; on `>`, emit the end tag. All three close the element, and
# before-attribute-name state is where quoted values live -- which is the whole
# of #1182. The old `html.parser` closed on none of them and silently threw the
# script body away, which is not a defensible reading of the spec and is a bug
# CPython has since fixed. Our walker was right; the oracle was wrong.
#
# So: freeze, do not relax. Restricting the corpus to forms every stdlib agrees
# on would leave `bare` and `trailing-space`, exactly the two the pre-#1182
# regex already handled. Skipping the five on old interpreters would delete the
# coverage on 8 of 20 legs. Frozen expectations run everywhere; the live parser
# is kept below as a cross-check where it is conformant, so these numbers
# cannot drift from a fixed stdlib either.
#
# **What this corpus does and does not prove, measured rather than assumed.**
# Re-running these seven against the old `[^>]*>` close: six still pass and only
# `script-tag-inside-a-value` fails (it raises `UnclosedBlock`). So the corpus
# is a *breadth* check on where the boundary lands, and exactly one of its rows
# is load-bearing against a revert of #1182. The rest of #1182's pin is the
# three verdict tests below -- `gt_inside_a_quoted_end_tag_value...`,
# `unterminated_end_tag_value...` and `truncated_end_tag...` -- which all three
# went red before the fix and assert refusals rather than bodies. Do not read
# seven green rows as seven guarantees.
#
# Ids are given rather than derived: pytest builds one from the markup itself,
# which puts `<`, `>` and `"` into every junit.xml test name CI reads back.
SPEC_END_TAG_FORMS = [
    ("bare", "<script>const z = 1;</script>TAIL",
     ["const z = 1;"], "13.2.5.17 on `>` -- emit the end tag"),
    ("trailing-space", "<script>const z = 1;</script >TAIL",
     ["const z = 1;"], "13.2.5.17 on whitespace -- before attribute name state"),
    ("solidus", "<script>const z = 1;</script/>TAIL",
     ["const z = 1;"], "13.2.5.17 on `/` -- self-closing start tag state"),
    ("junk-attribute", "<script>const z = 1;</script bar>TAIL",
     ["const z = 1;"], "attributes on an end tag are a parse error, not a non-close"),
    ("gt-is-the-whole-value", '<script>const z = 1;</script foo=">">TAIL',
     ["const z = 1;"], "13.2.5.36 attribute value (double-quoted) -- `>` is data"),
    ("gt-inside-a-value", '<script>const z = 1;</script data-t="a > b">TAIL',
     ["const z = 1;"], "13.2.5.36 -- the tag ends at the second `>`, not the first"),
    ("script-tag-inside-a-value",
     '<script>const z = 1;</script data-t="x > <script>const q = 2;</script">TAIL',
     ["const z = 1;"],
     "13.2.5.36 -- the contents of a value are never markup"),
]


@pytest.mark.parametrize(
    "markup,expected",
    [(m, e) for _id, m, e, _why in SPEC_END_TAG_FORMS],
    ids=[i for i, _m, _e, _why in SPEC_END_TAG_FORMS])
def test_end_tag_boundary_matches_the_spec(markup: str, expected: list) -> None:
    """Where the spec closes the block is where this adapter closes it.

    Not "a better regex until the reported case passes": #1153 replaced the
    start tag with a walker because the tag grammar is a small state machine
    and a pattern ending at the first `>` is wrong in both directions. The end
    tag reaches the same state, so it gets the same walker and is held to the
    same standard -- the spec, now stated rather than sampled from the stdlib.
    """
    adapter = _load_adapter()
    extracted = [body for _line, body in adapter.extract_js_blocks(markup)]
    assert extracted == expected, markup


def test_the_frozen_corpus_still_covers_the_forms_1182_was_about() -> None:
    """A corpus quietly trimmed to the easy cases is the failure mode here.

    The tempting repair for #1236 was to drop the five forms old stdlibs
    disagree on. Measured against the old `[^>]*>` close, six of the seven rows
    pass anyway and `script-tag-inside-a-value` is the one that does not -- so
    that row specifically must survive any future trim, or this file goes green
    on a revert of #1182. The other four are named too because they are the
    forms the old stdlib disagrees about, and re-deriving that costs a CI matrix.
    """
    ids = {i for i, _m, _e, _why in SPEC_END_TAG_FORMS}
    assert "script-tag-inside-a-value" in ids, (
        "the one row that fails against the pre-#1182 close is gone; what is "
        "left cannot tell the fix from the bug. Present: %s" % sorted(ids))
    assert {"solidus", "junk-attribute", "gt-is-the-whole-value",
            "gt-inside-a-value", "script-tag-inside-a-value"} <= ids, sorted(ids)
    for _id, _markup, expected, why in SPEC_END_TAG_FORMS:
        assert expected == ["const z = 1;"], (_id, expected)
        assert why, _id


HTML_PARSER_TOKEN = "html-parser-endtag-conformance(#1236)"


def _html_parser_is_spec_conformant():
    """(available, reason) -- does this stdlib close `</script bar>`?

    Probed by behaviour, not by `sys.version_info`. The fix was backported into
    patch releases, so a version comparison would need a table of per-branch
    patch numbers that is wrong the moment a branch cuts another release. The
    property is one feed away; ask for it.

    Reason is empty when available and never empty when not, so a `False` here
    can always say which absence it is.
    """
    probe = "<script>x</script bar>"
    closed = _script_data_per_block(probe) == ["x"]
    if closed:
        return True, ""
    return False, (
        "this stdlib's html.parser does not close `</script bar>` (it predates "
        "the CPython parse_endtag rewrite and drops the script body instead), "
        "so it cannot corroborate the frozen expectations -- it is the thing "
        "they were frozen against. Python %s" % sys.version.split()[0])


def test_a_conformant_html_parser_confirms_the_frozen_expectations() -> None:
    """The frozen numbers are checked against a real tokenizer where there is one.

    Freezing expectations trades a moving reference for a stale one, and this
    is what pays that back: on any interpreter whose `html.parser` implements
    the spec's end-tag states, every frozen answer above must equal what it
    produces. A wrong constant cannot sit here indefinitely.

    Where the stdlib is older this declines and says so, rather than passing.
    The frozen assertions above still run on that leg; what is unavailable is
    the second opinion, not the coverage.
    """
    available, why = _html_parser_is_spec_conformant()
    if not available:
        pytest.skip(HTML_PARSER_TOKEN + ": " + why)
    disagreements = [
        (form_id, expected, _script_data_per_block(markup))
        for form_id, markup, expected, _why in SPEC_END_TAG_FORMS
        if _script_data_per_block(markup) != expected
    ]
    assert not disagreements, (
        "a spec-conformant html.parser disagrees with the frozen expectations "
        "-- (form, frozen, stdlib): %s" % disagreements)


def test_gt_inside_a_quoted_end_tag_value_does_not_invent_a_second_block(
    tmp_path: Path,
) -> None:
    """The false-positive direction, and it is not verdict-neutral.

    `[^>]*` ends the close at the `>` inside `data-t`'s value, so the rest of
    that quoted value is then read as markup. When the value contains the text
    `<script>`, a block that exists only inside a string is extracted; the
    trailing `</script"` is not a close (no whitespace, slash or `>` follows
    `</script`), so nothing closes it and the adapter refuses the whole file --
    `skipped`, "NO inline <script> block in this file was checked" -- about a
    page html.parser reads as one clean script followed by text.
    """
    f = tmp_path / "endgt.html"
    f.write_text(
        "<html><body>\n"
        "<script>\n"
        "const a = 1;\n"
        '</script data-t="x > <script>const q = ;</script">\n'
        "</body></html>\n"
    )
    out = _run(str(f))
    assert "skipped" not in out, (
        "the adapter refused a file whose one script block is valid, because "
        "the `>` inside the end tag's quoted value cut the close short and "
        "the value's own text was then read as markup: %s" % out)
    assert_ok(out)
    assert out["count"] == 0


def test_unterminated_end_tag_value_is_skipped_not_ok(tmp_path: Path) -> None:
    """The false-`ok` direction -- the one the contract calls unacceptable.

    `</script data-t="oops>` is EOF-in-tag: html.parser emits no end tag at
    all, so this element never closes and #1185's rule applies. `[^>]*>`
    instead ran through to the `>` *inside* the unterminated value, invented a
    close there, and reported `ok: true` about a file the tokenizer cannot
    finish reading.

    A quoted value with no closing quote is already `skipped` on the *start*
    tag (`test_unterminated_attribute_value_is_skipped_not_ok`). One rule
    applied to half the cases is exactly what #1182 is.
    """
    f = tmp_path / "endunterminated.html"
    f.write_text(
        "<html><body>\n"
        "<script>\n"
        "const a = 1;\n"
        '</script data-t="oops>\n'
        "</body></html>\n"
    )
    out = _run(str(f))
    assert "skipped" in out, f"expected the third state, got {out}"
    assert "ok" not in out
    assert "count" not in out
    assert "errors" not in out
    reason = out["skipped"]
    assert "4" in reason, f"the reason must name the end tag's line: {reason}"
    assert "end tag" in reason, (
        "the reason must say it is the END tag that could not be delimited -- "
        "a reader sent to the start tag on line 2 finds nothing wrong there: "
        "%s" % reason)


def test_truncated_end_tag_is_not_reported_as_a_missing_end_tag(tmp_path: Path) -> None:
    """A truncated close and an absent close send the reader to different places.

    With `[^>]*>` a file that stops inside its end tag matched nothing at all,
    fell through to `UnclosedBlock`, and the refusal said the element "has no
    closing tag anywhere after it", naming the line of the *opening* tag.
    There is a closing tag; it is on line 4 and it is truncated. The next
    action is "finish this tag", not "add one" -- the same distinction that
    made `UndelimitedTag` and `UnclosedBlock` two types rather than one.
    """
    f = tmp_path / "endtruncated.html"
    f.write_text(
        "<html><body>\n"
        "<script>\n"
        "const a = 1;\n"
        "</script data-t"
    )
    out = _run(str(f))
    assert "skipped" in out, f"expected the third state, got {out}"
    reason = out["skipped"]
    assert "4" in reason, f"the reason must name the truncated end tag: {reason}"
    assert "no closing tag anywhere" not in reason, (
        "there IS a closing tag on line 4 and it is truncated; sending the "
        "reader to add one is the wrong next action: %s" % reason)
    assert "quote" not in reason.lower(), (
        "no quote is involved in this file: %s" % reason)
