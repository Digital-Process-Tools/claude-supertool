"""A capped comment list keeps both ends and says what it dropped (#738, after #719).

#719 fixed the *disclosure*: `## Comments (25)` above ten of them now reads `10
of 25 shown`. It deliberately left *which* ten. #738 asks whether the ten should
be the newest (what shipped) or the oldest (#719's own argument: the opening
objection, the design decision, the "do not merge until X" all land early).

The answer taken here is neither, because the question has no evidence behind it
and the shape of the failure says it never will. Both sides of #738 are correct
about *different comments*: the head carries the objection that opened the
thread, the tail carries the resolution that closed it. A cap that keeps one end
therefore guarantees that on every long thread one of the two load-bearing
regions is gone, and — this is the part that makes it this repo's house defect
rather than a preference — the reader cannot tell which, because a thread whose
opening never mattered renders identically to one whose opening was the whole
point. That is an absence produced by the tool read as an absence in the world.

The evidence #738 asked for cannot be gathered here and that is itself the
argument. Measured 2026-08-11 over the whole tracker, the busiest thread in this
repository has **six** comments and nothing has ever reached the cap, so there
is no local corpus in which to count where the load-bearing comment sits. These
ops read other repos through `repo:OWNER/NAME`, where 25-comment threads are
ordinary. With no measurement available, the design that does not require
choosing an end is the correct one — and it is not a new convention: `gl-mr`'s
`_budgeted_comments` has kept a head and a recency tail with an inline gap
marker since it was written, so the two GitHub ops are converging on a shipped
shape rather than drifting into a third one.

Ten comments still print. The split is 3 + 7, tail-weighted because "where does
this stand" remains the commoner question, and three is enough to carry a thread
that opens with an objection and a reply to it.

Every assertion runs through `main()` and reads rendered stdout: a helper tested
in isolation passes on a version where neither op calls it. Each test pairs an
under-cap control with the over-cap case, so none of them would survive a
do-nothing implementation.
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


_body = _load("presets/_body.py", "supertool_body_738")
gh_pr = _load("presets/github/pr.py", "github_pr_738")
gh_issue = _load("presets/github/issue.py", "github_issue_738")


def _comments(n: int) -> list:
    return [
        {"author": {"login": "user%d" % i}, "body": "[%d] comment body" % i,
         "createdAt": "2026-01-01"}
        for i in range(n)
    ]


def _gh_pr_fakes(monkeypatch, comments: list) -> None:
    payload = json.dumps({
        "number": 12, "title": "feat: thing", "state": "OPEN",
        "author": {"login": "max"}, "headRefName": "feat/x",
        "baseRefName": "master", "labels": [], "milestone": None,
        "isDraft": False, "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
        "reviews": [], "mergeCommit": None, "additions": 1, "deletions": 1,
        "changedFiles": 1, "statusCheckRollup": [], "url": "",
        "body": "short body", "comments": comments, "assignees": [],
        "createdAt": "", "updatedAt": "",
    })

    def fake_gh(args, timeout=10):  # type: ignore[no-untyped-def]
        if args and args[0] == "issue":
            return subprocess.CompletedProcess(["gh"], 1, "", "not found")
        return subprocess.CompletedProcess(["gh"], 0, payload, "")

    monkeypatch.setattr(gh_pr, "_gh", fake_gh)
    monkeypatch.setattr(gh_pr, "_fetch_review_threads", lambda url, iid: [])
    monkeypatch.setattr(gh_pr, "_local_branch_check", lambda branch: "")


def _run_gh_pr(monkeypatch, capsys, comments: list, *flags: str) -> str:
    _gh_pr_fakes(monkeypatch, comments)
    monkeypatch.setattr(sys, "argv", ["pr.py", "12", *flags])
    assert gh_pr.main() == 0
    return capsys.readouterr().out


def _gh_issue_fakes(monkeypatch, comments: list) -> None:
    payload = json.dumps({
        "number": 12, "title": "a bug", "state": "OPEN", "labels": [],
        "milestone": None, "assignees": [], "author": {"login": "max"},
        "url": "", "body": "short body", "comments": comments,
    })

    def fake_gh(args, timeout=10):  # type: ignore[no-untyped-def]
        if args and args[0] == "pr":
            return subprocess.CompletedProcess(["gh"], 0, "[]", "")
        return subprocess.CompletedProcess(["gh"], 0, payload, "")

    monkeypatch.setattr(gh_issue, "_gh", fake_gh)
    monkeypatch.setattr(gh_issue, "_download_images", lambda urls, n: [])


def _run_gh_issue(monkeypatch, capsys, comments: list, *flags: str) -> str:
    _gh_issue_fakes(monkeypatch, comments)
    monkeypatch.setattr(sys, "argv", ["issue.py", "12", *flags])
    assert gh_issue.main() == 0
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# the selection
# ---------------------------------------------------------------------------

def test_the_opening_comments_survive_the_cap(monkeypatch, capsys) -> None:
    """The head is the half #719 argued for and the half that used to be gone
    outright — on a 25-comment thread the first fifteen were unreachable."""
    out = _run_gh_issue(monkeypatch, capsys, _comments(25))
    for i in range(_body.COMMENT_HEAD):
        assert "[%d] comment body" % i in out


def test_the_closing_comments_survive_the_cap(monkeypatch, capsys) -> None:
    """The tail is the half that shipped, and flipping the slice would have
    thrown it away — an objection that was answered is worse signal than the
    answer."""
    out = _run_gh_issue(monkeypatch, capsys, _comments(25))
    for i in range(25 - _body.COMMENT_TAIL, 25):
        assert "[%d] comment body" % i in out


def test_the_middle_is_what_gets_dropped(monkeypatch, capsys) -> None:
    out = _run_gh_issue(monkeypatch, capsys, _comments(25))
    assert "[3] comment body" not in out
    assert "[12] comment body" not in out
    assert "[17] comment body" not in out


def test_the_number_of_comments_shown_is_unchanged(monkeypatch, capsys) -> None:
    """#738 is a selection change, not a budget change — the cap is still ten,
    so no caller pays more context for it."""
    assert _body.COMMENT_HEAD + _body.COMMENT_TAIL == 10
    out = _run_gh_issue(monkeypatch, capsys, _comments(25))
    assert "## Comments (10 of 25 shown" in out


# ---------------------------------------------------------------------------
# the disclosure — the cut moved, so the words describing it had to
# ---------------------------------------------------------------------------

def test_the_heading_no_longer_claims_the_withheld_ones_are_the_earlier_ones(
    monkeypatch, capsys
) -> None:
    """`15 earlier truncated` was true of a tail-only cut and is false of this
    one. A disclosure that names the wrong end is the defect #719 fixed, wearing
    the fix's own clothes."""
    out = _run_gh_issue(monkeypatch, capsys, _comments(25))
    assert "earlier truncated" not in out
    assert ("## Comments (10 of 25 shown, 15 hidden from the middle — "
            "use :full to fetch all)") in out


def test_the_gap_is_marked_where_it_happens(monkeypatch, capsys) -> None:
    """A header-only disclosure leaves the reader unable to tell that comment
    [2] and comment [18] are not consecutive — which is exactly the re-read the
    header was added to prevent."""
    whole = _run_gh_issue(monkeypatch, capsys, _comments(4))
    capped = _run_gh_issue(monkeypatch, capsys, _comments(25))
    assert "hidden here" not in whole
    assert "…[15 comments hidden here — use :full to fetch all]" in capped
    head_at = capped.index("[2] comment body")
    gap_at = capped.index("hidden here")
    tail_at = capped.index("[18] comment body")
    assert head_at < gap_at < tail_at


def test_a_gap_of_one_comment_says_comment_not_comments(
    monkeypatch, capsys
) -> None:
    """Same agreement bug as #841, one file over — pinned before it is written
    rather than after it is filed."""
    out = _run_gh_issue(monkeypatch, capsys, _comments(11))
    assert "…[1 comment hidden here — use :full to fetch all]" in out


# ---------------------------------------------------------------------------
# the boundaries
# ---------------------------------------------------------------------------

def test_a_thread_at_the_cap_is_whole_and_says_nothing(
    monkeypatch, capsys
) -> None:
    """The absence of a marker has to keep meaning the list is whole."""
    out = _run_gh_issue(monkeypatch, capsys, _comments(10))
    assert "## Comments (10)" in out
    assert "hidden" not in out
    for i in range(10):
        assert "[%d] comment body" % i in out


def test_full_still_returns_every_comment_with_no_markers(
    monkeypatch, capsys
) -> None:
    out = _run_gh_issue(monkeypatch, capsys, _comments(25), "full")
    assert "## Comments (25)" in out
    assert "hidden" not in out
    for i in range(25):
        assert "[%d] comment body" % i in out


# ---------------------------------------------------------------------------
# the two ops must not drift apart again — #719's whole argument
# ---------------------------------------------------------------------------

def test_gh_pr_and_gh_issue_select_and_disclose_identically(
    monkeypatch, capsys
) -> None:
    pr_out = _run_gh_pr(monkeypatch, capsys, _comments(25))
    issue_out = _run_gh_issue(monkeypatch, capsys, _comments(25))
    heading = ("## Comments (10 of 25 shown, 15 hidden from the middle — "
               "use :full to fetch all)")
    gap = "…[15 comments hidden here — use :full to fetch all]"
    for out in (pr_out, issue_out):
        assert heading in out
        assert gap in out
        assert "[0] comment body" in out
        assert "[24] comment body" in out
        assert "[12] comment body" not in out
