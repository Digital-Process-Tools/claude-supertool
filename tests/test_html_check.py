"""Tests for the html-check validator adapter (#833)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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

    Reported by CodeQL (py/bad-tag-filter, high) against SCRIPT_TAG, and it is
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
    tokenizer, which is spec-conformant here, the page below emits the data
    `const s = "` and then an end tag -- so the script a browser actually runs
    is a truncated, unterminated string, and reporting a syntax error is the
    correct answer rather than an over-match. Pinned because it is the first
    objection a reader will raise against the `[^>]*` in SCRIPT_TAG.
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
