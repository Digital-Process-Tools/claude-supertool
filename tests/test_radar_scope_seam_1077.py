"""#1077 — #846's scope seam named two callers and only one was wired.

`branch.scope_for` exists, in its own docstring's words, because "a caller that
has to remember to compute this will not". `dashboard.py` remembered.
`presets/watch/tiers/gh_prs.py` did not, and radar is the surface that reports
master's health on every tick.

Two things are pinned here, and they are different claims:

  * **the seam is a mechanism, not a reminder.** `verdict()` cannot be called
    without a scope. Omitting it is a `TypeError` at the call site rather than a
    green that silently over-claims. A test that only checked the tier would
    pass again the next time a fourth caller is added.
  * **the tier's silence is calibrated, not blanket.** Blanking on GREEN threw
    the disclosure away. Printing the clause on every green would put two
    permanently-undispatched cron/pull_request workflows on the board every
    tick, which is the habituation failure `scope_clause` itself guards against.
    So the tier speaks for the cases a green cannot account for — a
    **push-triggered** workflow that produced no run, or a declared set that
    could not be established — and stays quiet otherwise.

`could_tell` is the field that matters for the second one: radar's
`quiet_when_healthy` drops this tier's whole output when it reports healthy, so
un-blanking the lines without moving `could_tell` would emit them into a
suppressed tier.
"""
from __future__ import annotations

import base64
import importlib.util
import json
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


tier = _load("presets/watch/tiers/gh_prs.py", "radar_gh_prs_1077")
branch = tier.branch

SHA = "b3abea203ad1cbcbaafdea4c3fafbe1108ba6018"
RUN_ID = 41000000001

TESTS_YML = "name: tests\n\non:\n  push:\n    branches: [master]\n  pull_request:\n"
SLOW_YML = "name: slow tests\n\non:\n  schedule:\n    - cron: '0 6 * * *'\n"
CHANGELOG_YML = "name: changelog\n\non:\n  pull_request:\n"
# The shape the v0.27.0 mis-cut generalises to: a workflow that a push IS
# supposed to reach, producing no run on the head commit.
DEPLOY_YML = "name: deploy\n\non:\n  push:\n    branches: [master]\n"

QUIET_FILES = {
    ".github/workflows/tests.yml": TESTS_YML,
    ".github/workflows/slow-tests.yml": SLOW_YML,
    ".github/workflows/changelog.yml": CHANGELOG_YML,
}
LOUD_FILES = dict(QUIET_FILES, **{".github/workflows/deploy.yml": DEPLOY_YML})


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _Gh:
    """Fake `gh` for the whole `default_branch_report` chain."""

    def __init__(self, files=None, ran=("tests",), dir_rc: int = 0,
                 leg_conclusion: str = "success") -> None:
        self.files = QUIET_FILES if files is None else files
        self.ran = list(ran)
        self.dir_rc = dir_rc
        self.leg_conclusion = leg_conclusion

    def __call__(self, argv, *a, **kw):
        argv = list(argv)
        joined = " ".join(argv)
        if argv[:3] == ["gh", "repo", "view"]:
            return _Completed(json.dumps({
                "nameWithOwner": "o/r",
                "defaultBranchRef": {"name": "master"}}))
        if "contents/.github/workflows?" in joined:
            if self.dir_rc:
                return _Completed("", self.dir_rc, "HTTP 500")
            return _Completed(json.dumps([
                {"name": p.rsplit("/", 1)[-1], "path": p, "type": "file"}
                for p in self.files
            ]))
        if "contents/.github/workflows/" in joined:
            path = joined.split("contents/")[1].split("?")[0]
            body = self.files.get(path, "")
            return _Completed(json.dumps({
                "encoding": "base64",
                "content": base64.b64encode(body.encode()).decode(),
            }))
        if "filter=all" in joined:
            return _Completed(json.dumps({"jobs": [{"name": "pytest"}]}))
        if "commits/" in joined:
            return _Completed(json.dumps({
                "sha": SHA,
                "commit": {"committer": {"date": "2020-01-01T00:00:00Z"}}}))
        if argv[:3] == ["gh", "run", "list"]:
            return _Completed(json.dumps([
                {"workflowName": name, "headSha": SHA,
                 "databaseId": RUN_ID + i, "status": "completed",
                 "conclusion": "success", "event": "push",
                 "createdAt": "2020-01-01T00:01:00Z", "attempt": 1}
                for i, name in enumerate(self.ran)
            ]))
        if argv[:3] == ["gh", "run", "view"]:
            return _Completed(json.dumps({"jobs": [
                {"name": "pytest", "status": "completed",
                 "conclusion": self.leg_conclusion, "databaseId": 1}]}))
        return _Completed("{}")


def _report(monkeypatch, gh: _Gh):
    monkeypatch.setattr(branch.subprocess, "run", gh)
    monkeypatch.setattr(branch._declared_legs.subprocess, "run", gh)
    monkeypatch.setattr(branch._declared_workflows.subprocess, "run", gh)
    monkeypatch.setattr(tier.subprocess, "run", gh)
    return tier.default_branch_report("master", "o/r")


# ---------------------------------------------------------------------------
# the seam is a mechanism
# ---------------------------------------------------------------------------

def test_verdict_cannot_be_called_without_a_scope() -> None:
    """The docstring's own argument, made structural.

    With `scope` defaulting to `""`, a new caller gets an unscoped green and no
    signal at all that it skipped #846. Required, it gets a `TypeError` on the
    first run.
    """
    selected = {"tests": {"status": "completed", "conclusion": "success"}}
    legs = {"tests": ["success"]}
    with pytest.raises(TypeError):
        branch.verdict(selected, legs, [], SHA, 0)


def test_scope_for_reports_whether_the_scope_is_resolved() -> None:
    """A caller deciding whether to speak must not have to parse the prose."""
    assert len(branch.scope_for("o/r", SHA, {})) == 3


# ---------------------------------------------------------------------------
# the tier — what it says, and when it says nothing
# ---------------------------------------------------------------------------

def test_a_push_triggered_absence_reaches_the_radar(monkeypatch) -> None:
    lines, _ok = _report(monkeypatch, _Gh(files=LOUD_FILES, ran=["tests"]))
    body = "\n".join(lines)
    assert "deploy" in body, (
        f"a declared push-triggered workflow with no run on the head commit is "
        f"invisible on the board that reports master every tick:\n{body}")
    assert "NOT covered" in body, body


def test_a_push_triggered_absence_means_the_tier_could_not_tell(
        monkeypatch) -> None:
    """`quiet_when_healthy` drops this tier's whole output when it is healthy.

    So lines the tier emits under a healthy verdict are lines nobody reads.
    """
    _lines, could_tell = _report(monkeypatch, _Gh(files=LOUD_FILES,
                                                  ran=["tests"]))
    assert could_tell is False, (
        "the tier reported a green it cannot account for as one it could tell")


def test_an_unestablished_declared_set_means_the_tier_could_not_tell(
        monkeypatch) -> None:
    lines, could_tell = _report(monkeypatch, _Gh(dir_rc=1, ran=["tests"]))
    body = "\n".join(lines)
    assert "UNESTABLISHED" in body, (
        f"an unreadable workflow directory rendered as a fully covered "
        f"green:\n{body}")
    assert could_tell is False, body


def test_a_cron_only_absence_keeps_the_board_quiet(monkeypatch) -> None:
    """Calibration, not blanket disclosure.

    On this repo `slow tests` (schedule) and `changelog` (pull_request) produce
    no run on any master push, forever. Speaking about them every tick is how
    the one render that matters gets skipped — `scope_clause`'s own docstring
    says this repo has paid for that twice.
    """
    lines, could_tell = _report(monkeypatch, _Gh(ran=["tests"]))
    assert lines == [], (
        f"the board now says the same thing on every tick:\n{lines}")
    assert could_tell is True


def test_a_fully_covered_green_still_says_nothing(monkeypatch) -> None:
    lines, could_tell = _report(
        monkeypatch, _Gh(ran=["tests", "slow tests", "changelog"]))
    assert lines == [], lines
    assert could_tell is True


def test_a_red_master_is_unaffected(monkeypatch) -> None:
    """The scope rides on the green only; a finding must not be diluted."""
    lines, could_tell = _report(monkeypatch, _Gh(
        files=LOUD_FILES, ran=["tests"], leg_conclusion="failure"))
    body = "\n".join(lines)
    assert branch.NOT_GREEN in body, body
    assert could_tell is True, (
        "a red master is a thing the tier could tell, not a blind spot")
