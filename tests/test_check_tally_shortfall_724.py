"""The tally must disclose legs it never saw, not report the remainder as the whole (#724).

On PR #715, `gh-pr:715:status` printed `9 total: 8 passed, 0 failed, 1 pending`
against a fourteen-leg matrix, one minute after printing `14 total`. The
arithmetic check from #454 cannot catch that: `8 + 0 + 1 = 9` and the header
says `9 total`, so the line is internally consistent. Had the last leg gone
green the next call would have read `9 total: 9 passed, 0 failed, 0 pending`
with no warning marker at all — five legs invisible rather than pending, in
the one line a merge gate reads.

So these tests never assert a count on its own. Every one of them pins the
*difference* between the shortfall state and the reconciled state: same
rollup, same payload, only the number of legs the run declares changes. A
test that asserted `9 total` would pass on the broken code — that is exactly
what shipped.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

import pytest

PRESETS = Path(__file__).parent.parent / "presets"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr = _load("github_pr_724", PRESETS / "github" / "pr.py")
checks_mod = _load("supertool_checks_724", PRESETS / "_checks.py")

RUN_ID = "30687891233"


def _fake_run(stdout: str, returncode: int = 0) -> Any:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=""
    )


def _leg(name: str, state: str = "SUCCESS", run: str = RUN_ID, job: int = 1) -> dict:
    entry: dict[str, Any] = {"name": name, "status": "COMPLETED", "conclusion": state}
    if state in ("IN_PROGRESS", "QUEUED"):
        entry = {"name": name, "status": state}
    entry["detailsUrl"] = f"https://github.com/o/r/actions/runs/{run}/job/{job}"
    return entry


def _matrix(n: int, state: str = "SUCCESS") -> list[dict]:
    """`n` legs off one Actions run, named like this repo's real matrix."""
    return [_leg(f"pytest (ubuntu-latest, 3.{9 + i})", state, job=100 + i)
            for i in range(n)]


def _payload(rollup: list[dict], **overrides: Any) -> str:
    base = {
        "number": 715,
        "title": "fix: something",
        "state": "OPEN",
        "author": {"login": "max"},
        "headRefName": "fix/724",
        "baseRefName": "master",
        "labels": [],
        "milestone": None,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "reviews": [],
        "mergeCommit": None,
        "additions": 10,
        "deletions": 1,
        "changedFiles": 2,
        "statusCheckRollup": rollup,
        "url": "https://github.com/o/r/pull/715",
        "body": "",
        "comments": [],
    }
    base.update(overrides)
    return json.dumps(base)


def _run_pr(monkeypatch, capsys, rollup: list[dict],
            declared: int | None, declared_names: Sequence[str] = (),
            slim: bool = True, **ov: Any) -> str:
    """Render `gh-pr` with the run's declared leg count stubbed."""
    payload = _payload(rollup, **ov)
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **kw: _fake_run(payload))
    monkeypatch.setattr(pr, "_fetch_review_threads", lambda *a, **kw: [])
    monkeypatch.setattr(pr, "_head_commit_age_secs", lambda *a, **kw: 60)
    monkeypatch.setattr(
        pr, "_declared_legs", lambda *a, **kw: (declared, list(declared_names))
    )
    argv = ["pr.py", "715"] + (["status"] if slim else [])
    monkeypatch.setattr(sys, "argv", argv)
    assert pr.main() == 0
    return capsys.readouterr().out


def _checks_line(out: str) -> str:
    for line in out.splitlines():
        if line.lower().startswith("checks:"):
            return line
    raise AssertionError(f"no checks line in output:\n{out}")


# --- Pin 1: the shortfall state must not render like the reconciled one -----
#
# Same nine-leg rollup in both calls. The *only* difference is the number of
# legs the run declares. Code that does nothing with the declared count emits
# byte-identical output here and this test fails — which is the whole bar.


def test_shortfall_renders_differently_from_a_reconciled_tally(monkeypatch, capsys):
    rollup = _matrix(9)
    reconciled = _run_pr(monkeypatch, capsys, rollup, declared=9)
    short = _run_pr(monkeypatch, capsys, rollup, declared=14)
    assert short != reconciled, (
        "nine legs read out of a declared fourteen rendered identically to "
        "nine out of nine — the shortfall is invisible"
    )


def test_shortfall_states_both_numbers(monkeypatch, capsys):
    out = _run_pr(monkeypatch, capsys, _matrix(9), declared=14)
    assert "9 of 14" in out, out


# --- Pin 2: an all-green shortfall must never read as green -----------------
#
# The live near-miss. Nine legs, every one SUCCESS, five never seen. The old
# line was `9 total: 9 passed, 0 failed, 0 pending` — no marker, everything
# summing, and the merge gate says merge.


def test_all_passing_shortfall_carries_a_warning_marker(monkeypatch, capsys):
    line = _checks_line(_run_pr(monkeypatch, capsys, _matrix(9), declared=14))
    assert "⚠" in line, (
        f"nine green legs out of fourteen declared printed an unmarked "
        f"all-clear: {line!r}"
    )


def test_all_passing_shortfall_differs_from_a_genuine_all_green(monkeypatch, capsys):
    green = _checks_line(_run_pr(monkeypatch, capsys, _matrix(9), declared=9))
    short = _checks_line(_run_pr(monkeypatch, capsys, _matrix(9), declared=14))
    assert "⚠" not in green
    assert short != green


# --- Pin 3: the legs that were not read are named --------------------------


def test_missing_legs_are_named(monkeypatch, capsys):
    declared_names = [f"pytest (ubuntu-latest, 3.{9 + i})" for i in range(9)] + [
        "pytest (windows-latest, 3.9)",
        "notifiers (bun + TypeScript) (ubuntu-latest)",
    ]
    out = _run_pr(monkeypatch, capsys, _matrix(9), declared=11,
                  declared_names=declared_names)
    assert "pytest (windows-latest, 3.9)" in out
    assert "notifiers (bun + TypeScript) (ubuntu-latest)" in out


def test_missing_leg_names_are_capped(monkeypatch, capsys):
    declared_names = [f"pytest (ubuntu-latest, 3.{9 + i})" for i in range(9)] + [
        f"missing-leg-{i}" for i in range(9)
    ]
    out = _run_pr(monkeypatch, capsys, _matrix(9), declared=18,
                  declared_names=declared_names)
    assert "+4 more" in out, out


# --- Pin 4: reconciled stays terse -----------------------------------------
#
# The disclosure must cost nothing on the common path, or it gets ignored.


def test_reconciled_all_green_adds_no_lines(monkeypatch, capsys):
    out = _run_pr(monkeypatch, capsys, _matrix(14), declared=14)
    assert "⚠" not in out, out
    assert "of 14" not in out
    assert len(out) < 500


def test_declared_below_found_is_not_a_shortfall(monkeypatch, capsys):
    """An external check with no Actions run is extra, not missing."""
    rollup = _matrix(9) + [{"name": "netlify/deploy-preview",
                            "state": "SUCCESS", "detailsUrl": "https://nfy/x"}]
    out = _run_pr(monkeypatch, capsys, rollup, declared=9)
    assert "⚠" not in out, out


# --- Pin 5: unestablished declared count declines, it does not assume -------
#
# docs/validators.md, "Declining instead of guessing". Defaulting to the
# larger number silently would trade a loud failure for a quiet one; so would
# defaulting to the smaller one.


def test_unestablished_declared_count_is_declined_not_assumed(monkeypatch, capsys):
    out = _run_pr(monkeypatch, capsys, _matrix(9), declared=None)
    line = _checks_line(out)
    assert "⚠" in line, line
    assert "UNVERIFIED" in out.upper(), out


def test_unestablished_differs_from_reconciled(monkeypatch, capsys):
    unknown = _run_pr(monkeypatch, capsys, _matrix(9), declared=None)
    known = _run_pr(monkeypatch, capsys, _matrix(9), declared=9)
    assert unknown != known


def test_a_rollup_with_no_actions_run_reconciles_silently(monkeypatch, capsys):
    """No run id anywhere means no declared count exists to be short of."""
    rollup = [{"name": "netlify/deploy-preview", "state": "SUCCESS",
               "detailsUrl": "https://netlify.example/build/42"}]
    out = _run_pr(monkeypatch, capsys, rollup, declared=None)
    assert "⚠" not in out, out


# --- Pin 6: #454's arithmetic contract survives ----------------------------


def test_tally_terms_still_sum_to_the_legs_read(monkeypatch, capsys):
    rollup = _matrix(8) + [_leg("pytest (macos-latest, 3.9)", "IN_PROGRESS", job=9)]
    line = _checks_line(_run_pr(monkeypatch, capsys, rollup, declared=14))
    assert "9 total: 8 passed, 0 failed, 1 pending" in line, line


# --- Pin 7: the full dashboard discloses too -------------------------------


def test_full_dashboard_discloses_the_shortfall(monkeypatch, capsys):
    short = _run_pr(monkeypatch, capsys, _matrix(9), declared=14, slim=False)
    reconciled = _run_pr(monkeypatch, capsys, _matrix(9), declared=9, slim=False)
    assert short != reconciled
    assert "9 of 14" in short


# --- Pin 8: the pure reconciliation, unit level ----------------------------


def test_shortfall_helper_is_silent_when_it_reconciles():
    marker, lines = checks_mod.shortfall(found=14, declared=14)
    assert marker == ""
    assert lines == []


def test_shortfall_helper_marks_a_gap():
    marker, lines = checks_mod.shortfall(found=9, declared=14)
    assert marker != ""
    assert "9 of 14" in marker + " ".join(lines)


def test_shortfall_helper_declines_on_unknown_declared():
    marker, lines = checks_mod.shortfall(found=9, declared=None)
    assert "UNVERIFIED" in (marker + " ".join(lines)).upper()


def test_shortfall_helper_never_reports_more_found_than_declared_as_a_gap():
    assert checks_mod.shortfall(found=15, declared=14) == ("", [])


# --- Pin 9: the declared count is read off the run, not off the rollup ------


def _jobs_payload(names: Sequence[str], total: int | None = None) -> str:
    return json.dumps({
        "total_count": len(names) if total is None else total,
        "jobs": [{"name": n} for n in names],
    })


def test_declared_legs_reads_total_count_from_the_actions_api(monkeypatch):
    names = [f"pytest-{i}" for i in range(14)]
    monkeypatch.setattr(
        pr, "_gh", lambda *a, **kw: _fake_run(_jobs_payload(names))
    )
    total, got = pr._declared_legs("https://github.com/o/r/pull/715", [RUN_ID])
    assert total == 14
    assert got == names


def test_declared_legs_declines_when_gh_fails(monkeypatch):
    monkeypatch.setattr(pr, "_gh", lambda *a, **kw: _fake_run("", returncode=1))
    assert pr._declared_legs("https://github.com/o/r/pull/715", [RUN_ID]) == (None, [])


def test_declared_legs_declines_on_a_payload_without_a_count(monkeypatch):
    monkeypatch.setattr(pr, "_gh", lambda *a, **kw: _fake_run('{"number": 715}'))
    assert pr._declared_legs("https://github.com/o/r/pull/715", [RUN_ID]) == (None, [])


def test_declared_legs_costs_nothing_when_no_run_ids(monkeypatch):
    called: list[int] = []

    def _boom(*a: Any, **kw: Any):
        called.append(1)
        raise AssertionError("no run id — nothing should have been fetched")

    monkeypatch.setattr(pr, "_gh", _boom)
    assert pr._declared_legs("https://github.com/o/r/pull/715", []) == (None, [])
    assert not called


def test_rollup_run_ids_are_distinct_and_ordered():
    rollup = [
        _leg("a", run="77", job=1),
        _leg("b", run="77", job=2),
        _leg("c", run="88", job=3),
        {"name": "external", "detailsUrl": "https://ci.example/build/9"},
    ]
    assert pr._rollup_run_ids(rollup) == ["77", "88"]
