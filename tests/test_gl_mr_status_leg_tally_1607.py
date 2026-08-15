"""#1607 item 1 — `gl-mr:N:status` renders one word where `gh-pr` sums legs.

`pipeline: success` is the GitLab spelling of the defect `presets/_checks.py`
was written for: a pipeline whose jobs are half `skipped`, `manual` or
`canceled` renders byte-identically to one where every leg ran and passed.
`CANCELED`/`SKIPPED`/`MANUAL` are neither passes nor pendings, and today none of
them reach the render at all on a green pipeline, because the jobs endpoint is
only asked when the pipeline status is already not-green.

**The judgement is not duplicated.** #958's rule, restated by #1607: the render
may legitimately differ per platform, the *classification* may not. So the tally
is `_checks.summarize()` — the same function `gh-pr:N:status` sums its rollup
with — fed GitLab's own job status tokens. GitLab's `canceled` (one L) is not in
any of that module's four state sets, so it falls to the leftover term and is
**named** rather than dropped, which is exactly the property `summarize()`'s
docstring promises: every term after `N total` sums back to N.

**Round trip, stated not smuggled.** Counting legs costs one `glab api` call to
`projects/:id/pipelines/ID/jobs`. That call was already being made on every
not-green pipeline; this change makes it unconditional whenever the MR has a
pipeline at all, so a green poll now pays it too. #815 forbids buying a per-call
round trip *silently*, not buying one — and the in-repo precedent for paying it
is `presets/github/pr.py`'s `_declared_for_commit`, which fires 1+N requests on
every `gh-pr:N:status`, green ones included, on the argument that "a request is
cheaper than a merge on four green CodeQL legs" (#804). A tally that skips the
green pipeline cannot see the case it exists for, because a green pipeline with
four skipped legs is the case it exists for.

Every must-not-appear assertion below is paired with a must-fire twin in the
same fixture: a harness that stopped rendering anything at all would otherwise
satisfy the negative half in silence.

`glab` is unauthenticated in the agent sandbox, so the live half is stubbed.
Against a real project the calls are:

    glab api "projects/:id/merge_requests/IID/pipelines?per_page=1"
    glab api "projects/:id/pipelines/PIPELINE_ID/jobs?per_page=100"

on an MR whose pipeline succeeded with at least one `skipped` or `manual` job,
and the check is that `legs:` names it.
"""
from __future__ import annotations

import importlib.util
import json
import re
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


mr = _load("presets/gitlab/mr.py", "gitlab_mr_1607")
checks = _load("presets/_checks.py", "checks_1607")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _job(status: str, name: str = "leg", jid: int = 1) -> dict:
    return {"status": status, "name": name, "id": jid}


def _jobs(*statuses: str) -> list[dict]:
    return [_job(s, f"leg{i}", i + 1) for i, s in enumerate(statuses)]


def _stub_jobs(monkeypatch, rows: list, *, reason=None, capped: bool = False):
    """Stand in for the one `glab api .../jobs` round trip, and count it."""
    calls: list[str] = []

    def fake(endpoint: str, noun: str, *a, **k):
        calls.append(endpoint)
        return (None, reason, False) if reason is not None else (rows, None, capped)

    monkeypatch.setattr(mr, "_fetch_tally", fake)
    return calls


def _tally_line(lines: list[str]) -> str:
    found = [ln for ln in lines if ln.strip().startswith("legs:")]
    assert len(found) == 1, f"expected exactly one legs line, got {lines}"
    return found[0].strip()


_TERM = re.compile(r"(\d+) ([a-z_;]+)")


def _terms(tally: str) -> tuple[int, dict[str, int]]:
    """`N total: a passed, b failed, ...` parsed back into numbers."""
    head, _, rest = tally.partition("total:")
    total = int(head.split()[-1])
    rest = rest.split(chr(0x26A0))[0]
    return total, {name: int(n) for n, name in _TERM.findall(rest)}


# ---------------------------------------------------------------------------
# The defect: a green pipeline that hid four legs
# ---------------------------------------------------------------------------

def test_a_green_pipeline_with_skipped_legs_stops_reading_as_all_green(
        monkeypatch) -> None:
    _stub_jobs(monkeypatch, _jobs(*(["success"] * 8), "skipped", "skipped",
                                  "manual", "canceled"))
    tally = _tally_line(mr._pipeline_leg_lines(4242))
    assert tally.startswith("legs: 12 total: 8 passed, 0 failed, 0 pending"), tally
    assert "2 skipped" in tally and "1 manual" in tally and "1 canceled" in tally
    assert checks.NOT_GREEN in tally, tally


def test_an_all_success_pipeline_still_says_so(monkeypatch) -> None:
    """The must-fire twin: the line above must not be an artefact of failure."""
    _stub_jobs(monkeypatch, _jobs("success", "success", "success"))
    tally = _tally_line(mr._pipeline_leg_lines(4242))
    assert tally == "legs: 3 total: 3 passed, 0 failed, 0 pending", tally
    assert checks.NOT_GREEN not in tally


# ---------------------------------------------------------------------------
# The arithmetic: every term sums back to the leg count
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("statuses", [
    ("success", "failed", "running", "canceled", "skipped", "manual"),
    ("success",) * 5 + ("waiting_for_resource",),
    ("canceled",) * 3,
    ("success", "created", "scheduled", "preparing", "pending"),
])
def test_the_terms_sum_to_the_leg_count(monkeypatch, statuses) -> None:
    _stub_jobs(monkeypatch, _jobs(*statuses))
    total, terms = _terms(_tally_line(mr._pipeline_leg_lines(4242)))
    assert total == len(statuses)
    assert sum(terms.values()) == total, (statuses, terms)


def test_a_status_this_tool_has_never_heard_of_gets_its_own_term(
        monkeypatch) -> None:
    """Enumerating states trails whatever GitLab adds next; the sum cannot."""
    _stub_jobs(monkeypatch, _jobs("success", "quantum_entangled"))
    tally = _tally_line(mr._pipeline_leg_lines(4242))
    assert "1 quantum_entangled" in tally, tally
    total, terms = _terms(tally)
    assert total == 2 and sum(terms.values()) == 2


def test_an_unreadable_job_element_is_still_counted_as_a_leg(
        monkeypatch) -> None:
    """`_dict_elements` drops it; dropping it from the tally is the defect."""
    _stub_jobs(monkeypatch, [_job("success"), "not-an-object", _job("failed")])
    tally = _tally_line(mr._pipeline_leg_lines(4242))
    total, terms = _terms(tally)
    assert total == 3, tally
    assert terms.get("unknown") == 1, tally
    assert sum(terms.values()) == 3


# ---------------------------------------------------------------------------
# The third state: could not look is not the same as nothing to report
# ---------------------------------------------------------------------------

def test_a_declined_jobs_fetch_says_unknown_and_never_counts_to_zero(
        monkeypatch) -> None:
    _stub_jobs(monkeypatch, [], reason="glab timed out looking up jobs")
    lines = mr._pipeline_leg_lines(4242)
    tally = _tally_line(lines)
    assert tally.startswith("legs: UNKNOWN — glab timed out"), tally
    # The failure this exists to prevent: a zeroed tally reads as "all
    # accounted for, nothing outstanding" over a read nobody completed.
    assert "total:" not in tally, tally
    assert "0 passed" not in tally, tally


def test_an_empty_but_readable_jobs_list_is_not_unknown(monkeypatch) -> None:
    """The must-fire twin of the decline: a real zero must not say UNKNOWN."""
    _stub_jobs(monkeypatch, [])
    tally = _tally_line(mr._pipeline_leg_lines(4242))
    assert "UNKNOWN" not in tally, tally
    assert tally == f"legs: {mr._NO_JOBS}", tally
    # Not `_checks.NO_CHECKS`: its words are "no check runs on this commit",
    # which is the GitHub unit and the GitHub anchor. The classifier is shared;
    # a sentence about a different object is not.
    assert "commit" not in tally, tally


def test_a_full_page_discloses_the_tally_as_a_lower_bound(monkeypatch) -> None:
    _stub_jobs(monkeypatch, _jobs(*(["success"] * 100)), capped=True)
    lines = mr._pipeline_leg_lines(4242)
    assert any("PAGE FULL" in ln for ln in lines), lines


def test_a_short_page_is_not_hedged(monkeypatch) -> None:
    """The twin: hedging every tally is how a hedge stops meaning anything."""
    _stub_jobs(monkeypatch, _jobs(*(["success"] * 7)), capped=False)
    lines = mr._pipeline_leg_lines(4242)
    assert not any("PAGE FULL" in ln for ln in lines), lines


# ---------------------------------------------------------------------------
# One classifier, not two (#958)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("statuses", [
    ("success", "failed", "canceled"),
    ("skipped", "manual", "running", "success"),
    ("success",) * 4,
])
def test_the_tally_is_the_shared_classifier_verbatim(monkeypatch,
                                                     statuses) -> None:
    """If this drifts, a second GitLab-only judgement has been written."""
    _stub_jobs(monkeypatch, _jobs(*statuses))
    tally = _tally_line(mr._pipeline_leg_lines(4242))
    assert tally == f"legs: {checks.summarize(statuses)}", tally


# ---------------------------------------------------------------------------
# The round trip: bought once, and only when there is a pipeline
# ---------------------------------------------------------------------------

def test_no_pipeline_means_no_jobs_request(monkeypatch) -> None:
    calls = _stub_jobs(monkeypatch, _jobs("success"))
    assert mr._pipeline_leg_lines("") == []
    assert calls == [], calls


def test_a_pipeline_buys_exactly_one_jobs_request(monkeypatch) -> None:
    """The must-fire twin: the empty call list above is not the harness."""
    calls = _stub_jobs(monkeypatch, _jobs("success"))
    assert mr._pipeline_leg_lines(4242) != []
    assert len(calls) == 1, calls
    assert "pipelines/4242/jobs" in calls[0], calls


# ---------------------------------------------------------------------------
# End to end through the `:status` render
# ---------------------------------------------------------------------------

def _render(monkeypatch, capsys, *, pipe_status: str, jobs: list) -> str:
    payload = json.dumps({
        "iid": 1607, "state": "opened", "merge_status": "can_be_merged",
        "has_conflicts": False, "source_branch": "fix/1607",
        "target_branch": "master", "web_url": "https://gl.example/-/mr/1607",
    })
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["glab"], returncode=0, stdout=payload, stderr=""))
    monkeypatch.setattr(
        mr, "_fetch_array",
        lambda *a, **k: ([{"status": pipe_status, "id": 4242}], None))
    _stub_jobs(monkeypatch, jobs)
    monkeypatch.setattr(sys, "argv", ["mr.py", "1607", "status"])
    assert mr.main() == 0
    return capsys.readouterr().out


def test_the_status_render_tallies_a_green_pipeline(monkeypatch,
                                                    capsys) -> None:
    """The gate change. This is the poll a maintainer merges on."""
    out = _render(monkeypatch, capsys, pipe_status="success",
                  jobs=_jobs("success", "success", "skipped", "manual"))
    assert "pipeline: success" in out
    line = _tally_line(out.splitlines())
    assert line.startswith("legs: 4 total: 2 passed, 0 failed, 0 pending"), line
    assert "1 skipped" in line and "1 manual" in line


def test_the_status_render_still_tallies_a_failed_pipeline(monkeypatch,
                                                           capsys) -> None:
    out = _render(monkeypatch, capsys, pipe_status="failed",
                  jobs=_jobs("success", "failed"))
    line = _tally_line(out.splitlines())
    assert line == "legs: 2 total: 1 passed, 1 failed, 0 pending " + checks.NOT_GREEN, line
    # The named-legs disclosure this replaces must not have been lost with it.
    assert "failed: leg1 (job #2)" in out, out
