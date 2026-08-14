"""Two runs of one workflow on one SHA are two runs, not a re-run (#1640).

GitHub's default code-scanning setup emits **two distinct Actions runs per
push**, both carrying the same `workflow_id`, the same `path`
(`dynamic/github-code-scanning/codeql`) and the same `workflowName` as `gh`
renders it. Measured on `d1bb0837`, the v0.42.0 release candidate:

    31749711508  tests                          wf=280714892  16 legs
    31749711130  Push on master                 wf=327505695   3 legs
    31749711175  Code Quality: Push on master   wf=327505695   2 legs

`latest_per_workflow` keyed on the workflow name and kept the higher run id,
so `gh-branch` reported *18 legs across 2 workflows* over a population of 21
legs across 3 runs — and the dropped run carried `Analyze (actions)`, a leg no
other run on that commit performs. Everything was green, so the verdict was
true by luck; a failure in the dropped run would have rendered identically, on
the op the release gate is.

The discriminator cannot be the display name, the `workflow_id` or the `path`,
because the two runs share all three. It is the **run id**. And a re-run is a
further *attempt on the same run object* — `gh run rerun` reuses the id and
bumps `run_attempt` — so attempts still collapse and runs no longer do.

Every assertion here is written against "would this still pass if the code did
nothing": each one feeds a second run of an already-present workflow name and
demands it reach the verdict, which name-keyed selection cannot do.
"""
from __future__ import annotations

import importlib.util
import json
import re
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


branch = _load("presets/github/branch.py", "github_branch_1640")

_HEAD = "d1bb0837b00bfebc8ffbd5ce987f36977da8eb73"
_PREV = "fd254f05b33b83d38026d0156791cb8e18686b81"

# The two ids GitHub allocated to the two CodeQL runs on `d1bb0837`, kept
# verbatim so the ordering that mattered is the real one: the *dropped* run is
# the lower id, which is what made "keep the newest" look harmless.
_CODEQL_DROPPED = 31749711130
_CODEQL_KEPT = 31749711175
_TESTS_RUN = 31749711508


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _run(workflow: str, sha: str, status: str, conclusion: str | None,
         run_id: int, created: str = "2026-08-13T20:00:00Z",
         attempt: int = 1) -> dict:
    return {"workflowName": workflow, "headSha": sha, "databaseId": run_id,
            "status": status, "conclusion": conclusion, "event": "push",
            "createdAt": created, "attempt": attempt}


def _job(name: str, conclusion: str = "success") -> dict:
    return {"name": name, "status": "completed", "conclusion": conclusion,
            "databaseId": 900 + len(name), "steps": []}


def _iso(secs_ago: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(seconds=secs_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# The commit as GitHub actually had it: three runs, two of them one workflow.
def _live_runs(codeql_dropped_conclusion: str = "success") -> list:
    return [
        _run("tests", _HEAD, "completed", "success", _TESTS_RUN),
        _run("CodeQL", _HEAD, "completed", codeql_dropped_conclusion,
             _CODEQL_DROPPED, created="2026-08-13T20:00:01Z"),
        _run("CodeQL", _HEAD, "completed", "success", _CODEQL_KEPT,
             created="2026-08-13T20:00:02Z"),
    ]


_LIVE_JOBS = {
    _TESTS_RUN: [_job(f"pytest {i}") for i in range(16)],
    _CODEQL_DROPPED: [_job("Analyze (javascript-typescript)"),
                      _job("Analyze (python)"), _job("Analyze (actions)")],
    _CODEQL_KEPT: [_job("Analyze (python)"),
                   _job("Analyze (javascript-typescript)")],
}


def _render(monkeypatch, capsys, *, runs: list, jobs: dict,
            head_age: int = 4000, argv: list | None = None) -> str:
    def fake(cmd, *a, **kw):
        argv_ = list(cmd)
        joined = " ".join(str(x) for x in argv_)
        if argv_[:3] == ["gh", "repo", "view"]:
            return _Completed(json.dumps({
                "nameWithOwner": "Digital-Process-Tools/claude-supertool",
                "defaultBranchRef": {"name": "master"}}))
        if argv_[:2] == ["gh", "api"]:
            return _Completed(json.dumps({
                "sha": _HEAD,
                "commit": {"committer": {"date": _iso(head_age)}}}))
        if argv_[:3] == ["gh", "run", "list"]:
            return _Completed(json.dumps(runs))
        if argv_[:3] == ["gh", "run", "view"]:
            payload = jobs.get(int(argv_[3]), "__missing__")
            if payload == "__missing__":
                return _Completed("", 1, "HTTP 500")
            return _Completed(json.dumps({"jobs": payload}))
        return _Completed("", 1, "unexpected call: " + joined)

    monkeypatch.setattr(branch.subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["branch.py"] + (argv or []))
    branch.main()
    return capsys.readouterr().out


def _line(out: str, prefix: str) -> str:
    for line in out.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no {prefix!r} line in output:\n{out}")


def _state(out: str) -> str:
    """The state token off the verdict line — `NOT GREEN` contains `GREEN`."""
    line = _line(out, "Commit d1bb083:")
    m = re.match(r"Commit .+?: (.+)$", line)
    assert m, f"unparseable commit line: {line}"
    return m.group(1).strip()


# ---------------------------------------------------------------------------
# selection: the unit is the run
# ---------------------------------------------------------------------------

def test_two_runs_of_one_workflow_on_one_sha_are_both_selected() -> None:
    selected = branch.runs_on_sha(_live_runs(), _HEAD)

    ids = sorted(branch._run_id(r) for r in selected.values())
    assert ids == [_CODEQL_DROPPED, _CODEQL_KEPT, _TESTS_RUN], (
        "a run was dropped from the population the verdict is computed over: "
        f"{ids}")


def test_a_rerun_is_one_run_and_the_newest_attempt_wins() -> None:
    """`gh run rerun` reuses the run id and bumps `run_attempt` (#1409)."""
    runs = [_run("tests", _HEAD, "completed", "failure", _TESTS_RUN, attempt=1),
            _run("tests", _HEAD, "completed", "success", _TESTS_RUN, attempt=2)]
    selected = branch.runs_on_sha(runs, _HEAD)

    assert len(selected) == 1, (
        "two attempts of one run were counted as two runs: " + repr(selected))
    (run,) = selected.values()
    assert run["attempt"] == 2, "a superseded attempt outranked the latest one"


def test_runs_on_other_shas_are_still_ignored() -> None:
    assert branch.runs_on_sha(
        [_run("tests", _PREV, "completed", "success", 30)], _HEAD) == {}


def test_a_name_with_two_runs_is_still_one_covered_workflow() -> None:
    """The name-keyed consumers must not read two runs as a missing workflow."""
    selected = branch.runs_on_sha(_live_runs(), _HEAD)

    assert branch.workflow_names(selected) == {"tests", "CodeQL"}
    assert branch.missing_workflows({"tests", "CodeQL"}, selected) == []


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------

def test_a_failure_in_the_dropped_run_is_not_green(monkeypatch, capsys) -> None:
    """The whole issue: the lower-id run fails and the higher-id run passes."""
    out = _render(monkeypatch, capsys,
                  runs=_live_runs(codeql_dropped_conclusion="failure"),
                  jobs={**_LIVE_JOBS,
                        _CODEQL_DROPPED: [_job("Analyze (javascript-typescript)"),
                                          _job("Analyze (python)"),
                                          _job("Analyze (actions)", "failure")]},
                  argv=[_HEAD])

    assert _state(out) == branch.NOT_GREEN, (
        "a failing run was dropped by selection and the gate cleared the "
        "commit:\n" + out)
    assert str(_CODEQL_DROPPED) in out, (
        "the failing run is not even named, so a reader cannot find it:\n" + out)


def test_every_run_reaches_the_tally_and_the_table(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, runs=_live_runs(), jobs=_LIVE_JOBS,
                  argv=[_HEAD])

    assert _state(out) == branch.GREEN, out
    verdict = _line(out, "Verdict:")
    assert "21 legs" in verdict, (
        "the population is 16 + 3 + 2 = 21 legs; the verdict counted "
        "otherwise: " + verdict)
    assert "3 runs" in verdict, (
        "the verdict still counts workflows, which is the axis two runs of "
        "one workflow collapse on: " + verdict)
    for rid in (_TESTS_RUN, _CODEQL_DROPPED, _CODEQL_KEPT):
        assert str(rid) in out, (
            f"run {rid} has no row, so a reader cannot check the verdict "
            f"against it:\n" + out)


def test_the_two_rows_are_told_apart_by_run_id(monkeypatch, capsys) -> None:
    """Two rows carrying one workflow name must be distinguishable."""
    out = _render(monkeypatch, capsys, runs=_live_runs(), jobs=_LIVE_JOBS,
                  argv=[_HEAD])

    rows = [ln for ln in out.splitlines() if ln.startswith("CodeQL")]
    assert len(rows) == 2, "one of the two CodeQL runs has no row:\n" + out
    assert str(_CODEQL_DROPPED) in rows[0] + rows[1]
    assert str(_CODEQL_KEPT) in rows[0] + rows[1]
