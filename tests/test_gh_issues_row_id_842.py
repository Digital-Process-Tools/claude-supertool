"""`gh-issues` rows must lead their `#N` with the issue's own number, not a
mention's or a linked PR's (#842).

Reproduced live against `claude-remember`: a row read as issue `#231`, and
`#231` is a merged pull request — the open issue is `#226`. The row's own
number was always in `_row()`'s `ident` slot (`row["number"]`); the defect is
that `_linked_cell()` puts a *second*, foreign `#N` earlier on the line, in
the status cell, formatted identically to the row's own id. A reader taking
the first `#N` they see — the natural read order — gets the wrong object, and
the next move, `gh-issue:<that number>`, answers about it with nothing saying
so.

The bar from the issue: a test that merely checks the issue's number appears
*somewhere* in the row passes on the broken code (the number was always
present, in the fourth column) — so these assert on the *first* `#N` token,
positionally, which is the only assertion the broken render fails.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "issues.py"
_spec = importlib.util.spec_from_file_location("github_issues_842", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
issues = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issues)


def _issue(number: int, **kw: object) -> dict:
    row = {
        "number": number,
        "title": f"issue {number}",
        "state": "OPEN",
        "author": {"login": "someone"},
        "labels": [],
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "comments": [],
        "url": f"https://github.com/o/n/issues/{number}",
        "_external": False,
        "_stale": True,
        "_linked": None,
        "_mentions": None,
        "_comments": 1,
    }
    row.update(kw)
    return row


def _first_hash_number(head: str) -> str | None:
    m = re.search(r"#(\d+)", head)
    return m.group(1) if m else None


def test_mention_row_leads_with_the_issues_own_number_not_the_mentioning_prs() -> None:
    """`~ #556 mention +5 … #554` — the live #842 reproduction, one PR."""
    row = _issue(554, _linked=[],
                 _mentions=[{"number": 556, "state": "OPEN"},
                            {"number": 1, "state": "OPEN"}])
    head = issues._row(row).splitlines()[0]
    assert _first_hash_number(head) == "554", (
        f"the first #N on the row must be the issue's own number (554); "
        f"got {head!r}"
    )


def test_linked_closer_row_leads_with_the_issues_own_number_not_the_prs() -> None:
    """`✓ #761 merged +1 … #766` shape — a real closer, same defect class."""
    row = _issue(766, _linked=[{"number": 761, "state": "MERGED"},
                               {"number": 762, "state": "OPEN"}])
    head = issues._row(row).splitlines()[0]
    assert _first_hash_number(head) == "766", (
        f"the first #N on the row must be the issue's own number (766); "
        f"got {head!r}"
    )


def test_mention_row_still_names_the_mentioning_pr_and_the_extra_count() -> None:
    """The fix is ordering/labelling, not content — #556 and +1 must survive."""
    row = _issue(554, _linked=[],
                 _mentions=[{"number": 556, "state": "OPEN"},
                            {"number": 1, "state": "OPEN"}])
    head = issues._row(row).splitlines()[0]
    assert "556" in head
    assert "+1" in head


def test_the_pasteable_hash_sigil_never_precedes_a_foreign_number() -> None:
    """`#` is this board's sigil for 'the row's subject' (`_board.render_row`
    only ever spells `sigil="#"` for `ident`). A foreign PR number must never
    be spelled with a leading `#`, or it reads as pasteable into `gh-issue:`.
    """
    mention_cell = issues._linked_cell([], [{"number": 556, "state": "OPEN"}])
    assert "#556" not in mention_cell, mention_cell

    linked_cell = issues._linked_cell([{"number": 761, "state": "MERGED"}])
    assert "#761" not in linked_cell, linked_cell
