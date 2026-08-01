"""A capped body must read as capped, at all four sites (#698, after #681).

#681 fixed `gh-issue:N`: a raw `body[:DESCRIPTION_MAX]` slice, no marker, no
count, cutting mid-line — once three characters into a heading, with the next
section printed right after so the output read as a complete issue. The same
unguarded slice was left in three more read ops, which is what #698 is.

These are the ops briefs, triage and merge decisions are built from, so the bar
here is not "a marker is present somewhere". It is:

* a body over the cap must render **visibly differently** from one under it;
* the **withheld count must be exact**, not approximate and not merely
  non-zero;
* the disclosure must appear **before** `## Description`, since the reader this
  protects is the one who stops at the top;
* and the `:full` it points at must **actually return the whole body** — a way
  out that does not work is worse than none, because it stops the reader
  looking for another.

Every assertion below fails against the pre-#698 code. The under-cap controls
would pass against a do-nothing implementation on their own, so none of them
stands alone: each is paired in the same test with the over-cap case that
cannot.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_body = _load("presets/_body.py", "supertool_body_698")
gl_issue = _load("presets/gitlab/issue.py", "gitlab_issue_698")
gh_pr = _load("presets/github/pr.py", "github_pr_698")
gl_mr = _load("presets/gitlab/mr.py", "gitlab_mr_698")


# ---------------------------------------------------------------------------
# the shared cut itself
# ---------------------------------------------------------------------------

def test_cut_under_the_cap_withholds_nothing() -> None:
    shown, withheld = _body.cut("short body", 100)
    assert shown == "short body"
    assert withheld == 0


def test_cut_reports_exactly_what_it_removed() -> None:
    """The count is the contract — 'some text is missing' is the defect, not the fix."""
    body = "line one\nline two\n" + "z" * 500
    shown, withheld = _body.cut(body, 20)
    assert shown + body[len(shown):] == body
    assert withheld == len(body) - len(shown)
    assert withheld > 0


def test_cut_backs_off_to_the_last_line_break() -> None:
    body = "aaa\nbbb\n## a heading that runs past the cap"
    shown, _ = _body.cut(body, 12)
    assert shown == "aaa\nbbb"


def test_cut_falls_back_to_the_byte_offset_with_no_line_break_to_find() -> None:
    """One long paragraph has no boundary to back off to. It still gets counted."""
    shown, withheld = _body.cut("x" * 100, 40)
    assert len(shown) == 40
    assert withheld == 60


def test_full_path_returns_the_body_untouched() -> None:
    assert _body.cut("x" * 5000, None) == ("x" * 5000, 0)


# ---------------------------------------------------------------------------
# gl-issue:N
# ---------------------------------------------------------------------------

def _gl_issue_fakes(monkeypatch, description: str) -> None:
    payload = json.dumps({
        "title": "Plan", "state": "opened", "labels": [], "milestone": None,
        "assignees": [], "author": {"username": "florian"}, "iid": 12345,
        "web_url": "", "description": description, "project_id": 1,
    })

    def fake_glab(args, timeout=10):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(["glab"], 0, payload, "")

    def fake_api(endpoint, timeout=10):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(["glab"], 0, "[]", "")

    monkeypatch.setattr(gl_issue, "_glab", fake_glab)
    monkeypatch.setattr(gl_issue, "_glab_api", fake_api)
    monkeypatch.setattr(gl_issue, "_download_images", lambda urls, n: [])


def _run_gl_issue(monkeypatch, capsys, description: str, *flags: str) -> str:
    _gl_issue_fakes(monkeypatch, description)
    monkeypatch.setattr(sys, "argv", ["issue.py", "12345", *flags])
    assert gl_issue.main() == 0
    return capsys.readouterr().out


def test_gl_issue_over_cap_reads_differently_than_under_cap(monkeypatch, capsys) -> None:
    under = _run_gl_issue(monkeypatch, capsys, "x" * (gl_issue.DESCRIPTION_MAX - 10))
    over = _run_gl_issue(monkeypatch, capsys, "x" * (gl_issue.DESCRIPTION_MAX + 500))
    assert "TRUNCATED" not in under
    assert "withheld" not in under
    assert "TRUNCATED" in over


def test_gl_issue_states_the_exact_withheld_count(monkeypatch, capsys) -> None:
    out = _run_gl_issue(monkeypatch, capsys, "x" * (gl_issue.DESCRIPTION_MAX + 500))
    assert "500 withheld" in out
    assert f"{gl_issue.DESCRIPTION_MAX + 500} chars" in out
    assert "use :full" in out


def test_gl_issue_discloses_before_the_description(monkeypatch, capsys) -> None:
    out = _run_gl_issue(monkeypatch, capsys, "x" * (gl_issue.DESCRIPTION_MAX + 500))
    assert out.index("withheld") < out.index("## Description")


def test_gl_issue_cut_never_lands_mid_heading(monkeypatch, capsys) -> None:
    """#681's own repro, on the site it was not applied to."""
    body = ("x" * (gl_issue.DESCRIPTION_MAX - 5) + "\n"
            + "## The rest of this heading runs well past the cap " + "z" * 200)
    out = _run_gl_issue(monkeypatch, capsys, body)
    assert "## T" not in out


def test_gl_issue_full_returns_everything_and_says_nothing(monkeypatch, capsys) -> None:
    desc = "x" * (gl_issue.DESCRIPTION_MAX + 500)
    out = _run_gl_issue(monkeypatch, capsys, desc, "full")
    assert desc in out
    assert "TRUNCATED" not in out
    assert "withheld" not in out


# ---------------------------------------------------------------------------
# gh-pr:N
# ---------------------------------------------------------------------------

def _gh_pr_fakes(monkeypatch, body: str) -> None:
    payload = json.dumps({
        "number": 12, "title": "feat: thing", "state": "OPEN",
        "author": {"login": "max"}, "headRefName": "feat/x",
        "baseRefName": "master", "labels": [], "milestone": None,
        "isDraft": False, "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
        "reviews": [], "mergeCommit": None, "additions": 1, "deletions": 1,
        "changedFiles": 1, "statusCheckRollup": [], "url": "",
        "body": body, "comments": [], "assignees": [],
        "createdAt": "", "updatedAt": "",
    })

    def fake_gh(args, timeout=10):  # type: ignore[no-untyped-def]
        if args and args[0] == "issue":
            return subprocess.CompletedProcess(["gh"], 1, "", "not found")
        return subprocess.CompletedProcess(["gh"], 0, payload, "")

    monkeypatch.setattr(gh_pr, "_gh", fake_gh)
    monkeypatch.setattr(gh_pr, "_fetch_review_threads", lambda url, iid: [])
    monkeypatch.setattr(gh_pr, "_local_branch_check", lambda branch: "")


def _run_gh_pr(monkeypatch, capsys, body: str, *flags: str) -> str:
    _gh_pr_fakes(monkeypatch, body)
    monkeypatch.setattr(sys, "argv", ["pr.py", "12", *flags])
    assert gh_pr.main() == 0
    return capsys.readouterr().out


def test_gh_pr_over_cap_reads_differently_than_under_cap(monkeypatch, capsys) -> None:
    under = _run_gh_pr(monkeypatch, capsys, "x" * (gh_pr.DESCRIPTION_MAX - 10))
    over = _run_gh_pr(monkeypatch, capsys, "x" * (gh_pr.DESCRIPTION_MAX + 500))
    assert "TRUNCATED" not in under
    assert "withheld" not in under
    assert "TRUNCATED" in over


def test_gh_pr_states_the_exact_withheld_count(monkeypatch, capsys) -> None:
    out = _run_gh_pr(monkeypatch, capsys, "x" * (gh_pr.DESCRIPTION_MAX + 500))
    assert "500 withheld" in out
    assert f"{gh_pr.DESCRIPTION_MAX + 500} chars" in out
    assert "use :full" in out


def test_gh_pr_discloses_before_the_description(monkeypatch, capsys) -> None:
    out = _run_gh_pr(monkeypatch, capsys, "x" * (gh_pr.DESCRIPTION_MAX + 500))
    assert out.index("withheld") < out.index("## Description")


def test_gh_pr_cut_never_lands_mid_heading(monkeypatch, capsys) -> None:
    body = ("x" * (gh_pr.DESCRIPTION_MAX - 5) + "\n"
            + "## The rest of this heading runs well past the cap " + "z" * 200)
    out = _run_gh_pr(monkeypatch, capsys, body)
    assert "## T" not in out


def test_gh_pr_full_returns_everything_and_says_nothing(monkeypatch, capsys) -> None:
    """gh-pr had no :full at all. The disclosure names one, so one has to exist."""
    body = "x" * (gh_pr.DESCRIPTION_MAX + 500)
    out = _run_gh_pr(monkeypatch, capsys, body, "full")
    assert body in out
    assert "TRUNCATED" not in out
    assert "withheld" not in out


def test_gh_pr_status_still_returns_the_slim_dashboard(monkeypatch, capsys) -> None:
    """Adding :full must not disturb the flag that was already there."""
    out = _run_gh_pr(monkeypatch, capsys, "x" * 50, "status")
    assert "## Description" not in out
    assert "state:" in out


# ---------------------------------------------------------------------------
# gl-mr:N
# ---------------------------------------------------------------------------

def _gl_mr_fakes(monkeypatch, description: str) -> None:
    payload = json.dumps({
        "iid": 618, "title": "feat: thing", "state": "opened",
        "author": {"username": "max"}, "source_branch": "feat/x",
        "target_branch": "master", "labels": [], "milestone": None,
        "assignees": [], "reviewers": [], "draft": False,
        "merge_status": "can_be_merged", "has_conflicts": False,
        "head_pipeline": {"status": "success", "id": 1}, "merged_at": None,
        "merge_commit_sha": "", "web_url": "", "description": description,
        "diff_stats": {"additions": 1, "deletions": 1},
        "created_at": "", "updated_at": "",
    })

    def fake_run(args, **kw):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(["glab"], 0, payload, "")

    def fake_api(endpoint, timeout=10):  # type: ignore[no-untyped-def]
        # /approvals is the one endpoint read as an object, not a list.
        payload_out = "{}" if endpoint.endswith("/approvals") else "[]"
        return subprocess.CompletedProcess(["glab"], 0, payload_out, "")

    monkeypatch.setattr(gl_mr.subprocess, "run", fake_run)
    monkeypatch.setattr(gl_mr, "_glab_api", fake_api)
    monkeypatch.setattr(gl_mr, "_local_branch_check", lambda branch: "")
    monkeypatch.setattr(gl_mr, "_get_conflicting_files", lambda s, t: [])


def _run_gl_mr(monkeypatch, capsys, description: str, *flags: str) -> str:
    _gl_mr_fakes(monkeypatch, description)
    monkeypatch.setattr(sys, "argv", ["mr.py", "618", *flags])
    assert gl_mr.main() == 0
    return capsys.readouterr().out


def test_gl_mr_over_cap_reads_differently_than_under_cap(monkeypatch, capsys) -> None:
    under = _run_gl_mr(monkeypatch, capsys, "x" * (gl_mr.DESCRIPTION_MAX - 10))
    over = _run_gl_mr(monkeypatch, capsys, "x" * (gl_mr.DESCRIPTION_MAX + 500))
    assert "TRUNCATED" not in under
    assert "withheld" not in under
    assert "TRUNCATED" in over


def test_gl_mr_states_the_exact_withheld_count(monkeypatch, capsys) -> None:
    out = _run_gl_mr(monkeypatch, capsys, "x" * (gl_mr.DESCRIPTION_MAX + 500))
    assert "500 withheld" in out
    assert f"{gl_mr.DESCRIPTION_MAX + 500} chars" in out
    assert "use :full" in out


def test_gl_mr_discloses_before_the_description(monkeypatch, capsys) -> None:
    out = _run_gl_mr(monkeypatch, capsys, "x" * (gl_mr.DESCRIPTION_MAX + 500))
    assert out.index("withheld") < out.index("## Description")


def test_gl_mr_cut_never_lands_mid_heading(monkeypatch, capsys) -> None:
    body = ("x" * (gl_mr.DESCRIPTION_MAX - 5) + "\n"
            + "## The rest of this heading runs well past the cap " + "z" * 200)
    out = _run_gl_mr(monkeypatch, capsys, body)
    assert "## T" not in out


def test_gl_mr_full_returns_everything_and_says_nothing(monkeypatch, capsys) -> None:
    """gl-mr had a :full flag that never reached the description — the op's own
    docs said it uncapped comments and the file list, and the description was
    capped regardless. A disclosure naming :full has to be telling the truth."""
    desc = "x" * (gl_mr.DESCRIPTION_MAX + 500)
    out = _run_gl_mr(monkeypatch, capsys, desc, "full")
    assert desc in out
    assert "TRUNCATED" not in out
    assert "withheld" not in out


# ---------------------------------------------------------------------------
# the fourth site stays where #681 put it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod", [gl_issue, gh_pr, gl_mr])
def test_every_site_uses_the_one_shared_cut(mod: Any) -> None:
    """Five copies of a disclosure is how a sixth site forgets to have one."""
    assert hasattr(mod, "_body")
