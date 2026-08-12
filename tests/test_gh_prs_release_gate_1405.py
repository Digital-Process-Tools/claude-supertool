"""The release PR is the boundary commit, and the clock could not say so (#1405).

Two things are pinned here, and they arrived together because the second is what
made the first cheap to fix.

## The defect (#1405)

`gh-since-tag` excluded the PR whose merge *is* the tagged commit by comparing
instants: keep rows strictly after the boundary. That only holds if GitHub's
`merged_at` equals the merge commit's committer date, and it does not — GitHub
stamps `merged_at` when it records the merge, after the commit is written.
Measured on this repository's own releases, tag commit date vs the release PR's
`merged_at`:

    v0.30.0  #1161  08-09T06:29:26Z  ==  06:29:26Z   equal
    v0.31.0  #1198  08-09T15:13:43Z  ==  15:13:43Z   equal
    v0.32.0  #1250  08-09T22:35:33Z  <   22:35:34Z   ONE SECOND LATER
    v0.33.0  #1289  08-11T00:39:57Z  ==  00:39:57Z   equal
    v0.34.0  #1403  08-11T13:34:34Z  <   13:34:35Z   ONE SECOND LATER

Two in five, not "every release" as the issue title says. On the two that lost
the coin flip the release PR was counted as merged-since (count off by one) AND
absent from `TAG..master`, which excludes the tagged commit by construction — so
the reconciliation rendered UNVERIFIED. `merge_commit_sha` equalled the tagged
sha in every one of the five. The boundary is a commit; it is applied as
identity now, not as a clock.

## The fold

`gh-since-tag` is retired into `gh-prs:merged-since=TAG`. The tests for the
judgement that moved — boundary resolution, the reconcile, the fragment count —
stay in `tests/test_github_since_tag_1209.py` against `_release_gate`, which is
the same code under a new name. What is new here is the boundary's identity rule
and the module surface `gh-prs` consumes.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PRESETS = Path(__file__).parent.parent / "presets"
sys.path.insert(0, str(PRESETS))
sys.path.insert(0, str(PRESETS / "github"))

_spec = importlib.util.spec_from_file_location(
    "github_release_gate_1405", PRESETS / "github" / "_release_gate.py")
assert _spec is not None and _spec.loader is not None
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)


# v0.34.0, verbatim. The tag's commit instant, and the release PR that made it.
TAG_SHA = "a6c3a8bcf3e5db09e2f5b06cb54cc9187f2c5232"
TAG_COMMIT_DATE = "2026-08-11T15:34:34+02:00"   # = 13:34:34Z
RELEASE_PR = {
    "number": 1403,
    "title": "release: v0.34.0",
    "mergedAt": "2026-08-11T13:34:35Z",          # ONE SECOND after the commit
    "mergeCommit": {"oid": TAG_SHA},
}
LATER_PR = {
    "number": 1404,
    "title": "after the release",
    "mergedAt": "2026-08-11T16:00:00Z",
    "mergeCommit": {"oid": "f" * 40},
}


# ---------------------------------------------------------------------------
# The boundary is a commit, not an instant
# ---------------------------------------------------------------------------

def test_the_clock_really_does_put_the_release_pr_after_its_own_tag() -> None:
    """Not a test of our code — a pin on why the instant cannot do this job."""
    boundary = rg.parse_instant(TAG_COMMIT_DATE)
    merged = rg.parse_instant(RELEASE_PR["mergedAt"])
    assert boundary is not None and merged is not None
    assert merged > boundary
    kept, _undated = rg.filter_merged([RELEASE_PR], boundary)
    assert [r["number"] for r in kept] == [1403]


def test_the_row_whose_merge_is_the_tagged_commit_is_removed_by_identity() -> None:
    rest, tagged = rg.split_tagged_commit([RELEASE_PR, LATER_PR], TAG_SHA)
    assert [r["number"] for r in rest] == [1404]
    assert tagged is not None and tagged["number"] == 1403


def test_the_boundary_row_is_handed_back_so_it_can_be_disclosed() -> None:
    """Removing it silently would be the mute button this op exists to refuse."""
    _rest, tagged = rg.split_tagged_commit([RELEASE_PR], TAG_SHA)
    assert tagged is RELEASE_PR


def test_a_row_merged_as_some_other_commit_is_left_alone() -> None:
    rest, tagged = rg.split_tagged_commit([LATER_PR], TAG_SHA)
    assert [r["number"] for r in rest] == [1404]
    assert tagged is None


def test_an_amended_merge_commit_no_longer_matches_and_keeps_its_refusal() -> None:
    """#1405's third question: a squash commit rewritten after the merge.

    Its sha is no longer the API's `mergeCommit`, so identity does not pair it,
    it stays in the API set, and the reconcile renders it as the disagreement it
    is. That row deserves the refusal; the release PR does not.
    """
    amended = dict(RELEASE_PR, mergeCommit={"oid": "0" * 40})
    rest, tagged = rg.split_tagged_commit([amended], TAG_SHA)
    assert tagged is None
    assert [r["number"] for r in rest] == [1403]


def test_a_row_carrying_no_merge_commit_is_not_guessed_at() -> None:
    """No identity to compare is not a match and not a mismatch — leave it."""
    for row in ({"number": 7}, {"number": 7, "mergeCommit": None},
                {"number": 7, "mergeCommit": {}},
                {"number": 7, "mergeCommit": "a6c3a8b"}):
        rest, tagged = rg.split_tagged_commit([row], TAG_SHA)
        assert tagged is None, row
        assert rest == [row]


def test_an_unknown_boundary_sha_excludes_nothing() -> None:
    rest, tagged = rg.split_tagged_commit([RELEASE_PR, LATER_PR], "")
    assert [r["number"] for r in rest] == [1403, 1404]
    assert tagged is None


def test_the_merge_commit_is_actually_asked_for() -> None:
    """Identity by sha is worthless if the field never leaves the query."""
    assert "mergeCommit" in rg.PR_LIST_FIELDS.split(",")


def test_the_boundary_slice_still_does_not_pay_for_the_boards_field_set() -> None:
    """#1411's pin, carried across the fold. The gate is not a triage board."""
    assert "statusCheckRollup" not in rg.PR_LIST_FIELDS
    assert "reviewDecision" not in rg.PR_LIST_FIELDS


def test_read_tags_keeps_the_full_sha_because_seven_characters_are_not_an_id() -> None:
    fields = ["v0.34.0", "commit", "", TAG_COMMIT_DATE, "", TAG_SHA]

    def fake_run(argv, timeout):
        if argv[:2] == ["git", "for-each-ref"]:
            return True, "\t".join(fields) + "\n", ""
        return True, "v0.34.0\n", ""

    original = rg._run
    rg._run = fake_run
    try:
        tags, note = rg.read_tags("refs/remotes/origin/master")
    finally:
        rg._run = original
    assert note == ""
    assert len(tags) == 1
    assert tags[0]["full_sha"] == TAG_SHA
    assert tags[0]["sha"] == TAG_SHA[:7]
