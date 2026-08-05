"""`gh-run`'s header must sum the table beneath it, not echo one GitHub field (#789).

`Status: queued` was printed above a table of ten legs `completed success`, two
running and two queued. The field is GitHub's own and the table is correct, so
nothing was invented — but the most prominent line of the output said the
opposite of what the run was doing, and it was read that way twice in one
session before anyone checked the jobs API.

These tests pin the *arithmetic and the discriminations*, not the prose. The
bar the repo applies: would this still pass if the code did nothing? Every
assertion here is written so the answer is no —

* the tally terms are parsed back out and summed against the leg count, so a
  state that quietly evaporates (the #445/#454 defect) fails the sum;
* zero legs on an unfinished run and zero legs on a finished one must render
  differently, or they collapse back into the one sentence #585 removed;
* a payload with no job list at all must decline, not report zero.
"""
from __future__ import annotations

import importlib.util
import json
import re
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


run = _load("presets/github/run.py", "github_run_789")
checks = _load("presets/_checks.py", "checks_789")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _job(name: str, status: str, conclusion: str | None = None) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion,
            "databaseId": 4242, "steps": []}


def _payload(status: str, conclusion: str | None, jobs: Any,
             omit_jobs: bool = False) -> dict:
    d: dict[str, Any] = {
        "databaseId": 30972816902, "name": "tests", "status": status,
        "conclusion": conclusion, "event": "push", "headBranch": "master",
        "url": "https://github.com/o/r/actions/runs/30972816902",
    }
    if not omit_jobs:
        d["jobs"] = jobs
    return d


def _render(monkeypatch, capsys, payload: dict) -> str:
    """Run the op end to end against a faked `gh`, return stdout."""
    def fake(argv, *a, **kw):
        if list(argv)[:2] == ["git", "rev-parse"]:
            return _Completed("master\n")
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(run.subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["run.py", "30972816902"])
    assert run.main() == 0
    return capsys.readouterr().out


def _status_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("Status:"):
            return line
    raise AssertionError(f"no Status: line in output:\n{out}")


_TERM = re.compile(r"(\d+) ([a-z_]+)")


def _sum_terms(line: str) -> tuple[int, int]:
    """`(declared total, sum of every term after it)` from a summarize() line.

    The arithmetic promise of `presets/_checks.py` made checkable from the
    outside: a state the tally forgets to name cannot hide behind a label.
    """
    m = re.search(r"(\d+) total: (.+)", line)
    assert m, f"no `N total:` tally in: {line}"
    total = int(m.group(1))
    tail = m.group(2).split("(")[0]
    return total, sum(int(n) for n, _ in _TERM.findall(tail))


# ---------------------------------------------------------------------------
# the reported run
# ---------------------------------------------------------------------------

_ISSUE_JOBS = (
    [_job(f"pytest (leg {i})", "completed", "success") for i in range(10)]
    + [_job("pytest (macos-latest, 3.10)", "in_progress"),
       _job("pytest (macos-latest, 3.12)", "in_progress"),
       _job("pytest (macos-latest, 3.11)", "queued"),
       _job("pytest (windows-latest, 3.11)", "queued")]
)


def test_the_reported_run_no_longer_leads_with_queued(monkeypatch, capsys) -> None:
    """#789 verbatim: ten legs green, run-level field `queued`."""
    line = _status_line(_render(monkeypatch, capsys,
                                _payload("queued", None, _ISSUE_JOBS)))

    assert not line.startswith("Status: queued"), (
        "the header still leads with the run-level field: " + line)
    assert "14 total" in line
    assert "10 passed" in line
    assert "0 failed" in line
    assert "4 pending" in line
    assert checks.NOT_GREEN in line

    # The raw field stays visible — the issue asks for qualified, not removed.
    assert "run-level field: queued" in line
    # …and it comes after the tally, not before it.
    assert line.index("14 total") < line.index("run-level field")


def test_the_reported_run_tally_sums_to_its_leg_count(monkeypatch, capsys) -> None:
    total, summed = _sum_terms(_status_line(
        _render(monkeypatch, capsys, _payload("queued", None, _ISSUE_JOBS))))
    assert total == 14
    assert summed == 14


# ---------------------------------------------------------------------------
# judgment call 2 — every leg state lands somewhere, and the terms sum
# ---------------------------------------------------------------------------

_MIXED = [
    _job("a", "completed", "success"),
    _job("b", "completed", "success"),
    _job("c", "completed", "cancelled"),
    _job("d", "completed", "cancelled"),
    _job("e", "completed", "skipped"),
    _job("f", "completed", "timed_out"),
    _job("g", "completed", "action_required"),
    _job("h", "completed", "neutral"),
    _job("i", "completed", "failure"),
    _job("j", "completed", "some_state_github_added_later"),
]


def test_no_leg_state_vanishes_from_the_tally(monkeypatch, capsys) -> None:
    """#445/#454, applied to a run's jobs instead of a PR's rollup."""
    line = _status_line(_render(monkeypatch, capsys,
                                _payload("completed", "failure", _MIXED)))
    total, summed = _sum_terms(line)
    assert total == 10
    assert summed == 10, f"terms do not sum to the leg count: {line}"

    assert "2 passed" in line
    # timed_out + action_required + failure are red, per _checks.FAILED_STATES.
    assert "3 failed" in line
    assert "0 pending" in line
    assert "2 cancelled" in line
    assert "1 skipped" in line
    assert "1 neutral" in line
    # A state nobody taught the module about surfaces under its own name.
    assert "1 some_state_github_added_later" in line
    assert checks.NOT_GREEN in line


# ---------------------------------------------------------------------------
# judgment call 1 — which leads when the two disagree
# ---------------------------------------------------------------------------

def test_completed_run_with_a_running_leg_reports_the_disagreement(
        monkeypatch, capsys) -> None:
    """The mirror image of #789. Neither source may be silently dropped."""
    jobs = [_job("a", "completed", "success"),
            _job("b", "completed", "success"),
            _job("c", "in_progress")]
    line = _status_line(_render(monkeypatch, capsys,
                                _payload("completed", "success", jobs)))

    assert "1 pending" in line
    assert "run-level field: completed" in line
    assert "UNKNOWN" in line, (
        "a finished run with an unfinished leg is not a settled answer: " + line)
    assert checks.NOT_GREEN in line


def test_unfinished_run_whose_read_legs_all_passed_does_not_claim_it_is_over(
        monkeypatch, capsys) -> None:
    """The tally cannot see legs GitHub has not created yet (`needs:`)."""
    jobs = [_job("a", "completed", "success"), _job("b", "completed", "success")]
    line = _status_line(_render(monkeypatch, capsys,
                                _payload("in_progress", None, jobs)))

    assert "2 passed" in line
    assert "not marked complete" in line or "more legs" in line, (
        "an unfinished run reading as finished is #789 pointed the other way: "
        + line)
    assert "run-level field: in_progress" in line


def test_a_finished_all_green_run_reads_green(monkeypatch, capsys) -> None:
    jobs = [_job("a", "completed", "success"), _job("b", "completed", "success")]
    line = _status_line(_render(monkeypatch, capsys,
                                _payload("completed", "success", jobs)))
    assert "2 total: 2 passed, 0 failed, 0 pending" in line
    assert checks.NOT_GREEN not in line
    assert "UNKNOWN" not in line
    assert "run-level field: completed" in line


# ---------------------------------------------------------------------------
# judgment call 3 — zero legs is three states, not one
# ---------------------------------------------------------------------------

def test_zero_legs_on_an_unfinished_run_states_the_absence(
        monkeypatch, capsys) -> None:
    line = _status_line(_render(monkeypatch, capsys,
                                _payload("queued", None, [])))
    assert "no legs yet" in line
    assert "run-level field: queued" in line
    # Nothing was tallied, so nothing may be asserted about green.
    assert "0 passed" not in line
    assert checks.NOT_GREEN not in line
    assert "zero legs ran" not in line


def test_zero_legs_on_a_finished_run_is_a_finding_not_a_wait(
        monkeypatch, capsys) -> None:
    """A run that finished having created no job tested nothing."""
    line = _status_line(_render(monkeypatch, capsys,
                                _payload("completed", "success", [])))
    assert "zero legs ran" in line
    assert checks.NOT_GREEN in line
    assert "no legs yet" not in line
    assert "still" not in line


def test_a_payload_with_no_job_list_declines(monkeypatch, capsys) -> None:
    """Missing input and an empty input are different answers."""
    line = _status_line(_render(monkeypatch, capsys,
                                _payload("completed", "success", None,
                                         omit_jobs=True)))
    assert "UNKNOWN" in line
    assert "no legs yet" not in line
    assert "zero legs ran" not in line
    assert " total:" not in line


# ---------------------------------------------------------------------------
# the rest of the header survives the reshape
# ---------------------------------------------------------------------------

def test_event_branch_url_and_the_job_table_are_untouched(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _payload("queued", None, _ISSUE_JOBS))
    assert "# Run #30972816902 — tests" in out
    assert "Event: push" in out
    assert "Branch: master" in out
    assert "URL: https://github.com/o/r/actions/runs/30972816902" in out
    assert "pytest (macos-latest, 3.11)" in out
    assert out.count("pytest (") >= 14


# ---------------------------------------------------------------------------
# the pure renderer, no gh
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,concl,states,must,must_not", [
    ("queued", None, ["SUCCESS"] * 10 + ["IN_PROGRESS"] * 2 + ["QUEUED"] * 2,
     ["14 total", "4 pending", "run-level field: queued"], ["Status:"]),
    ("completed", "success", ["SUCCESS"], ["1 total: 1 passed"], ["UNKNOWN"]),
    ("completed", "failure", ["FAILURE"], ["1 failed", checks.NOT_GREEN], []),
])
def test_status_line_is_pure_and_reusable(raw, concl, states, must, must_not) -> None:
    line = run.status_line(raw, concl, states)
    for token in must:
        assert token in line
    for token in must_not:
        assert token not in line


def test_status_line_distinguishes_none_from_empty() -> None:
    """`None` = could not establish. `[]` = established as zero."""
    declined = run.status_line("completed", "success", None)
    empty = run.status_line("completed", "success", [])
    assert declined != empty
    assert "UNKNOWN" in declined
    assert "zero legs ran" in empty


def test_job_states_length_always_matches_the_job_count() -> None:
    jobs = [_job("a", "completed", "success"), _job("b", "queued"), {}, "junk"]
    states = run.job_states(jobs)
    assert states is not None and len(states) == 4
    assert run.job_states("not a list") is None
    assert run.job_states(None) is None
