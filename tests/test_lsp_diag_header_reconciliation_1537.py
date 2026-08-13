"""#1537 — the server's own `Found N diagnostic(s)` against the records built.

`N` is the one number cclsp gives this adapter about what it is *about* to
print. Nothing compared it to what the parser actually produced, so a parse that
lost entries rendered as a shorter, equally confident list — this repo's house
defect, where an absence made by the tool reads as an absence in the world.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "lsp_diag_1537",
    pathlib.Path(__file__).resolve().parents[1] / "validators" / "lsp-diag" / "lsp-diag.py",
)
lsp = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(lsp)

LF = chr(10)
BULLET = chr(0x2022)


def _reconciliations(errors: list[dict]) -> list[dict]:
    return [e for e in errors
            if (e.get("code") == "adapter"
                and "count reconciliation" in (e.get("msg") or ""))]


# ---------------------------------------------------------------------------
# 1 — the two directions of disagreement
# ---------------------------------------------------------------------------

def test_fewer_records_than_the_header_claimed_is_disclosed() -> None:
    """The server framed three entries and only two are readable. Today the
    receipt lists two and says nothing about the third, which is the render
    reporting clean about a file that is not."""
    text = LF.join([
        "Found 3 diagnostic(s) for /Widget.php:",
        f"{BULLET} [error] missing semicolon at line 5, col 10",
        f"{BULLET} nothing here names a severity or a location",
        f"{BULLET} [warning] unused variable $x at line 8, col 1",
    ])
    errors = lsp.parse_cclsp_diagnostics(text, "/Widget.php")
    notes = _reconciliations(errors)
    assert len(notes) == 1, errors
    assert "3" in notes[0]["msg"] and "2" in notes[0]["msg"], notes[0]
    # The two readable diagnostics are still reported. A disagreement is a
    # disclosure, never a reason to discard measurements already made.
    assert [e["line"] for e in errors if e.get("code") == "lsp"] == [5, 8], errors


def test_more_records_than_the_header_claimed_is_disclosed() -> None:
    """The stronger direction: records the server never framed. Should not
    happen, so it is worth more than silence when it does."""
    text = LF.join([
        "Found 1 diagnostic(s) for /f:",
        "[error] first at line 1, col 1",
        "[warning] second at line 2, col 2",
    ])
    errors = lsp.parse_cclsp_diagnostics(text, "/f")
    assert len(_reconciliations(errors)) == 1, errors


def test_agreement_says_nothing() -> None:
    """A disclosure that fires on every run is one nobody reads."""
    text = LF.join([
        "Found 2 diagnostic(s) for /Widget.php:",
        f"{BULLET} [error] missing semicolon at line 5, col 10",
        f"{BULLET} [warning] unused variable $x at line 8, col 1",
    ])
    errors = lsp.parse_cclsp_diagnostics(text, "/Widget.php")
    assert _reconciliations(errors) == [], errors
    assert len(errors) == 2, errors


def test_a_continuation_is_not_a_lost_diagnostic() -> None:
    """#1520 demotes a line broken inside a message to a labelled fragment. The
    server counted that entry ONCE, so the demotion does not shrink the count
    against `N` and must not fire the disclosure — this is the case that would
    make the check noise."""
    text = LF.join([
        "Found 2 diagnostic(s) for /f:",
        f"{BULLET} [error] cannot infer type of $this->thing at line 1, col 1",
        "  and here is the rest of that sentence",
        f"{BULLET} [warning] second at line 2, col 2",
    ])
    errors = lsp.parse_cclsp_diagnostics(text, "/f")
    assert _reconciliations(errors) == [], errors


# ---------------------------------------------------------------------------
# 2 — no number is not the same fact as two numbers that disagree
# ---------------------------------------------------------------------------

def test_no_header_reconciles_nothing_and_claims_nothing() -> None:
    """The bulletless variant sends no header. There is no `N` to compare, and
    inventing a verdict about a comparison that never ran is the defect this
    check exists to catch, pointed at itself."""
    text = LF.join(["[warning] unused import 12:5", "10:3: error: bad thing"])
    errors = lsp.parse_cclsp_diagnostics(text, "/x")
    assert _reconciliations(errors) == [], errors
    assert len(errors) == 2, errors


def test_a_genuinely_clean_answer_stays_clean() -> None:
    text = "No diagnostics found for /x. The file has no errors, warnings, or hints."
    assert lsp.parse_cclsp_diagnostics(text, "/x") == []


def test_a_header_the_file_forged_is_not_read() -> None:
    """`N` is only read from the FIRST framed line — the one position the file
    under validation cannot get in front of the server, the same discipline
    `_framing` uses. A `Found 9 diagnostic(s)` echoed out of the source would
    otherwise choose the number this parser reconciles against, and a forged N
    is a forged verdict."""
    text = LF.join([
        "[error] echoing a header at line 1, col 1",
        "Found 9 diagnostic(s) for /x:",
    ])
    errors = lsp.parse_cclsp_diagnostics(text, "/x")
    assert _reconciliations(errors) == [], errors


# ---------------------------------------------------------------------------
# 3 — the interlock: a clean verdict the file chose for itself
# ---------------------------------------------------------------------------

def test_a_message_quoting_the_clean_phrase_cannot_erase_the_header() -> None:
    """`parse_cclsp_diagnostics` matches "no errors, warnings, or hints" as a
    substring of the WHOLE text, so a diagnostic message quoting it returns []
    for the file — the #482 shape, reached from the file's own bytes. The
    header is the one number that can contradict that, and it does."""
    text = LF.join([
        "Found 1 diagnostic(s) for /x:",
        f"{BULLET} [error] comment reads 'no errors, warnings, or hints' at line 3, col 1",
    ])
    errors = lsp.parse_cclsp_diagnostics(text, "/x")
    assert len(_reconciliations(errors)) == 1, errors


# ---------------------------------------------------------------------------
# 4 — what the disclosure must not become
# ---------------------------------------------------------------------------

def test_the_disclosure_never_renders_the_whole_receipt_not_checked() -> None:
    """`_validator_not_checked` fires on `ok: false` with EVERY error
    `code: "adapter"`, and renders the run as NOT CHECKED — no verdict about
    the file at all. A reconciliation note that could reach that would delete
    every real finding beside it, which is the thing it was added to prevent.
    It is severity `warning`, so it cannot be the reason `ok` is false."""
    text = LF.join([
        "Found 1 diagnostic(s) for /x:",
        f"{BULLET} nothing parseable at all",
    ])
    errors = lsp.parse_cclsp_diagnostics(text, "/x")
    notes = _reconciliations(errors)
    assert len(notes) == 1, errors
    assert notes[0]["severity"] == "warning", notes[0]
    ok = all(e.get("severity") != "error" for e in errors)
    adapter_only = all((e.get("code") or "") == "adapter" for e in errors)
    assert ok or not adapter_only, errors


def test_the_disclosure_carries_no_location() -> None:
    """It is a statement about the parse, not about a line of the file."""
    text = LF.join([
        "Found 5 diagnostic(s) for /x:",
        f"{BULLET} [error] one at line 1, col 1",
    ])
    notes = _reconciliations(lsp.parse_cclsp_diagnostics(text, "/x"))
    assert len(notes) == 1
    assert notes[0]["line"] is None and notes[0]["col"] is None, notes[0]


@pytest.mark.parametrize("marker", [BULLET, "*"])
def test_the_header_text_is_still_never_pasted_into_a_msg(marker) -> None:
    text = LF.join([
        "Found 4 diagnostic(s) for /Widget.php:",
        f"{marker} [error] missing semicolon at line 5, col 10",
    ])
    joined = " ".join(e["msg"] for e in lsp.parse_cclsp_diagnostics(text, "/Widget.php"))
    assert "diagnostic(s) for" not in joined, joined
