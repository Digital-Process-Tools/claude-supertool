"""#827 — `gh-pr` names a non-Actions leg's id, so the finding is one op away.

The half of #827 that matters most in practice: you start from a red PR, not
from an id you already have. `gh-pr:N:status` names the failing legs (#619) and
prints an id beside each — except that `_checks.github_job_id()` only knows one
URL shape, `/actions/runs/<run>/job/<job>`, so a CodeQL leg is named with no id
at all and the reader is stranded.

**A live finding contradicts #821's docs.** `docs/presets/github.md` says "the
check-run id rides on `detailsUrl` only for Actions legs, so `gh-pr:N:status`
names a non-Actions leg with no id beside it". Read against the real API on
2026-08-05, `gh pr view 821 --json statusCheckRollup` returns, for the
`github-advanced-security` leg:

    {"name": "CodeQL",
     "detailsUrl": "https://github.com/Digital-Process-Tools/claude-supertool/runs/92264897684"}

That integer *is* the check-run id — `check-runs/92264897684` resolves to
`CodeQL` / `github-advanced-security`. The id was there the whole time, in a
field `gh-pr` already fetches, in a second URL shape nothing parsed. So this
half costs **no extra request**: it is a regex, not a call.

The trap the second shape sets is the reason `github_check_ref` returns a kind
and not just a number: an Actions leg's URL contains `/runs/<run-id>` too, and
a parser that matched it loosely would print the *run* id wearing a check run's
label — a wrong id under a confident header, which is the defect class this
whole issue is about, manufactured fresh.

Not verified live: the external-CI URL below, and every 404 path.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


checks = _load("checks_827", "presets/_checks.py")
pr = _load("github_pr_827", "presets/github/pr.py")

CHECK_ID = "92264897684"
CHECK_URL = f"https://github.com/Digital-Process-Tools/claude-supertool/runs/{CHECK_ID}"


# ---------------------------------------------------------------------------
# presets/_checks.py — pure, no gh
# ---------------------------------------------------------------------------

def test_a_non_actions_leg_yields_its_check_run_id() -> None:
    """The live-data defect: the id was in `detailsUrl` and nothing read it."""
    assert checks.github_check_ref({"detailsUrl": CHECK_URL}) == ("check", CHECK_ID)


def test_an_actions_leg_yields_a_job_id_and_never_the_run_id() -> None:
    """`/actions/runs/<run>/job/<job>` contains `/runs/<n>`. Do not take the bait."""
    url = "https://github.com/o/r/actions/runs/30993457393/job/92264786336"
    assert checks.github_check_ref({"detailsUrl": url}) == ("job", "92264786336")


def test_an_external_ci_url_yields_no_ref_at_all() -> None:
    """Legacy commit statuses point at someone else's server (#619).

    A path-shaped match on a foreign host would hand `gh-job` an integer that
    means something else entirely.
    """
    assert checks.github_check_ref(
        {"detailsUrl": "https://ci.example.com/runs/42"}) == ("", "")
    assert checks.github_check_ref({"detailsUrl": ""}) == ("", "")
    assert checks.github_check_ref({}) == ("", "")
    assert checks.github_check_ref("not a dict") == ("", "")  # type: ignore[arg-type]


def test_named_disclosure_labels_a_check_run_leg_as_a_check() -> None:
    entries = [("CodeQL", "FAILURE", "check", CHECK_ID)]
    lines = checks.named_disclosure(entries)
    assert lines == [f"  failed: CodeQL (check #{CHECK_ID})"]


def test_named_disclosure_still_labels_an_actions_leg_as_a_job() -> None:
    entries = [("pytest (ubuntu-latest, 3.9)", "FAILURE", "job", "111")]
    lines = checks.named_disclosure(entries)
    assert lines == ["  failed: pytest (ubuntu-latest, 3.9) (job #111)"]


def test_named_states_carries_the_kind_through() -> None:
    rollup = [
        {"name": "CodeQL", "conclusion": "FAILURE", "status": "COMPLETED",
         "detailsUrl": CHECK_URL},
        {"name": "pytest", "conclusion": "FAILURE", "status": "COMPLETED",
         "detailsUrl": "https://github.com/o/r/actions/runs/1/job/7"},
    ]
    assert checks.github_named_states(rollup) == [
        ("CodeQL", "FAILURE", "check", CHECK_ID),
        ("pytest", "FAILURE", "job", "7"),
    ]


# ---------------------------------------------------------------------------
# gh-pr:N:status — end to end
# ---------------------------------------------------------------------------

def _fake_gh_run(stdout: str, returncode: int = 0) -> Any:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr="")


def _pr_payload(**overrides: Any) -> str:
    base = {
        "number": 821,
        "title": "feat(gh-check): read a check run's annotations",
        "state": "OPEN",
        "author": {"login": "max"},
        "headRefName": "feat/793",
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
        "url": "https://github.com/o/r/pull/821",
        "body": "",
        "comments": [],
    }
    base.update(overrides)
    return json.dumps(base)


def test_slim_status_names_the_check_run_id_for_a_non_actions_leg(
        monkeypatch, capsys) -> None:
    """The reader's actual path: red PR → id → `gh-job:<id>`, no namespace lore."""
    rollup = [
        {"name": "pytest (ubuntu-latest, 3.9)", "conclusion": "SUCCESS",
         "status": "COMPLETED",
         "detailsUrl": "https://github.com/o/r/actions/runs/1/job/1"},
        {"name": "CodeQL", "conclusion": "FAILURE", "status": "COMPLETED",
         "detailsUrl": CHECK_URL},
    ]
    monkeypatch.setattr(pr.subprocess, "run",
                        lambda *a, **kw: _fake_gh_run(_pr_payload(statusCheckRollup=rollup)))
    monkeypatch.setattr(sys, "argv", ["pr.py", "821", "status"])
    rc = pr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert f"CodeQL (check #{CHECK_ID})" in out


def test_the_named_id_is_the_check_run_never_the_actions_run(
        monkeypatch, capsys) -> None:
    """The failure mode of a sloppier regex, pinned end to end."""
    rollup = [
        {"name": "pytest (ubuntu-latest, 3.9)", "conclusion": "FAILURE",
         "status": "COMPLETED",
         "detailsUrl": "https://github.com/o/r/actions/runs/30993457393/job/92264786336"},
    ]
    monkeypatch.setattr(pr.subprocess, "run",
                        lambda *a, **kw: _fake_gh_run(_pr_payload(statusCheckRollup=rollup)))
    monkeypatch.setattr(sys, "argv", ["pr.py", "821", "status"])
    pr.main()
    out = capsys.readouterr().out
    assert "job #92264786336" in out
    assert "30993457393" not in out
    assert "check #" not in out
