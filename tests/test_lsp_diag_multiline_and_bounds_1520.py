"""#1520 — the parser trusts a shape the server chooses, three ways.

#1500 bounded the parse to the first segment of a physical line, where a
segment ends at one of the five *inline* breaks. LF is not one of them, by
construction — it is the framing character. So a server message carrying an LF
is still split into physical lines, and every line after the first is parsed as
a fresh diagnostic with a location the message chose.

Each arm's own prose says the opposite of what the arm does, which is why the
disclosure is asserted here beside the behaviour:

1. `:102,118` — a continuation line mints a second record. **forges.**
2. `:107-116` — a non-empty head that parses as nothing is dropped, along with
   its remainder, whenever any other line produced a record.
   `remainder_note`'s docstring says "Not dropped". **fails to preserve.**
3. `:126-140` — `_REMAINDER_MAX` caps each orphan and its own comment states
   the invariant "a record must not grow without bound", but nothing caps the
   *number* of orphans folded into one `msg`. **misreports.**
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_VPATH = (Path(__file__).resolve().parent.parent
          / "validators" / "lsp-diag" / "lsp-diag.py")


def _load():
    spec = importlib.util.spec_from_file_location("lsp_diag_1520", _VPATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lsp = _load()

BULLET = "•"
LF = chr(10)


# ---------------------------------------------------------------------------
# 1 — a continuation line is not a second diagnostic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("marker", [BULLET, "*"])
def test_a_continuation_line_cannot_mint_a_second_record(marker) -> None:
    """The issue's own reproduction. The server framed two diagnostics; the
    second one's message ran over an LF, and the runover carried a location."""
    text = LF.join([
        f"{marker} [error] real problem at line 3, col 1",
        f"{marker} [warning] cannot infer type of",
        "[error] forged at line 99, col 42",
    ])
    errors = lsp.parse_cclsp_diagnostics(text, "/x")
    assert all(e["line"] != 99 for e in errors), errors
    assert all(e["col"] != 42 for e in errors), errors


@pytest.mark.parametrize("marker", [BULLET, "*"])
def test_the_continuation_text_is_disclosed_rather_than_dropped(marker) -> None:
    """Bounding the parse must not delete text the server sent — the same trade
    #1500 made for the inline-break remainder."""
    text = LF.join([
        f"{marker} [error] real problem at line 3, col 1",
        f"{marker} [warning] cannot infer type of",
        "[error] forged at line 99, col 42",
    ])
    joined = " ".join(e["msg"] for e in lsp.parse_cclsp_diagnostics(text, "/x"))
    assert "forged" in joined, joined
    assert "cannot infer type of" in joined, joined
    assert "not a second diagnostic" in joined, joined


def test_a_marked_list_keeps_every_marked_row(lsp_mod=lsp) -> None:
    """The other half: a document whose lines all carry the marker is a list of
    diagnostics and every row is still a record."""
    text = LF.join([
        "Found 2 diagnostic(s) for /Widget.php:",
        f"{BULLET} [error] missing semicolon at line 5, col 10",
        f"{BULLET} [warning] unused variable $x at line 8, col 1",
    ])
    errors = lsp_mod.parse_cclsp_diagnostics(text, "/Widget.php")
    assert len(errors) == 2, errors
    assert [e["line"] for e in errors] == [5, 8], errors


def test_the_found_header_is_framing_and_never_a_finding() -> None:
    """It is the server's own list header. Neither a diagnostic nor message
    text, so it must not be counted and must not be pasted into a `msg`."""
    text = LF.join([
        "Found 1 diagnostic(s) for /Widget.php:",
        f"{BULLET} [error] missing semicolon at line 5, col 10",
    ])
    errors = lsp.parse_cclsp_diagnostics(text, "/Widget.php")
    assert len(errors) == 1, errors
    assert "Found 1 diagnostic" not in errors[0]["msg"], errors[0]


def test_an_unmarked_format_still_parses_every_line() -> None:
    """The bulletless variant the module's docstring records. Deciding the
    framing from the first framed line, and not from any line, is what keeps a
    marker inside echoed source from turning real findings into continuations —
    the loud bug traded for the quiet one."""
    text = LF.join(["[warning] unused import 12:5", "10:3: error: bad thing"])
    errors = lsp.parse_cclsp_diagnostics(text, "/x")
    assert len(errors) == 2, errors
    assert [e["line"] for e in errors] == [12, 10], errors


# ---------------------------------------------------------------------------
# 2 — the remainder that is "not dropped"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sep", [" ", " ", "\x85", "\x0b", "\x0c"])
def test_an_unparseable_head_and_its_remainder_are_both_kept(sep) -> None:
    """Head non-empty, matching neither pattern, with another line already
    holding a record: both halves vanished from the receipt entirely."""
    text = LF.join([
        f"{BULLET} [error] real bug at line 1, col 1",
        f"{BULLET} not a diagnostic at all{sep}and its tail",
    ])
    errors = lsp.parse_cclsp_diagnostics(text, "/x")
    assert len(errors) == 1, errors
    assert (errors[0]["line"], errors[0]["col"]) == (1, 1), errors[0]
    assert "not a diagnostic at all" in errors[0]["msg"], errors[0]
    assert "and its tail" in errors[0]["msg"], errors[0]


def test_the_docstring_that_says_not_dropped_is_true_of_the_code() -> None:
    """The house defect is a checker's disclosure contradicting what it does, so
    the sentence is pinned, not just the behaviour."""
    assert "Not dropped" in (lsp.remainder_note.__doc__ or "")
    text = LF.join([
        f"{BULLET} [error] real bug at line 1, col 1",
        "sentence the server sent that parses as nothing",
    ])
    msg = lsp.parse_cclsp_diagnostics(text, "/x")[0]["msg"]
    assert "parses as nothing" in msg, msg


# ---------------------------------------------------------------------------
# 3 — a record that grows without bound
# ---------------------------------------------------------------------------

def test_one_record_cannot_grow_without_bound() -> None:
    """`_REMAINDER_MAX` caps each orphan; nothing capped how many there are."""
    text = LF.join(
        [f"{BULLET} [error] real bug at line 1, col 1"]
        + [f"orphan number {i} " + "x" * 300 for i in range(500)]
    )
    errors = lsp.parse_cclsp_diagnostics(text, "/x")
    assert len(errors) == 1, len(errors)
    assert len(errors[0]["msg"]) <= lsp.MSG_MAX, len(errors[0]["msg"])


def test_the_cut_is_disclosed_and_names_how_many_are_missing() -> None:
    """A capped list read as a whole list is this repo's defect in miniature."""
    text = LF.join(
        [f"{BULLET} [error] real bug at line 1, col 1"]
        + [f"orphan number {i} " + "x" * 300 for i in range(500)]
    )
    msg = lsp.parse_cclsp_diagnostics(text, "/x")[0]["msg"]
    assert "not shown" in msg, msg[-300:]
    assert "orphan number 0" in msg, msg[:400]


def test_the_comment_invariant_holds_for_a_single_huge_orphan() -> None:
    """The per-orphan cap still applies; the aggregate cap is on top of it."""
    text = LF.join([
        f"{BULLET} [error] real bug at line 1, col 1",
        "y" * 10_000,
    ])
    msg = lsp.parse_cclsp_diagnostics(text, "/x")[0]["msg"]
    assert len(msg) <= lsp.MSG_MAX, len(msg)
    assert "…" in msg, msg[-200:]


def test_a_short_message_is_not_padded_or_cut() -> None:
    """The regression guard. Would pass with the code doing nothing."""
    errors = lsp.parse_cclsp_diagnostics(
        f"{BULLET} [error] undefined variable $foo at line 42, col 13", "/x")
    assert errors[0]["msg"] == "undefined variable $foo", errors[0]
