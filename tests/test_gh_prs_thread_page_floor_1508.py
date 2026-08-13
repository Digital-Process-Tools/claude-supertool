"""#1508 — `gh-prs` counted unresolved threads over one capped page.

#1491's defect one op over, and it lands on a *flag* rather than on a number:
`gh-prs` never printed the count, so the mis-render the issue body describes
(":597-600 renders it as a bare number") does not exist. What exists is worse.

`_enrich` counts unresolved threads over `reviewThreads(first: THREADS_PAGE_MAX)`
and `_flags` turns that count into a boolean marker. At the cap the count is a
floor, and a floor of **zero** carries no information at all: a PR with 100+
threads whose page-1 threads are all resolved rendered with **no thread flag**,
identical to a PR that has none. GraphQL returns threads in creation order, so
the newest — the unresolved ones — are exactly the ones on page 2. That is
#1445's mechanism reached by a second route: not an omitted number, the removal
of the one marker that would have made the reader look.

A floor **above** zero is not touched. `at least 1 unresolved` and `1 unresolved`
make the same claim once the render is a boolean, so hedging it would be the
failure mode PR #1505's reviewer raised — qualifying every number instead of the
ones the reply cannot establish.

The truncation fact travels as its own key (`_unresolved_floor`) rather than by
collapsing a capped zero into the `None` that means "the call declined". Two
reasons: `_unresolved` then keeps meaning one thing, and a floor above zero stays
a floor for any future consumer instead of being stored as an exact count.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "presets"))
sys.path.insert(0, str(ROOT / "presets" / "github"))
_spec = importlib.util.spec_from_file_location(
    "github_prs_1508", ROOT / "presets" / "github" / "prs.py")
assert _spec is not None and _spec.loader is not None
prs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prs)

import pr as gh_pr  # noqa: E402  (the module the cap constant is defined in)

# Taken off `pr`, not off `prs`, so a missing re-export fails one assertion
# below rather than erroring collection out of every test in the file.
CAP = gh_pr.THREADS_PAGE_MAX


def _threads(total: int, unresolved: int) -> list:
    return [{"isResolved": i >= unresolved} for i in range(total)]


def _enriched(monkeypatch, total: int, unresolved: int) -> dict:
    monkeypatch.setattr(prs, "_fetch_review_threads_detailed",
                        lambda url, n: (_threads(total, unresolved), ""),
                        raising=False)
    pr_list = [{"number": 1508, "url": "u"}]
    prs._enrich(pr_list)
    return pr_list[0]


# --- a zero taken off a capped page is not a zero ---------------------------

def test_a_full_page_of_resolved_threads_does_not_render_as_clean(
        monkeypatch) -> None:
    """The whole defect. Every thread on page 1 resolved, more pages unread."""
    p = _enriched(monkeypatch, CAP, 0)
    assert "threads?" in prs._flags(p), prs._flags(p)


def test_the_page_cap_travels_out_of_enrich(monkeypatch) -> None:
    assert _enriched(monkeypatch, CAP, 0)["_unresolved_floor"] is True


def test_a_short_page_is_not_a_floor(monkeypatch) -> None:
    assert _enriched(monkeypatch, 3, 0)["_unresolved_floor"] is False


def test_the_query_the_count_is_taken_off_asks_for_exactly_this_cap() -> None:
    """The inference `len(threads) >= CAP` is sound only while the constant and
    the query agree. `gh-prs` reads both from `gh-pr` rather than keeping a
    second copy — the number was hand-copied into a comment there until #1491.
    """
    assert prs.THREADS_PAGE_MAX is gh_pr.THREADS_PAGE_MAX
    assert f"reviewThreads(first:{CAP})" in gh_pr._THREADS_QUERY


# --- and only the zero. The rest must stay unhedged ------------------------

def test_a_floor_above_zero_still_carries_the_plain_flag(monkeypatch) -> None:
    """Not vacuous, and deliberately would pass with the production change
    reverted: what it pins is the half that had to stay the same. The render is
    a boolean, so `at least 4 unresolved` and `4 unresolved` are the same claim
    — downgrading this row to `threads?` would trade a finding the reply *does*
    establish for an unknown, which is the failure mode #1505's reviewer raised.
    """
    p = _enriched(monkeypatch, CAP, 4)
    assert "threads" in prs._flags(p) and "threads?" not in prs._flags(p)


def test_an_uncapped_zero_still_carries_no_flag_at_all(monkeypatch) -> None:
    """The complement, same reasoning: a page short of the cap establishes the
    total, so `0` is a count and flagging it would put a marker on every clean
    PR on the board.
    """
    assert prs._flags(_enriched(monkeypatch, 7, 0)) == ""


def test_a_declined_fetch_is_still_a_decline_and_not_a_floor(
        monkeypatch) -> None:
    monkeypatch.setattr(prs, "_fetch_review_threads_detailed",
                        lambda url, n: (None, "HTTP 403: rate limit"),
                        raising=False)
    pr_list = [{"number": 1508, "url": "u"}]
    prs._enrich(pr_list)
    assert pr_list[0]["_unresolved"] is None
    assert pr_list[0]["_unresolved_floor"] is False


# --- the board says which reading `threads?` is ----------------------------

def test_the_board_names_the_page_cap_when_a_row_hit_it() -> None:
    """`threads?` has two producers now. The flag alphabet is one glyph wide on
    a width-sensitive row, so the distinction goes in a line above the board
    rather than in a second token.
    """
    note = prs._floor_note([{"_unresolved_floor": True},
                            {"_unresolved_floor": False}])
    assert note and str(CAP) in note, note


def test_the_board_says_nothing_when_no_row_hit_the_cap() -> None:
    assert not prs._floor_note([{"_unresolved_floor": False}, {}])
