"""A capped comment list must read as capped (#719, after #698/#681).

#698 fixed the *character* half of this: a body cut to a cap with no marker read
as a complete document because nothing said it was not. #719 is the same absence
in **count** form, and it is worse, because the header hands the reader a number
they have every reason to trust:

    ## Comments (25)

    **someone** (2026-01-04):
    ...ten of them...

A reviewer reading that concludes those ten *are* the discussion. The ten shown
were the most recent, so the fifteen withheld were exactly the ones carrying the
original objection. `gh-issue` has said this correctly since #681 — `N of M
shown, K ... — use :full to fetch all`. `gh-pr`, standing right next to it,
never adopted it. This is #263's shape: the convention existed and one call site
had not taken it up.

**Which ten, and the exact wording, moved in #738** — the cap now keeps the
first three and the last seven and says `15 hidden from the middle`. The
assertions here were updated to that rather than deleted: the disclosure
contract is what this file guards, and it survives the selection change intact.

Two more things this pins, both found while checking the issue's own open
questions rather than assumed:

* **`gh-pr`'s per-comment body cap.** Each comment was sliced at `COMMENT_MAX`
  with no marker at all — the character defect #698 fixed for descriptions,
  still live one block further down the same file.
* **`gl-mr`'s `:full` half-works.** Its comment *count* disclosure is fine (a
  budgeted render with an inline gap marker naming the hidden count and bytes).
  But `_render_note` cut every note at `COMMENT_MAX` silently and did so on the
  `:full` path too — while `docs/presets/gitlab.md` promised `:full` "uncaps the
  file list and the comments". A `:full` that a disclosure points at has to be
  true, which is the same reason #698 had to give `gh-pr` a `:full` at all.

Every assertion below fails against the pre-#719 code except where a test pairs
an under-cap control with the over-cap case in the same body — the control alone
would pass a do-nothing implementation, so none of them stands alone. All of it
runs through `main()` and reads the rendered stdout: a helper tested in isolation
passes on a version where `pr.py` never calls it, which is precisely the bug.
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


_body = _load("presets/_body.py", "supertool_body_719")
gh_pr = _load("presets/github/pr.py", "github_pr_719")
gh_issue = _load("presets/github/issue.py", "github_issue_719")
gl_mr = _load("presets/gitlab/mr.py", "gitlab_mr_719")


def _comments(n: int, body: str = "comment body") -> list[dict]:
    return [
        {"author": {"login": f"user{i}"}, "body": f"[{i}] {body}",
         "createdAt": "2026-01-0{}".format(i % 9 + 1)}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# gh-pr:N — the reported defect
# ---------------------------------------------------------------------------

def _gh_pr_fakes(monkeypatch, comments: list[dict]) -> None:
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


def _run_gh_pr(monkeypatch, capsys, comments: list[dict], *flags: str) -> str:
    _gh_pr_fakes(monkeypatch, comments)
    monkeypatch.setattr(sys, "argv", ["pr.py", "12", *flags])
    assert gh_pr.main() == 0
    return capsys.readouterr().out


def test_gh_pr_capped_comment_list_reads_differently_than_a_whole_one(
    monkeypatch, capsys
) -> None:
    """The whole defect: `## Comments (25)` above ten of them is indistinguishable
    from `## Comments (10)` above ten of them."""
    whole = _run_gh_pr(monkeypatch, capsys, _comments(3))
    capped = _run_gh_pr(monkeypatch, capsys, _comments(25))
    assert "## Comments (3)" in whole
    assert "hidden" not in whole
    assert "## Comments (3)" not in capped
    # The word was "truncated" until #738 moved the cut from the head of the
    # thread to its middle; what is pinned is that a capped list is visibly
    # capped, not the verb used to say so.
    assert "hidden" in capped


def test_gh_pr_states_the_exact_number_of_comments_withheld(
    monkeypatch, capsys
) -> None:
    """'some comments are missing' is the defect, not the fix — 25 minus the ten
    shown is fifteen, and the reader is owed the fifteen."""
    out = _run_gh_pr(monkeypatch, capsys, _comments(25))
    assert ("## Comments (10 of 25 shown, 15 hidden from the middle — "
            "use :full to fetch all)") in out


def test_gh_pr_names_a_way_to_see_the_withheld_comments(monkeypatch, capsys) -> None:
    out = _run_gh_pr(monkeypatch, capsys, _comments(25))
    assert "use :full" in out.split("## Comments")[1]


def test_gh_pr_full_returns_every_comment_and_says_nothing(monkeypatch, capsys) -> None:
    """The escape hatch the disclosure names has to work — `gh-pr:N:full` existed
    for the description (#698) and never reached the comments."""
    out = _run_gh_pr(monkeypatch, capsys, _comments(25), "full")
    assert "## Comments (25)" in out
    assert "truncated" not in out
    for i in range(25):
        assert f"[{i}] comment body" in out


def test_gh_pr_keeps_both_ends_of_the_thread(monkeypatch, capsys) -> None:
    """This pinned the most-recent-ten selection so that changing it would show
    up as a failing test rather than a quietly different render. #738 changed it
    on purpose, and it did — updated here rather than deleted, because the
    guard is the point and the selection it guards is now head-plus-tail.
    Full reasoning and the selection tests are in
    `tests/test_comment_cap_both_ends_738.py`."""
    out = _run_gh_pr(monkeypatch, capsys, _comments(25))
    assert "[0] comment body" in out
    assert "[24] comment body" in out
    assert "[12] comment body" not in out


def test_gh_pr_marks_a_comment_cut_at_the_character_cap(monkeypatch, capsys) -> None:
    """#698's defect, one block below where #698 fixed it: each comment was
    sliced at COMMENT_MAX with no marker anywhere."""
    cap = gh_pr.COMMENT_MAX
    short = _run_gh_pr(monkeypatch, capsys, [
        {"author": {"login": "a"}, "body": "y" * (cap - 10), "createdAt": "2026-01-01"}])
    long = _run_gh_pr(monkeypatch, capsys, [
        {"author": {"login": "a"}, "body": "y" * (cap + 400), "createdAt": "2026-01-01"}])
    assert f"truncated at {cap} chars" not in short
    assert f"…[truncated at {cap} chars — use :full]" in long


def test_gh_pr_full_returns_whole_comment_bodies(monkeypatch, capsys) -> None:
    body = "y" * (gh_pr.COMMENT_MAX + 400)
    out = _run_gh_pr(monkeypatch, capsys, [
        {"author": {"login": "a"}, "body": body, "createdAt": "2026-01-01"}], "full")
    assert body in out
    assert "truncated" not in out


def test_gh_pr_zero_comments_still_prints_the_bare_header(monkeypatch, capsys) -> None:
    """An uncut list says nothing extra, so the absence of a marker is signal."""
    out = _run_gh_pr(monkeypatch, capsys, [])
    assert "## Comments (0)" in out


# ---------------------------------------------------------------------------
# gh-issue:N — the site that already had it, pinned so the shared move is proven
# ---------------------------------------------------------------------------

def _gh_issue_fakes(monkeypatch, comments: list[dict]) -> None:
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


def _run_gh_issue(monkeypatch, capsys, comments: list[dict], *flags: str) -> str:
    _gh_issue_fakes(monkeypatch, comments)
    monkeypatch.setattr(sys, "argv", ["issue.py", "12", *flags])
    assert gh_issue.main() == 0
    return capsys.readouterr().out


def test_the_two_github_ops_disclose_a_capped_comment_list_identically(
    monkeypatch, capsys
) -> None:
    """Two hand-maintained copies of a disclosure is how a third site forgets to
    have one — the argument `presets/_body.py` was created to settle. Compared on
    rendered output, not on the helper, because a helper both ops merely *import*
    proves nothing about what either one prints."""
    pr_out = _run_gh_pr(monkeypatch, capsys, _comments(25))
    issue_out = _run_gh_issue(monkeypatch, capsys, _comments(25))
    heading = ("## Comments (10 of 25 shown, 15 hidden from the middle — "
               "use :full to fetch all)")
    assert heading in pr_out
    assert heading in issue_out


def test_the_two_github_ops_mark_a_cut_comment_identically(
    monkeypatch, capsys
) -> None:
    """The *wording* is shared; the cap is not, and deliberately so — a PR render
    also carries checks, reviews, threads and a diff stat, so its comments get 500
    where an issue's get 1000, the same per-context split DESCRIPTION_MAX has."""
    cap_pr, cap_issue = gh_pr.COMMENT_MAX, gh_issue.COMMENT_MAX
    assert cap_pr != cap_issue
    pr_out = _run_gh_pr(monkeypatch, capsys, [
        {"author": {"login": "a"}, "body": "y" * (cap_pr + 400),
         "createdAt": "2026-01-01"}])
    issue_out = _run_gh_issue(monkeypatch, capsys, [
        {"author": {"login": "a"}, "body": "y" * (cap_issue + 400),
         "createdAt": "2026-01-01"}])
    assert f"…[truncated at {cap_pr} chars — use :full]" in pr_out
    assert f"…[truncated at {cap_issue} chars — use :full]" in issue_out


# ---------------------------------------------------------------------------
# gl-mr:N — the issue's second open question, checked rather than assumed
# ---------------------------------------------------------------------------

def _gl_mr_fakes(monkeypatch, notes: list[dict]) -> None:
    payload = json.dumps({
        "iid": 618, "title": "feat: thing", "state": "opened",
        "author": {"username": "max"}, "source_branch": "feat/x",
        "target_branch": "master", "labels": [], "milestone": None,
        "assignees": [], "reviewers": [], "draft": False,
        "merge_status": "can_be_merged", "has_conflicts": False,
        "head_pipeline": {"status": "success", "id": 1}, "merged_at": None,
        "merge_commit_sha": "", "web_url": "", "description": "short",
        "diff_stats": {"additions": 1, "deletions": 1},
        "created_at": "", "updated_at": "",
    })

    def fake_run(args, **kw):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(["glab"], 0, payload, "")

    def fake_api(endpoint, timeout=10):  # type: ignore[no-untyped-def]
        if "/notes" in endpoint:
            return subprocess.CompletedProcess(["glab"], 0, json.dumps(notes), "")
        payload_out = "{}" if endpoint.endswith("/approvals") else "[]"
        return subprocess.CompletedProcess(["glab"], 0, payload_out, "")

    monkeypatch.setattr(gl_mr.subprocess, "run", fake_run)
    monkeypatch.setattr(gl_mr, "_glab_api", fake_api)
    monkeypatch.setattr(gl_mr, "_local_branch_check", lambda branch: "")
    monkeypatch.setattr(gl_mr, "_get_conflicting_files", lambda s, t: [])


def _run_gl_mr(monkeypatch, capsys, notes: list[dict], *flags: str) -> str:
    _gl_mr_fakes(monkeypatch, notes)
    monkeypatch.setattr(sys, "argv", ["mr.py", "618", *flags])
    assert gl_mr.main() == 0
    return capsys.readouterr().out


def test_gl_mr_already_discloses_a_hidden_comment_count(monkeypatch, capsys) -> None:
    """Regression guard, not a fix: `gl-mr` budgets by bytes and prints an inline
    gap naming the hidden count, so the count-shaped half of #719 is not a defect
    here and must not be 'fixed' into a different convention."""
    notes = [{"author": {"username": f"u{i}"}, "body": "z" * 900,
              "created_at": "2026-01-01"} for i in range(40)]
    out = _run_gl_mr(monkeypatch, capsys, notes)
    assert "## Comments (40)" in out
    assert "more comment(s) hidden" in out
    assert "gl-mr:618:full" in out


def test_gl_mr_marks_a_note_cut_at_the_character_cap(monkeypatch, capsys) -> None:
    """`_render_note` sliced every note at COMMENT_MAX with no marker — #698's
    defect on a fifth site, found by the check #719 asked for."""
    cap = gl_mr.COMMENT_MAX
    short = _run_gl_mr(monkeypatch, capsys, [
        {"author": {"username": "a"}, "body": "y" * (cap - 10),
         "created_at": "2026-01-01"}])
    long = _run_gl_mr(monkeypatch, capsys, [
        {"author": {"username": "a"}, "body": "y" * (cap + 400),
         "created_at": "2026-01-01"}])
    assert f"truncated at {cap} chars" not in short
    assert f"…[truncated at {cap} chars — use :full]" in long


def test_gl_mr_full_returns_whole_note_bodies(monkeypatch, capsys) -> None:
    """`docs/presets/gitlab.md` promises `:full` "uncaps the file list and the
    comments". It uncapped how many comments printed, never how much of each."""
    body = "y" * (gl_mr.COMMENT_MAX + 400)
    out = _run_gl_mr(monkeypatch, capsys, [
        {"author": {"username": "a"}, "body": body, "created_at": "2026-01-01"}],
        "full")
    assert body in out
    assert "truncated" not in out


# ---------------------------------------------------------------------------
# the shape lives in one place
# ---------------------------------------------------------------------------

def test_the_comment_disclosure_has_one_home() -> None:
    assert hasattr(_body, "comments_heading")
    assert hasattr(_body, "comment_cut_notice")
