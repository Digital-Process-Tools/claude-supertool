"""#619 — the check tally answers "how many", never "which".

`gh-pr:N:status` (and the full `gh-pr:N`) print `checks: 14 total: 1 passed, 5
failed, 8 pending`, which is exactly the arithmetic #454 was filed to get —
and exactly one call short of the next question: which five, and what do I
run next on each? Filed after falling back to `gh pr checks | jq` and then
`gh api .../actions/jobs/<id>/logs`, three calls where the op could plausibly
have been one.

These tests pin three things a green suite would happily pass without:

* the failing (and any not-`SUCCESS`-not-pending) legs are *named*, not just
  counted — a state folded into a bare count is #445/#454's defect class
* a job id rides along per named leg, parsed from `detailsUrl`, so
  `gh-job:<id>:fail` is reachable with no `gh api` detour
* naming is bounded — `+N more`, this repo's established disclosure
  vocabulary (#605) — so a wide matrix cannot blow the output budget
* `CANCELLED` / `SKIPPED` never get counted as passed or as pending; they
  get their own named line, because that fold is the exact house defect this
  whole issue is about

A test asserting only "some extra text appears when not green" would pass on
a version that dumps every leg unbounded, or one that silently drops
`CANCELLED`. Each assertion below fails on one specific version of "did
almost nothing."
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


def _load(name: str, rel: str):
    path = _ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


checks = _load("checks_619", "presets/_checks.py")
pr = _load("github_pr_619", "presets/github/pr.py")
mr = _load("gitlab_mr_619", "presets/gitlab/mr.py")


# ---------------------------------------------------------------------------
# presets/_checks.py — the shared classifier, pure, no gh/glab
# ---------------------------------------------------------------------------

def test_github_job_id_parses_details_url() -> None:
    check = {"detailsUrl": "https://github.com/o/r/actions/runs/1/job/91015853871"}
    assert checks.github_job_id(check) == "91015853871"


def test_github_job_id_empty_when_url_has_no_job_segment() -> None:
    # Legacy commit statuses point at an external system, not an Actions job.
    check = {"detailsUrl": "https://ci.example.com/build/42"}
    assert checks.github_job_id(check) == ""


def test_github_job_id_empty_when_no_details_url() -> None:
    assert checks.github_job_id({}) == ""
    assert checks.github_job_id("not a dict") == ""  # type: ignore[arg-type]


def test_named_disclosure_empty_when_all_green() -> None:
    """The core terseness guarantee: an all-passed rollup adds zero lines."""
    entries = [("pytest (ubuntu, 3.9)", "SUCCESS", "job", "1"),
               ("pytest (ubuntu, 3.10)", "SUCCESS", "job", "2")]
    assert checks.named_disclosure(entries) == []


def test_named_disclosure_names_failed_legs_with_job_ids() -> None:
    entries = [
        ("pytest (ubuntu-latest, 3.9)", "FAILURE", "job", "111"),
        ("pytest (ubuntu-latest, 3.10)", "FAILURE", "job", "222"),
        ("pytest (macos-latest, 3.12)", "SUCCESS", "job", "333"),
    ]
    lines = checks.named_disclosure(entries)
    assert len(lines) == 1
    assert lines[0].startswith("  failed: ")
    assert "pytest (ubuntu-latest, 3.9) (job #111)" in lines[0]
    assert "pytest (ubuntu-latest, 3.10) (job #222)" in lines[0]
    assert "macos-latest" not in lines[0]  # the passing leg is not named


def test_named_disclosure_skips_pending_legs_entirely() -> None:
    """Pending resolves itself — naming it is noise, not signal (judgment call)."""
    entries = [
        ("pytest (ubuntu-latest, 3.9)", "FAILURE", "job", "1"),
        ("notifiers (bun)", "IN_PROGRESS", "", ""),
        ("notifiers (deno)", "QUEUED", "", ""),
    ]
    lines = checks.named_disclosure(entries)
    text = "\n".join(lines)
    assert "notifiers" not in text
    assert "pending" not in text


def test_named_disclosure_never_folds_cancelled_into_a_count() -> None:
    """#445/#454's defect class, named — CANCELLED gets its own line."""
    entries = [
        ("deploy-preview", "CANCELLED", "job", "9"),
        ("pytest (ubuntu, 3.9)", "SUCCESS", "job", "1"),
    ]
    lines = checks.named_disclosure(entries)
    assert any(l.startswith("  cancelled: deploy-preview (job #9)") for l in lines)


def test_named_disclosure_skipped_and_failed_get_separate_lines() -> None:
    entries = [
        ("job-a", "SKIPPED", "", ""),
        ("job-b", "FAILURE", "job", "5"),
    ]
    lines = checks.named_disclosure(entries)
    assert len(lines) == 2
    labels = {l.split(":", 1)[0].strip() for l in lines}
    assert labels == {"skipped", "failed"}


def test_named_disclosure_bounds_with_plus_n_more() -> None:
    entries = [(f"pytest (ubuntu, 3.{n})", "FAILURE", "job", str(n))
               for n in range(9, 15)]
    lines = checks.named_disclosure(entries, cap=5)
    assert len(lines) == 1
    assert lines[0].count("job #") == 5
    assert "+1 more" in lines[0]


def test_named_disclosure_no_plus_more_when_under_cap() -> None:
    entries = [("only-one", "FAILURE", "job", "1")]
    lines = checks.named_disclosure(entries, cap=5)
    assert "more" not in lines[0]


# ---------------------------------------------------------------------------
# gh-pr:N:status (slim) — the terse, repeatedly-polled form
# ---------------------------------------------------------------------------

def _fake_gh_run(stdout: str, returncode: int = 0) -> Any:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr="")


def _pr_payload(**overrides: Any) -> str:
    base = {
        "number": 617,
        "title": "feat: name failing legs",
        "state": "OPEN",
        "author": {"login": "max"},
        "headRefName": "feat/619",
        "baseRefName": "master",
        "labels": [],
        "milestone": None,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "REVIEW_REQUIRED",
        "reviews": [],
        "mergeCommit": {},
        "additions": 10,
        "deletions": 2,
        "changedFiles": 2,
        "statusCheckRollup": [],
        "url": "https://github.com/o/r/pull/617",
        "body": "",
        "comments": [],
    }
    base.update(overrides)
    return json.dumps(base)


def _mixed_rollup() -> list[dict]:
    rollup = [
        {"name": "pytest (ubuntu-latest, 3.9)", "conclusion": "SUCCESS",
         "status": "COMPLETED", "detailsUrl": "https://github.com/o/r/actions/runs/1/job/1"},
    ]
    for n in range(10, 15):
        rollup.append({
            "name": f"pytest (ubuntu-latest, 3.{n})", "conclusion": "FAILURE",
            "status": "COMPLETED",
            "detailsUrl": f"https://github.com/o/r/actions/runs/1/job/{n}",
        })
    rollup.append({"name": "notifiers (bun)", "status": "IN_PROGRESS",
                    "detailsUrl": ""})
    return rollup


def test_slim_status_names_failed_legs_with_job_ids(monkeypatch, capsys) -> None:
    payload = _pr_payload(statusCheckRollup=_mixed_rollup())
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **kw: _fake_gh_run(payload))
    monkeypatch.setattr(sys, "argv", ["pr.py", "617", "status"])
    rc = pr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "checks: 7 total: 1 passed, 5 failed, 1 pending" in out
    assert "  failed: " in out
    assert "job #10" in out
    assert "notifiers" not in out  # pending leg, deliberately not named


def test_slim_status_bounds_failed_names_with_plus_more(monkeypatch, capsys) -> None:
    rollup = [{"name": f"pytest (ubuntu, 3.{n})", "conclusion": "FAILURE",
               "status": "COMPLETED",
               "detailsUrl": f"https://github.com/o/r/actions/runs/1/job/{n}"}
              for n in range(9, 15)]
    payload = _pr_payload(statusCheckRollup=rollup)
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **kw: _fake_gh_run(payload))
    monkeypatch.setattr(sys, "argv", ["pr.py", "617", "status"])
    pr.main()
    out = capsys.readouterr().out
    assert "+1 more" in out


def test_slim_status_all_green_adds_no_extra_lines(monkeypatch, capsys) -> None:
    """The terseness guarantee end to end, not just in the helper.

    The leg count is stubbed as reconciled on purpose (#724). Left unstubbed,
    the fixture's `gh` double answers the Actions jobs call with a PR payload,
    the tally declines as UNVERIFIED, and this test measures the length of a
    decline notice instead of the terseness it claims to pin.
    """
    rollup = [{"name": "pytest (ubuntu, 3.9)", "conclusion": "SUCCESS",
               "status": "COMPLETED", "detailsUrl": "https://github.com/o/r/actions/runs/1/job/1"}]
    payload = _pr_payload(statusCheckRollup=rollup)
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **kw: _fake_gh_run(payload))
    monkeypatch.setattr(pr, "_declared_for_commit",
                        lambda *a, **kw: (1, ["pytest (ubuntu, 3.9)"], [], ""))
    monkeypatch.setattr(sys, "argv", ["pr.py", "617", "status"])
    pr.main()
    out = capsys.readouterr().out
    assert "failed:" not in out
    assert "⚠" not in out
    assert len(out) < 500


def test_slim_status_names_cancelled_leg_not_folded(monkeypatch, capsys) -> None:
    rollup = [
        {"name": "pytest (ubuntu, 3.9)", "conclusion": "SUCCESS", "status": "COMPLETED",
         "detailsUrl": "https://github.com/o/r/actions/runs/1/job/1"},
        {"name": "deploy-preview", "conclusion": "CANCELLED", "status": "COMPLETED",
         "detailsUrl": "https://github.com/o/r/actions/runs/1/job/2"},
    ]
    payload = _pr_payload(statusCheckRollup=rollup)
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **kw: _fake_gh_run(payload))
    monkeypatch.setattr(sys, "argv", ["pr.py", "617", "status"])
    pr.main()
    out = capsys.readouterr().out
    assert "cancelled: deploy-preview (job #2)" in out


def test_full_dashboard_also_names_failed_legs(monkeypatch, capsys) -> None:
    """Judgment call: naming lives in both the terse and full forms (#619)."""
    payload = _pr_payload(statusCheckRollup=_mixed_rollup(), body="x", comments=[])
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **kw: _fake_gh_run(payload))
    monkeypatch.setattr(pr, "_fetch_review_threads", lambda *a, **kw: [])
    monkeypatch.setattr(sys, "argv", ["pr.py", "617"])
    rc = pr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Checks: 7 total: 1 passed, 5 failed, 1 pending" in out
    assert "  failed: " in out
    assert "job #10" in out


# ---------------------------------------------------------------------------
# gl-mr:N:status — same gap, different vocabulary (glab pipeline jobs)
# ---------------------------------------------------------------------------

def _fake_gl_run(stdout: str, returncode: int = 0) -> Any:
    return subprocess.CompletedProcess(args=["glab"], returncode=returncode, stdout=stdout, stderr="")


def _mr_payload(**overrides: Any) -> str:
    base = {
        "iid": 618,
        "state": "opened",
        "merge_status": "can_be_merged",
        "has_conflicts": False,
        "source_branch": "feat/619",
        "target_branch": "master",
        "head_pipeline": {"status": "failed", "id": 5001},
        "merged_at": None,
        "merge_commit_sha": "",
        "web_url": "https://gitlab.example/o/r/-/merge_requests/618",
    }
    base.update(overrides)
    return json.dumps(base)


def test_gl_slim_status_names_failed_and_cancelled_jobs(monkeypatch, capsys) -> None:
    mr_json = _mr_payload()
    jobs_json = json.dumps([
        {"id": 1, "name": "pytest (3.9)", "status": "success"},
        {"id": 2, "name": "pytest (3.10)", "status": "failed"},
        {"id": 3, "name": "deploy", "status": "canceled"},
        {"id": 4, "name": "docs", "status": "running"},
    ])

    def fake_run(args, **kw):  # type: ignore[no-untyped-def]
        if args[:2] == ["glab", "mr"]:
            return _fake_gl_run(mr_json)
        return _fake_gl_run("[]")  # any bare `glab api` call not stubbed below

    def fake_api(endpoint, timeout=10):  # type: ignore[no-untyped-def]
        if "pipelines" in endpoint and "jobs" in endpoint:
            return _fake_gl_run(jobs_json)
        return _fake_gl_run("[]")

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    monkeypatch.setattr(mr, "_glab_api", fake_api)
    monkeypatch.setattr(sys, "argv", ["mr.py", "618", "status"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "pipeline: failed" in out
    assert "failed: pytest (3.10) (job #2)" in out
    assert "canceled: deploy (job #3)" in out  # GitLab spells it with one L
    assert "docs" not in out  # running — resolves itself, not named


def test_gl_slim_status_all_green_skips_jobs_fetch_entirely(monkeypatch, capsys) -> None:
    """Cost discipline: the extra API call is bought only when it might matter."""
    mr_json = _mr_payload(head_pipeline={"status": "success", "id": 5002})
    calls: list[str] = []

    def fake_run(args, **kw):  # type: ignore[no-untyped-def]
        return _fake_gl_run(mr_json)

    def fake_api(endpoint, timeout=10):  # type: ignore[no-untyped-def]
        calls.append(endpoint)
        return _fake_gl_run("[]")

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    monkeypatch.setattr(mr, "_glab_api", fake_api)
    monkeypatch.setattr(sys, "argv", ["mr.py", "618", "status"])
    mr.main()
    out = capsys.readouterr().out
    assert "failed:" not in out
    assert not any("jobs" in c for c in calls)
