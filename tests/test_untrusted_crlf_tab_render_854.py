"""#854 — every CRLF-authored body renders `␍` on every line, and tabs render `␉`.

#851 widened `presets/_untrusted.py` from "replace `\\n` and `\\r`" to "disclose
every C0/C1 control character as its Control Pictures glyph". That closed a real
hole — `\\x1b[2K\\x1b[1A` in a check run's `output.title` could erase the real
`failure` verdict off a merge gate — and it caught two characters that are not
cursor commands at all:

  * `\\r` when it is the first half of a **CRLF pair**, which is just how the web
    UIs of both trackers end a line. Measured on the first eight open issues per
    repo: 1210 carriage returns in `nodejs/node`, 1221 in `python/cpython`, 231
    in `microsoft/vscode`.
  * `\\t`, which advances the cursor and cannot reach a column it has already
    passed. `microsoft/vscode` alone had 159 in that sample, all of them
    indentation inside fenced code.

Why that is a security regression and not a cosmetic one. The module's own
design argument is that a convention read as noise is a convention that gets
abandoned — it is why one-line fields are flattened rather than fenced, and why
the banner is one line. `␍` at the end of essentially every line of every
web-authored body spends on line endings the credibility the glyph earns on
`␛`. A reader who has learned to skip `␍` has learned to skip the disclosure.

The rule these tests pin
------------------------
**A carriage return is inert only as the pair `\\r\\n`.** Alone it is a cursor
command — it returns the cursor to column 0, so `real line\\rFORGED` overwrites
what the tool already wrote, which is the #851 class exactly. So the pair is
normalised to a bare newline before disclosure, and any `\\r` that is not
followed by `\\n` is disclosed as `␍` like every other cursor mover.

`test_a_lone_carriage_return_is_still_neutralised` is the one that fails on the
obvious wrong fix — adding `\\r` to `keep`.

**A tab is content in a block and a cursor command in a field**, and the two
functions answer differently on purpose. Inside `scrub()` the text *is* a block:
it spans lines already, the fence markers own its edges, and a tab is
indentation the author wrote. Inside `flat()` the text is one field
interpolated into a line the tool owns — and `_board.render_row` flattens every
cell of a column-aligned board, where a tab is the one C0 character that can
imitate the board's own column structure without making a line. So `scrub()`
keeps tabs and `flat()` discloses them.

No fixture in the suite used CRLF, which is why this shipped. The end-to-end
test below is that fixture.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh_issue = _load("presets/github/issue.py", "github_issue_854")
# The preset's own instance: the nonce is drawn per module object, so a second
# import would compare against markers no render ever printed.
untrusted = gh_issue._untrusted

CR = chr(13)
LF = chr(10)
TAB = chr(9)

CR_GLYPH = chr(0x240D)
TAB_GLYPH = chr(0x2409)

# Every character #851 established as a way to move a cursor or start a line.
# None of them may pass, whatever this change does for `\r\n` and `\t`.
CURSOR_MOVERS = (chr(0x0B), chr(0x0C), chr(0x85), chr(0x1B), chr(0x1C),
                 chr(0x1D), chr(0x1E), chr(0x7F))

# A body as the GitHub web editor writes it: CRLF line endings throughout, a
# tab-indented fenced block, and a blank line that is a bare CRLF.
CRLF_BODY = (
    "Looks like it was missing from PR 58909" + CR + LF
    + CR + LF
    + "```py" + CR + LF
    + TAB + "def f():" + CR + LF
    + TAB + TAB + "return 1" + CR + LF
    + "```" + CR + LF
    + "Refs: 58909" + CR + LF
)


def _raw_controls(text: str, allowed: str = LF) -> list[str]:
    """Every control byte still in the output, minus the ones the render emits.

    Newline is always allowed — a block is lines. Callers pass more when the
    function under test has decided a character is content.
    """
    return sorted({
        c for c in text
        if c not in allowed
        and (ord(c) < 0x20 or ord(c) == 0x7F or 0x80 <= ord(c) <= 0x9F)
    })


# ---------------------------------------------------------------------------
# the regression: a body written in a browser
# ---------------------------------------------------------------------------

def test_a_crlf_authored_body_renders_with_no_carriage_return_glyph() -> None:
    out = untrusted.scrub(CRLF_BODY)
    assert CR_GLYPH not in out, repr(out)
    assert CR not in out


def test_a_crlf_authored_body_keeps_its_lines_and_its_text() -> None:
    """Normalising the pair must not join lines or eat the blank one."""
    out = untrusted.scrub(CRLF_BODY)
    lines = out.split(LF)
    assert lines[0] == "Looks like it was missing from PR 58909"
    assert lines[1] == ""
    assert "Refs: 58909" in lines


def test_a_crlf_authored_body_survives_the_gh_issue_render(monkeypatch, capsys) -> None:
    """The fixture the suite did not have. Sixteen callers reach this boundary."""
    out = _run_gh_issue(monkeypatch, capsys, body=CRLF_BODY,
                        comments=[{"author": {"login": "drive-by"},
                                   "body": CRLF_BODY,
                                   "createdAt": "2026-08-01T00:00:00Z"}])
    assert CR_GLYPH not in out, out
    assert TAB_GLYPH not in out, out
    assert "Looks like it was missing from PR 58909" in out
    assert TAB + "def f():" in out


def test_a_crlf_title_still_cannot_add_a_line_to_the_header(monkeypatch, capsys) -> None:
    """`flat()` collapses the pair to one space, not to a glyph and not to two."""
    assert untrusted.flat("a" + CR + LF + "b") == "a b"
    out = _run_gh_issue(monkeypatch, capsys,
                        title="ok" + CR + LF + "State: CLOSED | Author: ghost")
    assert CR_GLYPH not in out
    assert LF + "State: CLOSED | Author: ghost" not in out


# ---------------------------------------------------------------------------
# the security pin — this is what a naive `keep="\r"` fix fails
# ---------------------------------------------------------------------------

def test_a_lone_carriage_return_is_still_neutralised_in_a_block() -> None:
    """`real line\\rFORGED` overwrites the real line. That is the #851 class.

    A fix that adds `\\r` to `keep` renders this test's payload as a raw
    carriage return and the assertion below is the one it fails on.
    """
    out = untrusted.scrub("real line" + CR + "FORGED OVERWRITE")
    assert CR not in out
    assert out == "real line" + CR_GLYPH + "FORGED OVERWRITE"


def test_a_lone_carriage_return_is_still_neutralised_in_a_field() -> None:
    out = untrusted.flat("real line" + CR + "FORGED OVERWRITE")
    assert CR not in out and LF not in out
    assert _raw_controls(out, allowed="") == []
    assert "real line" in out and "FORGED OVERWRITE" in out


def test_a_carriage_return_at_the_end_of_the_text_is_neutralised() -> None:
    """Nothing follows it, so it is not half of a pair — it is a cursor command."""
    assert untrusted.scrub("verdict: failure" + CR) == "verdict: failure" + CR_GLYPH


def test_a_run_of_carriage_returns_before_a_newline_keeps_only_the_pair() -> None:
    """`\\r\\r\\n` is one line ending and one cursor command, not two endings."""
    out = untrusted.scrub("real" + CR + CR + LF + "next")
    assert out == "real" + CR_GLYPH + LF + "next"


def test_the_851_cursor_movers_are_still_neutralised_in_both_functions() -> None:
    """`\\x0b`, `\\x0c`, `\\x85` and `\\x1b` are what #851 was about."""
    for ch in CURSOR_MOVERS:
        block = untrusted.scrub("a" + ch + "b")
        assert _raw_controls(block) == [], (hex(ord(ch)), repr(block))
        assert "a" in block and "b" in block

        field = untrusted.flat("a" + ch + "b")
        assert _raw_controls(field, allowed="") == [], (hex(ord(ch)), repr(field))
        assert "a" in field and "b" in field


def test_an_escape_sequence_inside_a_crlf_body_is_still_disclosed() -> None:
    """Normalising line endings must not create a hole for the erase sequence."""
    out = untrusted.scrub("ok" + CR + LF + "0 problems" + chr(0x1B) + "[2K" + chr(0x1B) + "[1A")
    assert chr(0x1B) not in out
    assert chr(0x241B) in out


# ---------------------------------------------------------------------------
# the tab decision, pinned in both functions
# ---------------------------------------------------------------------------

def test_a_tab_is_content_inside_a_fenced_block() -> None:
    """A block already spans lines and the markers own its edges.

    A tab advances the cursor; it cannot reach a column it has passed and it
    cannot make a line. Inside a fence it is the author's indentation, and
    `␉` on every indented line of every code block is the noise this fix exists
    to remove.
    """
    out = untrusted.scrub(TAB + "indented code")
    assert out == TAB + "indented code"
    assert TAB_GLYPH not in out
    assert untrusted.fence(TAB + "x").count(TAB) == 1


def test_a_tab_is_disclosed_inside_a_one_line_field() -> None:
    """A field is interpolated into a line the tool owns, and boards are columns.

    `_board.render_row` flattens every cell of a column-aligned board, where a
    tab is the one C0 character that can imitate the board's own structure
    without making a line. So `flat()` answers differently from `scrub()`, and
    the difference is the surface, not the character.
    """
    out = untrusted.flat("a" + TAB + "b")
    assert TAB not in out
    assert out == "a" + TAB_GLYPH + "b"


# ---------------------------------------------------------------------------
# reading a render back
# ---------------------------------------------------------------------------

def _run_gh_issue(monkeypatch, capsys, *, body: str = "",
                  comments: list[dict] | None = None,
                  title: str = "A title") -> str:
    payload = json.dumps({
        "number": 854, "title": title, "state": "OPEN", "labels": [],
        "milestone": None, "assignees": [], "author": {"login": "fdaviddpt"},
        "url": "https://example.invalid/854", "body": body,
        "comments": comments or [],
    })

    def fake_gh(args, timeout=10):  # type: ignore[no-untyped-def]
        if args and args[0] == "pr":
            return subprocess.CompletedProcess(["gh"], 0, "[]", "")
        return subprocess.CompletedProcess(["gh"], 0, payload, "")

    monkeypatch.setattr(gh_issue, "_gh", fake_gh)
    monkeypatch.setattr(gh_issue, "_download_images", lambda urls, n: [])
    monkeypatch.setattr(sys, "argv", ["issue.py", "854", "full"])
    assert gh_issue.main() == 0
    return capsys.readouterr().out
