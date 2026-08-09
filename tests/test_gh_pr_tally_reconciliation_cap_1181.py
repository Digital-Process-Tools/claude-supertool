"""#1181 — `gh-pr:N:status` declines the tally, and never says why.

The issue attributes this to the Copilot check-run "having no workflow to count
against". Measured against the live repo on 2026-08-09, that is not the
mechanism, and the check named is not the one involved:

    #1208  rollup 22, actions legs 21, non-Actions entries ['CodeQL'],
           4 runs on the commit -> declared=21, reconciled, no marker
    #1177  rollup 21, actions legs 20, non-Actions entries ['CodeQL'],
           6 runs on the commit (five of them named 'changelog') -> declared=None
    #1175  rollup 22, actions legs 21,
           5 runs incl. 'Running Copilot Code Review' -> declared=None

Copilot *does* have an enumerable workflow run. What it does is add a fifth run
to a repo that already has four, tipping `_declared_for_commit` over
`MAX_RECONCILED_RUNS = 4`, which returns `None`, which is the only path to
`TALLY UNVERIFIED`. A re-run does the same thing without Copilot: #1177 and
#1178 carry five separate `changelog` run records on one head sha.

An extra rollup entry belonging to no Actions run — CodeQL — is harmless:
`shortfall` reconciles on `declared <= found` precisely so external checks are
extra rather than missing.

Two defects follow, and this file pins both:

* the cap counts several run records of the *same workflow* on one commit as
  distinct runs, so re-running a leg blinds the tally
* the decline says only that the count "could not be established", which is
  true of every cause and actionable for none
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


checks = _load("checks_1181", "presets/_checks.py")
pr = _load("github_pr_1181", "presets/github/pr.py")


def _detail(run_id: str, name: str) -> dict:
    return {
        "name": name,
        "conclusion": "SUCCESS",
        "detailsUrl": f"https://github.com/o/n/actions/runs/{run_id}/job/1",
    }


def _pr_json(rollup: list[dict]) -> dict:
    return {
        "url": "https://github.com/o/n/pull/1177",
        "headRefOid": "a" * 40,
        "statusCheckRollup": rollup,
    }


# ---------------------------------------------------------------------------
# the re-run case — #1177 / #1178, five `changelog` records on one sha
# ---------------------------------------------------------------------------

def test_repeated_runs_of_one_workflow_do_not_blind_the_tally(
        monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [("1", "changelog"), ("2", "changelog"), ("3", "changelog"),
            ("4", "changelog"), ("5", "changelog"), ("6", "tests")]
    monkeypatch.setattr(pr, "_runs_on_commit", lambda *_a: list(runs))
    monkeypatch.setattr(pr._declared_legs, "legs_for_run",
                        lambda _o, _r, rid: ["changelog"] if rid != "6"
                        else ["t1", "t2"])
    rollup = [_detail("1", "changelog"), _detail("6", "t1"), _detail("6", "t2")]
    result = pr._declared_for_commit(_pr_json(rollup))
    declared, uncovered = result[0], result[2]
    assert declared is not None, (
        "five records of one workflow on one commit is one workflow, not five "
        "runs' worth of reconciliation budget"
    )
    assert uncovered == []


# ---------------------------------------------------------------------------
# the Copilot case — a fifth *distinct* workflow on a four-workflow repo
# ---------------------------------------------------------------------------

def test_five_distinct_workflows_still_reconcile(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """This repo has four workflows; Copilot's review run is the fifth."""
    runs = [("1", "changelog"), ("2", "tests"), ("3", "PR #1175"),
            ("4", "Code Quality: PR #1175"), ("5", "Running Copilot Code Review")]
    monkeypatch.setattr(pr, "_runs_on_commit", lambda *_a: list(runs))
    monkeypatch.setattr(pr._declared_legs, "legs_for_run",
                        lambda _o, _r, rid: [f"leg{rid}"])
    rollup = [_detail(str(i), f"leg{i}") for i in range(1, 6)]
    declared = pr._declared_for_commit(_pr_json(rollup))[0]
    assert declared == 5, (
        f"a five-workflow commit must still be reconcilable; got {declared!r}"
    )


def test_a_genuinely_unreadable_run_still_declines(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The #454 disclosure is not being silenced — only made informative."""
    monkeypatch.setattr(pr, "_runs_on_commit", lambda *_a: [("1", "tests")])
    monkeypatch.setattr(pr._declared_legs, "legs_for_run",
                        lambda _o, _r, _rid: None)
    declared = pr._declared_for_commit(_pr_json([_detail("1", "t1")]))[0]
    assert declared is None


def test_an_unreadable_commit_listing_still_declines(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr, "_runs_on_commit", lambda *_a: None)
    declared = pr._declared_for_commit(_pr_json([_detail("1", "t1")]))[0]
    assert declared is None


# ---------------------------------------------------------------------------
# the decline has to name its own cause
# ---------------------------------------------------------------------------

def test_the_unverified_line_carries_the_reason() -> None:
    marker, lines = checks.shortfall(
        21, None, reason="9 Actions runs on this commit exceed the "
                         "reconciliation cap of 8")
    assert marker == checks.UNVERIFIED_MARK
    body = " ".join(lines)
    assert "cap of 8" in body, (
        f"a warning that names no cause is one nobody can act on; got {body!r}"
    )
    assert "21 legs read" in body, "the existing #454 wording must survive"


def test_the_unverified_line_without_a_reason_is_unchanged() -> None:
    _marker, lines = checks.shortfall(21, None)
    body = " ".join(lines)
    assert "21 legs read" in body
    assert "could not be established" in body


def test_reconciled_and_short_paths_are_untouched() -> None:
    assert checks.shortfall(21, 21) == ("", [])
    assert checks.shortfall(21, 22)[0].startswith(checks.INCOMPLETE_MARK)
