"""#1482 — `gh-run` named every leg and handed out an id for only the red ones.

The job table rendered name, status, conclusion and step progress. The job's
**id** appeared in exactly one place: the `## Failed jobs` section below the
table. So the op answered "which leg" for every leg and "what do I call it
next" for the failed ones only, and the reader whose question was about a leg
that *passed* — which git version does the ubuntu leg run — had to leave
supertool for `gh api`. That is #1409 one op over: a listing that names the
failing thing without naming the handle its sibling op needs.

Two properties are pinned, and the second is the reason this was not mechanical.

* **Every row carries its id**, not just the red ones, with the `gh-job:<id>`
  pointer under the table in the shape `gh-branch` already uses (#1409).
* **The namespace word is `job`, always, and never `check`.** `gh-job` answers
  for both id namespaces since #827 and they overlap, so `gh-pr:N:status`
  disambiguates with two literal words. This op's source cannot mint the second
  one: `gh run view --json jobs` reads
  `repos/{o}/{r}/actions/runs/{id}/jobs`, an Actions-jobs endpoint, and a check
  run is in no run's job list (`docs/presets/github.md`, the #827 table). So
  the label is not a guess here, it is established by the endpoint — and it is
  written out per row rather than left to the column header, because a row gets
  quoted on its own.

An id the payload did not carry renders `id unread` — not `job #?`, and above
all not `job #None`, which is what `.get("databaseId", "?")` produced for a key
present with a null value. An absence must not wear the shape of an id.

The bar: would this still pass if the code did nothing? Every assertion below
names the id of a leg that is **green**, which the old render never printed.
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


run = _load("presets/github/run.py", "github_run_1482")


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _job(name: str, status: str = "completed", conclusion: str | None = "success",
         **over: Any) -> dict:
    j = {"name": name, "status": status, "conclusion": conclusion,
         "databaseId": 94155891332, "steps": []}
    j.update(over)
    return j


def _payload(jobs: Any, status: str = "completed",
             conclusion: str | None = "success") -> dict:
    return {
        "databaseId": 30972816902, "name": "tests", "status": status,
        "conclusion": conclusion, "event": "push", "headBranch": "master",
        "url": "https://github.com/o/r/actions/runs/30972816902",
        "jobs": jobs,
    }


def _render(monkeypatch, capsys, payload: dict) -> str:
    def fake(argv, *a, **kw):
        # Only the run fetch gets the payload. A blanket fake hands the same
        # JSON to `_branch_locale`'s git calls, and the whole payload then
        # echoes back inside the `You are on:` line — which is how the
        # `not-an-id` assertion below passed on a string that was never
        # rendered as an id.
        if list(argv)[:3] == ["gh", "run", "view"]:
            return _Completed(json.dumps(payload))
        return _Completed("")

    monkeypatch.setattr(run.subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["run.py", "30972816902"])
    assert run.main() == 0
    return capsys.readouterr().out


def _table_rows(out: str) -> list[str]:
    """The job table's rows — everything between the rule and the next blank."""
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if set(ln) == {"-"}) + 1
    rows = []
    for ln in lines[start:]:
        if not ln.strip():
            break
        rows.append(ln)
    return rows


# ---------------------------------------------------------------------------
# the id is on every row, including the green ones
# ---------------------------------------------------------------------------

def test_a_passing_leg_carries_its_job_id(monkeypatch, capsys) -> None:
    """The #1482 case verbatim: the leg you need is the one that passed."""
    jobs = [_job("pytest (ubuntu-latest, 3.12)", databaseId=94155891332)]
    rows = _table_rows(_render(monkeypatch, capsys, _payload(jobs)))

    assert len(rows) == 1, rows
    assert "job #94155891332" in rows[0], (
        "a green leg's row names no id, so the route to gh-job is still a raw "
        "gh api call:\n" + rows[0])


def test_every_row_carries_an_id_not_only_the_red_ones(monkeypatch, capsys) -> None:
    jobs = [_job("green", databaseId=111),
            _job("pendin", status="in_progress", conclusion=None, databaseId=222),
            _job("red", conclusion="failure", databaseId=333)]
    out = _render(monkeypatch, capsys, _payload(jobs, conclusion="failure"))
    rows = _table_rows(out)

    assert len(rows) == 3, rows
    for row, ident in zip(rows, ("111", "222", "333")):
        assert f"job #{ident}" in row, f"{ident} missing from row:\n{row}"


def test_the_table_header_names_the_id_column(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _payload([_job("a")]))
    header = next(ln for ln in out.splitlines() if ln.startswith("Job "))
    assert "Job id" in header, header


def test_the_rule_under_the_header_is_as_wide_as_the_header(
        monkeypatch, capsys) -> None:
    """A rule frozen at the old width reads as a truncated table."""
    lines = _render(monkeypatch, capsys, _payload([_job("a")])).splitlines()
    i = next(i for i, ln in enumerate(lines) if set(ln) == {"-"})
    assert len(lines[i]) == len(lines[i - 1]), (lines[i - 1], lines[i])


# ---------------------------------------------------------------------------
# the pointer under the table
# ---------------------------------------------------------------------------

def test_the_table_points_at_gh_job(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _payload([_job("a")]))
    assert "gh-job:<id>" in out, (
        "the ids are printed with no statement of what reads them:\n" + out)


def test_no_pointer_when_there_are_no_jobs(monkeypatch, capsys) -> None:
    """A pointer at ids nobody was given is noise, and reads as a missing table."""
    out = _render(monkeypatch, capsys, _payload([]))
    assert "gh-job:<id>" not in out, out


# ---------------------------------------------------------------------------
# the namespace, which is the judgment call
# ---------------------------------------------------------------------------

def test_the_namespace_word_is_job_and_never_check(monkeypatch, capsys) -> None:
    """#827: a `check #` labelled `job #` sends the reader to a 404.

    The converse is what this op could get wrong, and it is settled by the
    endpoint rather than inferred per row — so no row may ever say `check`.
    """
    jobs = [_job("green"), _job("red", conclusion="failure")]
    out = _render(monkeypatch, capsys, _payload(jobs, conclusion="failure"))
    for row in _table_rows(out):
        assert "check #" not in row, row
    assert re.search(r"\bjob #\d+", out), out


def test_the_pointer_states_the_namespace_the_ids_belong_to(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _payload([_job("a")]))
    note = next(ln for ln in out.splitlines() if "gh-job:<id>" in ln)
    assert "check run" in note, (
        "the note hands over ids without saying which of GitHub's two id "
        "namespaces they are in:\n" + note)


# ---------------------------------------------------------------------------
# an absent id is an absence, not an id
# ---------------------------------------------------------------------------

def test_a_null_job_id_is_not_rendered_as_an_id(monkeypatch, capsys) -> None:
    """`.get("databaseId", "?")` returns None for a present-but-null key."""
    out = _render(monkeypatch, capsys,
                  _payload([_job("a", databaseId=None)]))
    assert "job #None" not in out, out
    assert "job #?" not in out, out
    assert "id unread" in _table_rows(out)[0], _table_rows(out)


def test_a_missing_job_id_key_is_not_rendered_as_an_id(monkeypatch, capsys) -> None:
    job = _job("a")
    del job["databaseId"]
    out = _render(monkeypatch, capsys, _payload([job]))
    assert "job #" not in out, out
    assert "id unread" in _table_rows(out)[0], _table_rows(out)


def test_a_red_leg_with_no_readable_id_says_so_in_the_failed_section(
        monkeypatch, capsys) -> None:
    """The section and the table render an id through one helper, so an
    absence cannot be honest in one place and a `None` in the other."""
    out = _render(monkeypatch, capsys,
                  _payload([_job("red", conclusion="failure", databaseId=None)],
                           conclusion="failure"))
    section = out.split("## Failed jobs")[1]
    assert "(id unread)" in section, section
    assert "None" not in section, section


def test_a_non_integer_job_id_is_refused(monkeypatch, capsys) -> None:
    """A string id would render as one and 404 on the op it points at."""
    out = _render(monkeypatch, capsys,
                  _payload([_job("a", databaseId="not-an-id")]))
    assert "not-an-id" not in out, out
    assert "id unread" in _table_rows(out)[0], _table_rows(out)


def test_the_helper_is_the_only_place_the_word_job_is_minted() -> None:
    """One renderer for both sites — the property the two tests above rely on."""
    assert run.job_id_cell({"databaseId": 7}) == "job #7"
    assert run.job_id_cell({"databaseId": None}) == "id unread"
    assert run.job_id_cell({}) == "id unread"
    assert run.job_id_cell("not a dict") == "id unread"
    assert run.job_id_cell({"databaseId": True}) == "id unread"
