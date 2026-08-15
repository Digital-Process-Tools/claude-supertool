"""`gl-mr` tallied one capped page and printed the number as a total (#1517).

#1491's defect verbatim on the GitLab side. `_fetch_array` does not paginate, so
every caller that counts its return is reporting a floor as a total — and GitLab
returns discussions oldest-first, so on a busy MR the newest and least-resolved
threads are on the page that was never fetched. `Unresolved threads: 0 / 12` on a
merge blocker is the expensive spelling of it.

The issue names three sites and there is a fourth, in the same file and the same
render: `## Comments (N)`, off `per_page=50`. Its own comment says why it counts
— "a wrong number is indistinguishable from a right one" — which is the argument
for this fix, one endpoint over.

**Where the fact lives.** Not inside `_fetch_array`: it is shared with callers
that do not tally (`pipelines?per_page=1` wants exactly one row), and paginating
there would buy them round-trips nobody asked for. `_fetch_tally` wraps it for
the callers that count, and reads the cap back off the endpoint string it was
handed — so the render's inference cannot drift from what was asked, which is
#1505's rule on the GitHub side.

**A number that was not capped must still print as exact.** Hedging every tally
is the failure mode #1505's reviewer raised and had argued down, so every
assertion below has a not-capped twin.

`glab` is unauthenticated in the agent sandbox, so the live half is stubbed here.
Against a real project the call is:

    glab api "projects/:id/merge_requests/IID/discussions?per_page=100"

on an MR with more than 100 discussion threads, and the check is that the render
says `>=` rather than a bare number.
"""
from __future__ import annotations

import importlib.util
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


mr = _load("presets/gitlab/mr.py", "gitlab_mr_1517")


def _thread(resolved: bool) -> dict:
    return {"notes": [{"resolvable": True, "resolved": resolved}]}


def _stub_array(monkeypatch, rows: list) -> None:
    monkeypatch.setattr(mr, "_fetch_array", lambda *a, **k: (rows, None))


# ---------------------------------------------------------------------------
# The cap is read back off the endpoint that was sent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint,cap", [
    ("projects/:id/merge_requests/7/discussions?per_page=100", 100),
    ("projects/:id/merge_requests/7/notes?per_page=50&sort=asc", 50),
    ("projects/:id/pipelines/3/jobs?per_page=100&scope=failed", 100),
    ("projects/:id/merge_requests/7/pipelines?per_page=1", 1),
    ("projects/:id/merge_requests/7/discussions", None),
])
def test_the_cap_comes_from_the_endpoint_not_from_a_constant(endpoint, cap) -> None:
    assert mr._page_cap(endpoint) == cap


def test_a_full_page_is_reported_capped_and_a_short_one_is_not(monkeypatch) -> None:
    endpoint = "projects/:id/merge_requests/7/discussions?per_page=3"
    _stub_array(monkeypatch, [{}, {}, {}])
    rows, reason, capped = mr._fetch_tally(endpoint, "discussions")
    assert reason is None and len(rows) == 3
    assert capped is True

    _stub_array(monkeypatch, [{}, {}])
    _rows, _reason, capped = mr._fetch_tally(endpoint, "discussions")
    assert capped is False


def test_a_failed_fetch_is_not_a_capped_page(monkeypatch) -> None:
    monkeypatch.setattr(mr, "_fetch_array", lambda *a, **k: (None, "boom"))
    rows, reason, capped = mr._fetch_tally("x?per_page=100", "discussions")
    assert rows is None and reason == "boom" and capped is False


# ---------------------------------------------------------------------------
# Site 1 — unresolved threads, the merge blocker
# ---------------------------------------------------------------------------

def test_a_thread_tally_off_a_full_page_is_a_floor() -> None:
    lines = mr._unresolved_thread_lines([_thread(False), _thread(True)], True)
    assert lines[0] == "Unresolved threads: >=1 / >=2", lines
    assert any("PAGE FULL" in line for line in lines), lines


def test_a_thread_tally_off_a_short_page_is_exact() -> None:
    lines = mr._unresolved_thread_lines([_thread(False), _thread(True)], False)
    assert lines[0] == "Unresolved threads: 1 / 2", lines
    assert not any("PAGE FULL" in line for line in lines), lines


# ---------------------------------------------------------------------------
# Site 2 — the non-passing jobs block on the :status render
# ---------------------------------------------------------------------------

def test_none_non_passing_off_a_full_page_is_not_a_clean_pipeline(monkeypatch) -> None:
    monkeypatch.setattr(
        mr, "_fetch_tally",
        lambda *a, **k: ([{"id": i, "name": "t", "status": "success"}
                          for i in range(100)], None, True))
    lines = mr._pipeline_leg_lines(42)
    assert any("none non-passing" in line for line in lines), lines
    assert any("PAGE FULL" in line for line in lines), lines
    # #1607 put a tally above that line. It is the same floor and must carry
    # the same hedge — "100 total: 100 passed" off a full page is the exact
    # sentence this file exists to stop being read as a total.
    assert any(line.strip().startswith("legs: 100 total:") for line in lines), lines


def test_none_non_passing_off_a_short_page_says_so_plainly(monkeypatch) -> None:
    monkeypatch.setattr(
        mr, "_fetch_tally",
        lambda *a, **k: ([{"id": 1, "name": "t", "status": "success"}],
                         None, False))
    lines = mr._pipeline_leg_lines(42)
    assert any("none non-passing" in line for line in lines), lines
    assert not any("PAGE FULL" in line for line in lines), lines
    # The twin: an uncapped tally prints as exact, or the hedge above stops
    # meaning anything.
    assert any(line.strip() == "legs: 1 total: 1 passed, 0 failed, 0 pending"
               for line in lines), lines


# ---------------------------------------------------------------------------
# Site 3 — the failed-jobs block
# ---------------------------------------------------------------------------

def test_a_failed_job_count_off_a_full_page_is_a_floor() -> None:
    jobs = [{"id": i, "name": "n", "stage": "s"} for i in range(100)]
    lines = mr._failed_jobs_block(jobs, True)
    assert lines[0] == "Failed jobs (>=100):", lines
    assert any("PAGE FULL" in line for line in lines), lines


def test_a_failed_job_count_off_a_short_page_is_exact() -> None:
    lines = mr._failed_jobs_block([{"id": 1, "name": "n", "stage": "s"}], False)
    assert lines[0] == "Failed jobs (1):", lines
    assert not any("PAGE FULL" in line for line in lines), lines


def test_no_failed_job_on_a_failed_pipeline_still_says_so() -> None:
    lines = mr._failed_jobs_block([], False)
    assert "Failed jobs: none" in lines[0], lines


# ---------------------------------------------------------------------------
# Site 4 — the comment count, not named by the issue
# ---------------------------------------------------------------------------

def test_a_comment_count_off_a_full_page_is_a_floor() -> None:
    assert mr._floor(50, True) == ">=50"
    assert mr._floor(50, False) == "50"
