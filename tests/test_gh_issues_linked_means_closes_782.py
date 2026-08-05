"""`gh-issues` must rank on *will close*, not *mentions* (#782).

`_linked` fed the rank from `timelineItems`, which merges two different facts.
Measured on this repo before any code was written:

    issue #735: CrossReferencedEvent -> PR#736   (#736 closes #720 — a mention)
    issue #778: CrossReferencedEvent -> PR#781   (#781 closes #778 — a closer)

A `Closes #N` line in a PR body produces a `CrossReferencedEvent` exactly like
prose does; `ConnectedEvent` comes only from the Development sidebar, unused
here. So `__typename` cannot discriminate, and the issue's own first suggested
fix — split on it — would have marked #778 *unlinked*, dropping true links
while keeping false ones. Strictly worse than the bug.

`closedByPullRequestsReferences` is the relationship GitHub itself computes.
Verified 4-for-4: #735 none, #778 -> PR#781, #746 none, #782 none.

Why this is worse than a wrong cell: `_linked` is a **rank tier**. A merged PR
that merely name-drops an issue pushed that issue *down* the board, so work
nobody had started sorted below work someone had. The board exists to answer
"is anyone on it"; it was answering "has anyone mentioned it".
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "github" / "issues.py"
_spec = importlib.util.spec_from_file_location("gh_issues_782", PRESET)
assert _spec is not None and _spec.loader is not None
issues = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issues)


def _node(*, closers=None, mentions=None, **extra) -> dict:
    """A GraphQL issue node carrying closers and/or bare cross-references."""
    node = {
        "number": 1,
        "authorAssociation": "OWNER",
        "lastEditedAt": None,
        "closedByPullRequestsReferences": {
            "nodes": [{"number": n, "state": s} for n, s in (closers or [])]
        },
        "timelineItems": {
            "nodes": [
                {
                    "__typename": "CrossReferencedEvent",
                    "source": {"__typename": "PullRequest",
                               "number": n, "state": s},
                }
                for n, s in (mentions or [])
            ]
        },
    }
    node.update(extra)
    return node


# --- the query must ask for the field at all -------------------------------

def test_query_requests_closers_with_closed_prs_included() -> None:
    """`includeClosedPrs` is not optional.

    Without it a *merged* closer vanishes, which recreates this defect in the
    opposite direction — an issue that was fixed and merged renders as
    unclaimed, which is the input that gets work re-delegated.
    """
    q = issues._graphql_query("o", "n", [1])

    assert "closedByPullRequestsReferences" in q
    assert "includeClosedPrs: true" in q


# --- the discrimination itself ---------------------------------------------

def test_a_merged_pr_that_only_mentions_the_issue_is_not_linked() -> None:
    """The #735 / #736 case — the false positive this issue is about."""
    linked = issues._closing_prs(_node(mentions=[(736, "MERGED")]))

    assert linked == [], (
        "a PR that merely references the number counts as linked, so an "
        f"untouched issue sinks in the rank. Got {linked!r}"
    )


def test_a_merged_closer_is_linked() -> None:
    """The #778 / #781 case — a true link that must survive."""
    linked = issues._closing_prs(_node(closers=[(781, "MERGED")]))

    assert [pr["number"] for pr in linked] == [781]


def test_a_closer_wins_even_when_mentions_are_also_present() -> None:
    """Real issues have both; the closer is the answer."""
    node = _node(closers=[(781, "MERGED")], mentions=[(736, "MERGED")])

    assert [pr["number"] for pr in issues._closing_prs(node)] == [781]


# --- three states, which matter more here than anywhere else ---------------

def test_a_missing_field_is_unknown_not_empty() -> None:
    """Enrichment that could not answer must not render as 'no PR'.

    `_linked` is a rank tier, so an unknown that sorts as 'nobody is on it'
    does not merely misreport — it places the row wrongly and it gets worked
    in the wrong order.
    """
    node = _node()
    del node["closedByPullRequestsReferences"]

    assert issues._closing_prs(node) is None


def test_an_explicit_null_is_also_unknown() -> None:
    """GraphQL returns null for a field it could not resolve."""
    node = _node()
    node["closedByPullRequestsReferences"] = None

    assert issues._closing_prs(node) is None


def test_a_genuine_empty_list_is_no_pr_not_unknown() -> None:
    """The control: an answered zero is a real answer and must stay distinct."""
    assert issues._closing_prs(_node()) == []


# --- the rank consequence, end to end --------------------------------------

@pytest.mark.parametrize(
    "linked, expect_tier",
    [([], 0), ([{"number": 1, "state": "OPEN"}], 1)],
    ids=["unlinked-sorts-first", "linked-sorts-later"],
)
def test_rank_tier_follows_the_corrected_signal(linked, expect_tier) -> None:
    row = {"_external": False, "_stale": False, "_linked": linked,
           "createdAt": "2026-01-01"}

    assert issues._rank_key(row)[3] == expect_tier
