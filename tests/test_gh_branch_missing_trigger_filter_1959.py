"""#1959: branch mode called a commit NOT GREEN that its own footer said the
same workflow could not have produced a run for.

`missing_workflows(prev_names, selected)` treats "ran on the previous head,
absent here" as evidence of a missing run. For a workflow whose triggers
exclude the event that produced this commit -- `schedule`, `workflow_dispatch`
-- that is not evidence of anything: its last run came from the schedule, and
its absence on a push is exactly what the declaration predicts. The op already
computes that trigger analysis (`_declared_workflows.is_push_triggered`,
printed in the footer `undispatched_lines` renders) and `verdict()`'s
creation-window branch did not consult it before this fix.

Control pair, per the issue: a workflow whose triggers exclude this commit's
event must not hold the verdict, and a workflow that a push SHOULD reach and
that produced no run must still hold it -- that second one is the state the
whole gate exists for, and it is the one a careless fix destroys by dropping
too much rather than too little.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


branch = _load("presets/github/branch.py", "github_branch_1959")

SHA = "b" * 40


def _run(name: str, rid: int) -> dict:
    return {"workflowName": name, "databaseId": rid, "attempt": 1,
            "headSha": SHA, "status": "completed", "conclusion": "success"}


SLOW_TESTS_DECLARED = {
    "name": "slow tests", "path": ".github/workflows/slow-tests.yml",
    "triggers": ["schedule", "workflow_dispatch"],
}
DEPLOY_DECLARED = {
    "name": "deploy", "path": ".github/workflows/deploy.yml",
    "triggers": ["push"],
}
UNKNOWN_TRIGGERS_DECLARED = {
    "name": "mystery", "path": ".github/workflows/mystery.yml",
    "triggers": None,
}


# ---------------------------------------------------------------------------
# missing_workflows() itself
# ---------------------------------------------------------------------------

def test_a_schedule_only_workflow_is_dropped_from_missing():
    """The exact #1959 shape: ran on the previous head, absent here, and its
    own declared triggers say a push was never going to produce a run."""
    selected = branch.runs_on_sha([_run("tests", 1)], SHA)
    missing = branch.missing_workflows(
        {"tests", "slow tests"}, selected, [SLOW_TESTS_DECLARED])
    assert missing == [], missing


def test_a_push_triggered_workflow_is_still_reported_missing():
    """The control this fix must not destroy: a workflow a push SHOULD reach,
    absent here, still holds the verdict."""
    selected = branch.runs_on_sha([_run("tests", 1)], SHA)
    missing = branch.missing_workflows(
        {"tests", "deploy"}, selected, [DEPLOY_DECLARED])
    assert missing == ["deploy"], missing


def test_unreadable_triggers_are_not_dropped():
    """`None` is unknown, not "definitely not a push" -- must not be dropped
    on the strength of a trigger block this op could not read."""
    selected = branch.runs_on_sha([_run("tests", 1)], SHA)
    missing = branch.missing_workflows(
        {"tests", "mystery"}, selected, [UNKNOWN_TRIGGERS_DECLARED])
    assert missing == ["mystery"], missing


def test_a_workflow_absent_from_the_declared_set_is_not_dropped():
    """No declared entry for the name at all is not "provably not a push" --
    without an entry to consult, the safe default is to keep reporting it."""
    selected = branch.runs_on_sha([_run("tests", 1)], SHA)
    missing = branch.missing_workflows(
        {"tests", "ghost"}, selected, [DEPLOY_DECLARED])
    assert missing == ["ghost"], missing


def test_no_declared_set_at_all_keeps_the_old_behaviour():
    """`declared=None` (the default) is every caller this fix did not touch --
    unfiltered, byte for byte what `missing_workflows` returned before #1959."""
    selected = branch.runs_on_sha([_run("tests", 1)], SHA)
    missing = branch.missing_workflows({"tests", "slow tests"}, selected)
    assert missing == ["slow tests"], missing


# ---------------------------------------------------------------------------
# verdict() end to end -- the contradiction inside one render
# ---------------------------------------------------------------------------

def test_verdict_does_not_contradict_its_own_scope_footer():
    """The exact #1959 render: a schedule-only workflow missing inside the
    creation window must not produce a NOT GREEN the footer then disagrees
    with by saying no run was ever expected."""
    selected = branch.runs_on_sha([_run("tests", 1)], SHA)
    legs = {"tests": ["success"]}
    missing = branch.missing_workflows(
        {"tests", "slow tests"}, selected, [SLOW_TESTS_DECLARED])
    state, sentence = branch.verdict(
        selected, legs, missing, SHA, age_secs=600, grace=900,
        scope="")
    assert state == branch.GREEN, sentence
    assert "slow tests" not in sentence, sentence


def test_verdict_still_waits_on_a_push_triggered_gap_in_the_window():
    """Same shape, but the missing workflow IS push-triggered -- the verdict
    must still say "still expected, waiting is correct" inside the window.
    Losing this case is the fix silently over-reaching."""
    selected = branch.runs_on_sha([_run("tests", 1)], SHA)
    legs = {"tests": ["success"]}
    missing = branch.missing_workflows(
        {"tests", "deploy"}, selected, [DEPLOY_DECLARED])
    state, sentence = branch.verdict(
        selected, legs, missing, SHA, age_secs=600, grace=900,
        scope="")
    assert state == branch.NOT_GREEN, sentence
    assert "deploy" in sentence, sentence
    assert "still expected" in sentence, sentence


# ---------------------------------------------------------------------------
# full render through main() -- the exact contradiction from the issue
# ---------------------------------------------------------------------------

CURRENT_SHA = "c" * 40
PREV_SHA = "d" * 40
RUN_ID = 91000000001

TESTS_YML = "name: tests\n\non:\n  push:\n    branches: [master]\n"
SLOW_YML = "name: slow tests\n\non:\n  schedule:\n    - cron: '0 6 * * *'\n  workflow_dispatch: {}\n"
FILES = {
    ".github/workflows/tests.yml": TESTS_YML,
    ".github/workflows/slow-tests.yml": SLOW_YML,
}


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _Gh:
    """Fake `gh` for the whole `gh-branch` chain -- the #1959 reproduction:
    `tests` ran on the current head; `slow tests` ran only on the previous
    head, and its own declared triggers (schedule, workflow_dispatch) say a
    push was never going to reach it. The current head is inside the ~15min
    creation window, which is exactly when the bug fired live."""

    def __call__(self, argv, *a, **kw):
        argv = list(argv)
        joined = " ".join(argv)
        if argv[:3] == ["gh", "repo", "view"]:
            return _Completed(json.dumps({
                "nameWithOwner": "o/r",
                "defaultBranchRef": {"name": "master"}}))
        if "contents/.github/workflows?" in joined:
            return _Completed(json.dumps([
                {"name": Path(p).name, "path": p, "type": "file"}
                for p in FILES
            ]))
        if "contents/.github/workflows/" in joined:
            path = joined.split("contents/")[1].split("?")[0]
            body = FILES.get(path, "")
            return _Completed(json.dumps({
                "encoding": "base64",
                "content": base64.b64encode(body.encode()).decode(),
            }))
        if "filter=all" in joined:
            return _Completed(json.dumps({"jobs": [{"name": "pytest"}]}))
        if "commits/" in joined:
            recent = (datetime.now(timezone.utc) -
                      timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return _Completed(json.dumps({
                "sha": CURRENT_SHA,
                "commit": {"committer": {"date": recent}}}))
        if argv[:3] == ["gh", "run", "list"]:
            old = (datetime.now(timezone.utc) -
                   timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
            recent = (datetime.now(timezone.utc) -
                      timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return _Completed(json.dumps([
                {"workflowName": "tests", "headSha": CURRENT_SHA,
                 "databaseId": RUN_ID, "status": "completed",
                 "conclusion": "success", "event": "push",
                 "createdAt": recent, "attempt": 1},
                {"workflowName": "slow tests", "headSha": PREV_SHA,
                 "databaseId": RUN_ID + 1, "status": "completed",
                 "conclusion": "success", "event": "schedule",
                 "createdAt": old, "attempt": 1},
            ]))
        if argv[:3] == ["gh", "run", "view"]:
            return _Completed(json.dumps({"jobs": [
                {"name": "pytest", "status": "completed",
                 "conclusion": "success", "databaseId": 1}]}))
        return _Completed("{}")


def _render(monkeypatch, capsys) -> str:
    gh = _Gh()
    monkeypatch.setattr(branch.subprocess, "run", gh)
    monkeypatch.setattr(branch._declared_legs.subprocess, "run", gh)
    monkeypatch.setattr(branch._declared_workflows.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", ["branch.py", "master"])
    branch.main()
    return capsys.readouterr().out


def test_branch_mode_render_does_not_contradict_itself(monkeypatch, capsys) -> None:
    """The exact live shape from #1959: verdict and footer must agree.

    Before the fix, the verdict line said `slow tests` was still expected
    while the footer, eight lines below, said no run was expected from it at
    all -- naming the very trigger set that made it impossible.
    """
    out = _render(monkeypatch, capsys)
    verdict_line = next(l for l in out.splitlines() if l.startswith("Verdict:"))
    assert "Branch master: GREEN" in out, out
    # The old bug: the verdict said a run was "still expected" for a workflow
    # the footer, in the same render, said could not have run at all.
    assert "still expected" not in verdict_line, out
    assert "NOT GREEN" not in verdict_line, out
    assert "no push trigger" in out, out


def test_dashboard_default_section_shares_the_same_fix() -> None:
    """`presets/dashboard/dashboard.py` re-derives this verdict for the board
    read immediately before a release is tagged (#1959's own "release gate
    1"). It calls `missing_workflows`/`scope_for` independently of
    `gh-branch`'s `main()`, so the fix has to reach it too or the same
    contradiction survives on the one surface that gates a tag.
    """
    dash = _load("presets/dashboard/dashboard.py", "dashboard_1959")
    gh = _Gh()
    import subprocess as _sp
    real = _sp.run
    try:
        _sp.run = gh  # type: ignore[assignment]
        section = dash.collect_default("o/r", "master")
    finally:
        _sp.run = real
    body = "\n".join(section.lines or [])
    first_line = body.split("\n")[0]
    assert "master" in body and "GREEN" in first_line, body
    assert "NOT GREEN" not in first_line, body

