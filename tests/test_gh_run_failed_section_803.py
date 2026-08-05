"""`gh-run`'s failed-jobs section and `Duration` column must classify by the
shared predicate, not by literal string comparison (#803).

Both sites hardcoded a state list. `## Failed jobs (N)` and the ` <!` row
marker gated on `j_conclusion == "failure"`, so a leg that `timed_out`, was
`cancelled` or is `action_required` appeared in **no** failed-jobs section —
the one section a reader skips to in order to find out what broke. The
`Duration` column counted a step resolved only when its conclusion was one of
`success`/`failure`/`skipped`, so a job whose steps had all finished could
render `8/10 steps` and read as still working.

This is #445/#454 one section below the header #802 just fixed, so these tests
pin the same property that fix pinned, plus the one it created the risk of:

* every non-`failure` red state reaches the section, named by its own state;
* the section's arithmetic and the header's arithmetic are the *same* terms —
  two numbers on one screen that disagree is worse than one that was small;
* benign states (`SKIPPED`, `NEUTRAL`, `MANUAL`) and pending legs stay out —
  widening a section is only a fix if it does not start over-firing;
* the step count answers "how much is left to happen", so a resolved step is
  counted whatever its verdict, and an unreadable one is not counted at all.

The bar: would this still pass if the code did nothing? Every assertion below
is written so the answer is no.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


run = _load("presets/github/run.py", "github_run_803")
checks = _load("presets/_checks.py", "checks_803")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _step(name: str, conclusion: str | None, status: str = "completed") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


def _job(name: str, status: str, conclusion: str | None = None,
         steps: list[dict] | None = None, job_id: int = 4242) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion,
            "databaseId": job_id, "steps": steps if steps is not None else []}


def _payload(status: str, conclusion: str | None, jobs: Any) -> dict:
    return {
        "databaseId": 30972816902, "name": "tests", "status": status,
        "conclusion": conclusion, "event": "push", "headBranch": "master",
        "url": "https://github.com/o/r/actions/runs/30972816902",
        "jobs": jobs,
    }


def _render(monkeypatch, capsys, payload: dict) -> str:
    def fake(argv, *a, **kw):
        if list(argv)[:2] == ["git", "rev-parse"]:
            return _Completed("master\n")
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(run.subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["run.py", "30972816902"])
    assert run.main() == 0
    return capsys.readouterr().out


def _section(out: str) -> list[str]:
    """The failed-jobs section, heading first, or [] when it was not printed."""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## Failed jobs"):
            return lines[i:]
    return []


def _heading(out: str) -> str:
    sec = _section(out)
    assert sec, f"no failed-jobs section in output:\n{out}"
    return sec[0]


def _status_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("Status:"):
            return line
    raise AssertionError(f"no Status: line in output:\n{out}")


def _table_row(out: str, job_name: str) -> str:
    for line in out.splitlines():
        if line.startswith(job_name + " "):
            return line
    raise AssertionError(f"no table row for {job_name!r} in:\n{out}")


_TERM = re.compile(r"(\d+) ([a-z_]+)")


def _terms(text: str) -> dict[str, int]:
    """`{label: count}` for every `N label` term in a tally fragment."""
    return {lab: int(n) for n, lab in _TERM.findall(text)}


def _header_terms(out: str) -> dict[str, int]:
    line = _status_line(out)
    m = re.search(r"\d+ total: (.+)", line)
    assert m, f"no `N total:` tally in: {line}"
    return _terms(m.group(1).split("(")[0])


def _section_terms(out: str) -> dict[str, int]:
    head = _heading(out)
    m = re.search(r"\)\s*—\s*(.+)", head)
    assert m, (
        "the failed-jobs heading states no breakdown, so its count cannot be "
        f"reconciled with the header tally: {head}")
    return _terms(m.group(1))


def _section_count(out: str) -> int:
    m = re.search(r"## Failed jobs \((\d+)\)", _heading(out))
    assert m, _heading(out)
    return int(m.group(1))


def _listed_jobs(out: str) -> list[str]:
    names = []
    for line in _section(out)[1:]:
        m = re.match(r"  - (.+?) \(job #", line)
        if m:
            names.append(m.group(1))
    return names


# ---------------------------------------------------------------------------
# 1. every red state reaches the section
# ---------------------------------------------------------------------------

_ONE_OF_EACH = [
    _job("green", "completed", "success", job_id=1),
    _job("broke", "completed", "failure", job_id=2),
    _job("clock", "completed", "timed_out", job_id=3),
    _job("stopped", "completed", "cancelled", job_id=4),
    _job("waiting", "completed", "action_required", job_id=5),
]


def test_timed_out_cancelled_and_action_required_reach_the_section(
        monkeypatch, capsys) -> None:
    """#803 verbatim: one leg per non-`failure` red state, none may vanish."""
    out = _render(monkeypatch, capsys,
                  _payload("completed", "failure", _ONE_OF_EACH))
    listed = _listed_jobs(out)

    for name in ("broke", "clock", "stopped", "waiting"):
        assert name in listed, (
            f"{name!r} is red but appears in no failed-jobs section: "
            f"{listed}")
    assert "green" not in listed
    assert _section_count(out) == 4


def test_each_listed_leg_names_the_state_that_put_it_there(
        monkeypatch, capsys) -> None:
    """A `cancelled` leg under a heading that says "failed" must say so."""
    out = _render(monkeypatch, capsys,
                  _payload("completed", "failure", _ONE_OF_EACH))
    body = "\n".join(_section(out)[1:])
    for name, state in (("clock", "timed_out"), ("stopped", "cancelled"),
                        ("waiting", "action_required"), ("broke", "failure")):
        assert re.search(rf"  - {name} \(job #\d+\) — {state}\b", body), (
            f"{name!r} is not labelled {state!r} in the section:\n{body}")


def test_the_row_marker_fires_for_every_red_state(monkeypatch, capsys) -> None:
    """The ` <!` marker and section membership are one decision, not two."""
    out = _render(monkeypatch, capsys,
                  _payload("completed", "failure", _ONE_OF_EACH))
    for name in ("broke", "clock", "stopped", "waiting"):
        assert _table_row(out, name).rstrip().endswith("<!"), (
            f"row for {name!r} carries no marker: {_table_row(out, name)}")
    assert not _table_row(out, "green").rstrip().endswith("<!")


# ---------------------------------------------------------------------------
# 2. the section and the header cannot drift
# ---------------------------------------------------------------------------

_MIXED = [
    _job("a", "completed", "success", job_id=1),
    _job("b", "completed", "success", job_id=2),
    _job("c", "completed", "cancelled", job_id=3),
    _job("d", "completed", "cancelled", job_id=4),
    _job("e", "completed", "skipped", job_id=5),
    _job("f", "completed", "timed_out", job_id=6),
    _job("g", "completed", "action_required", job_id=7),
    _job("h", "completed", "neutral", job_id=8),
    _job("i", "completed", "failure", job_id=9),
    _job("j", "completed", "some_state_github_added_later", job_id=10),
    _job("k", "in_progress", None, job_id=11),
]


def test_section_terms_are_the_header_terms(monkeypatch, capsys) -> None:
    """Two numbers on one screen that disagree is worse than one too small."""
    out = _render(monkeypatch, capsys, _payload("completed", "failure", _MIXED))
    header = _header_terms(out)
    section = _section_terms(out)

    assert section, "the section publishes no terms to reconcile"
    for label, count in section.items():
        assert header.get(label) == count, (
            f"section says {count} {label}, header says "
            f"{header.get(label)} {label} — the two tallies have drifted\n"
            f"header:  {_status_line(out)}\nsection: {_heading(out)}")


def test_section_count_is_the_sum_of_its_own_terms(
        monkeypatch, capsys) -> None:
    """The arithmetic promise of `_checks`, applied one section down."""
    out = _render(monkeypatch, capsys, _payload("completed", "failure", _MIXED))
    assert _section_count(out) == sum(_section_terms(out).values())
    assert _section_count(out) == len(_listed_jobs(out))


def test_an_unknown_state_is_listed_under_its_own_name(
        monkeypatch, capsys) -> None:
    """A state nobody taught `_checks` about is red, and named, not folded."""
    out = _render(monkeypatch, capsys, _payload("completed", "failure", _MIXED))
    assert "j" in _listed_jobs(out)
    assert _section_terms(out).get("some_state_github_added_later") == 1


# ---------------------------------------------------------------------------
# 3. what this makes worse — the section must not over-fire
# ---------------------------------------------------------------------------

def test_benign_and_pending_legs_stay_out_of_the_section(
        monkeypatch, capsys) -> None:
    """A wider section is only a fix if it is still specific (#750)."""
    out = _render(monkeypatch, capsys, _payload("completed", "failure", _MIXED))
    listed = _listed_jobs(out)
    for name in ("a", "b", "e", "h", "k"):
        assert name not in listed, (
            f"{name!r} is not red but the section claims it: {listed}")
    for label in ("passed", "pending", "skipped", "neutral"):
        assert label not in _section_terms(out)


def test_an_all_green_run_prints_no_section_at_all(
        monkeypatch, capsys) -> None:
    jobs = [_job("a", "completed", "success"),
            _job("b", "completed", "skipped"),
            _job("c", "completed", "neutral"),
            _job("d", "queued")]
    out = _render(monkeypatch, capsys, _payload("in_progress", None, jobs))
    assert _section(out) == [], (
        "a run with no red leg printed a failed-jobs section:\n" + out)
    assert " <!" not in out


# ---------------------------------------------------------------------------
# 4. the failed-step lines under a red leg
# ---------------------------------------------------------------------------

def test_a_cancelled_leg_still_names_the_step_it_died_on(
        monkeypatch, capsys) -> None:
    """Listing only `failure` steps left a cancelled leg with no detail."""
    jobs = [_job("stopped", "completed", "cancelled", steps=[
        _step("checkout", "success"),
        _step("build", "cancelled"),
        _step("test", None, status="queued"),
    ], job_id=77)]
    out = _render(monkeypatch, capsys, _payload("completed", "cancelled", jobs))
    body = "\n".join(_section(out)[1:])
    assert "step: build" in body, (
        "the leg is listed with no step at all, so the section names the "
        f"failure without locating it:\n{body}")
    assert "checkout" not in body
    assert "test" not in body


def test_step_lines_are_capped_like_every_other_disclosure(
        monkeypatch, capsys) -> None:
    """A cancelled leg can carry dozens of cancelled steps (#605's cap)."""
    steps = [_step(f"s{i}", "cancelled") for i in range(12)]
    jobs = [_job("stopped", "completed", "cancelled", steps=steps, job_id=78)]
    out = _render(monkeypatch, capsys, _payload("completed", "cancelled", jobs))
    body = "\n".join(_section(out)[1:])
    assert body.count("step: ") <= checks.NAMED_CAP
    assert f"+{12 - checks.NAMED_CAP} more" in body


# ---------------------------------------------------------------------------
# 5. the Duration column counts progress, not verdicts
# ---------------------------------------------------------------------------

def test_a_fully_resolved_job_never_renders_as_partly_done(
        monkeypatch, capsys) -> None:
    """`8/10 steps` on a finished job reads as still working (#803)."""
    steps = [_step("a", "success"), _step("b", "failure"),
             _step("c", "skipped"), _step("d", "cancelled"),
             _step("e", "timed_out"), _step("f", "neutral"),
             _step("g", "action_required")]
    jobs = [_job("done", "completed", "cancelled", steps=steps, job_id=9)]
    out = _render(monkeypatch, capsys, _payload("completed", "cancelled", jobs))
    assert "7/7 steps" in _table_row(out, "done"), _table_row(out, "done")


def test_a_running_step_is_not_counted_as_resolved(
        monkeypatch, capsys) -> None:
    """The column would be worthless if it counted everything."""
    steps = [_step("a", "success"), _step("b", None, status="in_progress"),
             _step("c", None, status="queued")]
    jobs = [_job("moving", "in_progress", None, steps=steps, job_id=10)]
    out = _render(monkeypatch, capsys, _payload("in_progress", None, jobs))
    assert "1/3 steps" in _table_row(out, "moving"), _table_row(out, "moving")


def test_a_step_carrying_neither_field_is_not_counted_resolved(
        monkeypatch, capsys) -> None:
    """UNKNOWN is not progress. Counting it would guess in both directions."""
    steps = [_step("a", "success"), {"name": "b"}]
    jobs = [_job("partial", "in_progress", None, steps=steps, job_id=11)]
    out = _render(monkeypatch, capsys, _payload("in_progress", None, jobs))
    assert "1/2 steps" in _table_row(out, "partial"), _table_row(out, "partial")
