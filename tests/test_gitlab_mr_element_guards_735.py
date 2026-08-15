"""`gl-mr` must survive an element of a remote array that is not an object (#735).

#720 fixed the *top-level* type check on two `json.loads` sites in
`presets/gitlab/mr.py`. The sites here check the top-level type and then trust
every element inside it: `isinstance(jobs, list)` followed by `job.get("id")`.

No heterogeneous array has been observed from GitLab and every endpoint is
documented as returning an array of objects, so these tests are the only
evidence the guards work. They therefore run `main()` end to end rather than
stubbing the parse: the half of the defect that matters is what happens to the
*rest* of the dashboard. An `AttributeError` in the discussions loop takes the
pipeline, files, description and comments sections with it, so every test
asserts the sections *below* the damaged one still render.

Each test also asserts the disclosure line. A guard that skips silently turns a
loud crash into a quiet undercount — "9 threads" when 12 came back — which is
this repo's most-filed defect class (`docs/validators.md`, "Declining instead
of guessing"). Asserting only "no exception" would pass on that version.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "mr.py"
_spec = importlib.util.spec_from_file_location("gitlab_mr_735", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)


IID = 20881

BASE_MR = {
    "iid": IID,
    "title": "Something",
    "state": "opened",
    "merge_status": "can_be_merged",
    "has_conflicts": False,
    "source_branch": "fix/735",
    "target_branch": "master",
    "description": "body",
    "changes_count": "3",
    "reviewers": [{"username": "alice"}],
    "assignees": [{"username": "bob"}],
    "head_pipeline": {"status": "failed", "id": 136900},
    "web_url": "https://gitlab.example/foo/-/merge_requests/20881",
}

GOOD = {
    "approvals": json.dumps({"approved_by": []}),
    "pipelines": json.dumps([{"status": "failed", "id": 136900}]),
    "discussions": json.dumps([
        {"notes": [{"resolvable": True, "resolved": True}]},
        {"notes": [{"resolvable": True, "resolved": False}]},
    ]),
    "diffs": json.dumps([{"new_path": "a.py"}, {"new_path": "b.py"}]),
    "failed_jobs": json.dumps([{"id": 1, "name": "phpstan", "stage": "test"}]),
    "notes": json.dumps([{"body": "hi", "author": {"username": "carol"},
                          "created_at": "2026-01-01T00:00:00Z"}]),
    "branch_mrs_open": json.dumps([{"iid": IID}]),
    "branch_mrs": json.dumps([{"iid": IID}]),
}


def _cp(argv: list[str], rc: int, stdout: str, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(args=argv, returncode=rc, stdout=stdout, stderr=stderr)


def _classify(url: str) -> str:
    """Which of gl-mr's fetches this `glab api` URL is."""
    if url.endswith("/approvals"):
        return "approvals"
    if "/pipelines?" in url:
        return "pipelines"
    if "/discussions" in url:
        return "discussions"
    if "/diffs?" in url:
        return "diffs"
    if "scope=failed" in url:
        return "failed_jobs"
    if "/jobs?" in url:
        return "named_jobs"
    if "/notes?" in url:
        return "notes"
    if "source_branch=" in url:
        return "branch_mrs_open" if "state=opened" in url else "branch_mrs"
    return "other"


def _dispatch(overrides: dict[str, str], mr_payload: str) -> Any:
    def run(cmd: Any, *a: Any, **kw: Any) -> Any:
        argv = list(cmd)
        if argv and argv[0] == "git":
            return _cp(argv, 1, "", "")
        if "api" in argv:
            kind = _classify(str(argv[-1]))
            body = overrides.get(kind, GOOD.get(kind, "[]"))
            return _cp(argv, 0, body)
        if argv and argv[0] == "glab":
            return _cp(argv, 0, mr_payload)
        return _cp(argv, 0, "")
    return run


def _render(monkeypatch, capsys, *, overrides: dict[str, str] | None = None,
            mr_payload: dict | None = None, arg: str = str(IID)) -> tuple[int, str]:
    payload = json.dumps({**BASE_MR, **(mr_payload or {})})
    monkeypatch.setattr(mr.subprocess, "run", _dispatch(overrides or {}, payload))
    monkeypatch.setattr(sys, "argv", ["mr.py", arg])
    rc = mr.main()
    return rc, capsys.readouterr().out


def _assert_tail_intact(out: str) -> None:
    """Everything the discussions loop sits above (#720's own post-condition)."""
    assert "Pipeline:" in out, out
    assert "## Files" in out, out
    assert "## Description" in out, out
    assert "## Comments" in out, out


# ---------------------------------------------------------------------------
# Baseline — the healthy render, so the guards are not asserted against a
# version where the whole block was simply dropped.
# ---------------------------------------------------------------------------

def test_healthy_render_is_complete_and_says_nothing_about_shapes(monkeypatch, capsys) -> None:
    rc, out = _render(monkeypatch, capsys)
    assert rc == 0
    assert "Unresolved threads: 1 / 2" in out
    assert "Reviewers: alice" in out
    assert "Assignees: bob" in out
    assert "Failed jobs (1):" in out
    assert "## Comments (1)" in out
    _assert_tail_intact(out)
    assert "could not read" not in out


# ---------------------------------------------------------------------------
# One site per test. `None`, a bare string and a nested list — the three shapes
# a JSON array can hold where an object was documented.
# ---------------------------------------------------------------------------

def test_discussions_with_unreadable_elements_still_renders_the_tail(monkeypatch, capsys) -> None:
    bad = json.dumps([
        {"notes": [{"resolvable": True, "resolved": False}]},
        None,
        "surprise",
    ])
    rc, out = _render(monkeypatch, capsys, overrides={"discussions": bad})
    assert rc == 0
    assert "Unresolved threads: 1 / 1" in out
    assert "2 of 3 discussions had a shape supertool could not read" in out
    _assert_tail_intact(out)


def test_discussion_notes_that_are_not_objects_are_disclosed(monkeypatch, capsys) -> None:
    """The inner list too — `dd.get("notes")` yields the elements `n.get()` runs on."""
    bad = json.dumps([{"notes": [{"resolvable": True, "resolved": False}, None, ["x"]]}])
    rc, out = _render(monkeypatch, capsys, overrides={"discussions": bad})
    assert rc == 0
    assert "Unresolved threads: 1 / 1" in out
    assert "notes had a shape supertool could not read" in out
    _assert_tail_intact(out)


def test_discussion_notes_field_that_is_not_a_list_is_disclosed(monkeypatch, capsys) -> None:
    """`(dd.get("notes") or [])` accepts a string and iterates its characters."""
    bad = json.dumps([{"notes": "oops"}])
    rc, out = _render(monkeypatch, capsys, overrides={"discussions": bad})
    assert rc == 0
    assert "could not read" in out
    _assert_tail_intact(out)


def test_failed_jobs_with_unreadable_elements_still_renders_the_tail(monkeypatch, capsys) -> None:
    bad = json.dumps([{"id": 1, "name": "phpstan", "stage": "test"}, None, "x"])
    rc, out = _render(monkeypatch, capsys, overrides={"failed_jobs": bad})
    assert rc == 0
    assert "Failed jobs (1):" in out
    assert "#1 | phpstan | test" in out
    assert "2 of 3 failed jobs had a shape supertool could not read" in out
    _assert_tail_intact(out)


def test_notes_with_unreadable_elements_still_renders_the_comment_count(monkeypatch, capsys) -> None:
    bad = json.dumps([
        {"body": "hi", "author": {"username": "carol"}, "created_at": "2026-01-01T00:00:00Z"},
        None,
        ["nested"],
    ])
    rc, out = _render(monkeypatch, capsys, overrides={"notes": bad})
    assert rc == 0
    assert "## Comments (1)" in out
    assert "2 of 3 comments had a shape supertool could not read" in out
    assert "**carol**" in out


def test_diffs_with_unreadable_elements_still_renders_the_files_block(monkeypatch, capsys) -> None:
    bad = json.dumps([{"new_path": "a.py"}, None, "b.py"])
    rc, out = _render(monkeypatch, capsys, overrides={"diffs": bad})
    assert rc == 0
    assert "## Files" in out
    assert "a.py" in out
    assert "2 of 3 changed files had a shape supertool could not read" in out
    assert "## Description" in out
    assert "## Comments" in out


def test_reviewers_with_unreadable_elements_still_renders_the_tail(monkeypatch, capsys) -> None:
    """`reviewers` is an array inside the MR object, not its own json.loads —
    same defect, and it renders *above* the discussions loop."""
    rc, out = _render(monkeypatch, capsys,
                      mr_payload={"reviewers": [{"username": "alice"}, None, "bob"]})
    assert rc == 0
    assert "Reviewers: alice" in out
    assert "2 of 3 reviewers had a shape supertool could not read" in out
    _assert_tail_intact(out)


def test_assignees_with_unreadable_elements_still_renders_the_tail(monkeypatch, capsys) -> None:
    rc, out = _render(monkeypatch, capsys,
                      mr_payload={"assignees": [None, {"username": "bob"}]})
    assert rc == 0
    assert "Assignees: bob" in out
    assert "1 of 2 assignees had a shape supertool could not read" in out
    _assert_tail_intact(out)


def test_reviewers_field_that_is_not_an_array_is_disclosed_not_reported_as_none(
        monkeypatch, capsys) -> None:
    """The trap the guard itself creates: `Reviewers: none` is a claim about the
    MR. It must not be printed about a payload that could not be read."""
    rc, out = _render(monkeypatch, capsys, mr_payload={"reviewers": "alice"})
    assert rc == 0
    assert "1 of 1 reviewers had a shape supertool could not read" in out
    _assert_tail_intact(out)


def test_absent_reviewers_stay_a_silent_none(monkeypatch, capsys) -> None:
    """An absent field is a real empty answer, not an unreadable one."""
    rc, out = _render(monkeypatch, capsys, mr_payload={"reviewers": None})
    assert rc == 0
    assert "Reviewers: none" in out
    assert "reviewers had a shape" not in out


def test_latest_pipeline_element_that_is_not_an_object_falls_back(monkeypatch, capsys) -> None:
    """`pipes[0]` was returned unchecked and `.get` ran on it four lines later."""
    rc, out = _render(monkeypatch, capsys, overrides={"pipelines": json.dumps(["nope"])})
    assert rc == 0
    assert "Pipeline: failed (#136900)" in out
    _assert_tail_intact(out)


def test_pipeline_user_that_is_not_an_object_still_renders(monkeypatch, capsys) -> None:
    """`(pipeline.get("user") or {}).get(...)` guards null and nothing else."""
    bad = json.dumps([{"status": "failed", "id": 136900, "user": "alice"}])
    rc, out = _render(monkeypatch, capsys, overrides={"pipelines": bad})
    assert rc == 0
    assert "Pipeline: failed (#136900)" in out
    _assert_tail_intact(out)


def test_mr_payload_that_is_not_an_object_declines_instead_of_crashing(monkeypatch, capsys) -> None:
    """`glab mr view --output json` was parsed and used with no type check at all
    — the top-level gap #720 fixed elsewhere in this file, still open here."""
    monkeypatch.setattr(mr.subprocess, "run", _dispatch({}, "[]"))
    monkeypatch.setattr(sys, "argv", ["mr.py", str(IID)])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "ERROR" in out
    assert "list" in out


def test_branch_lookup_element_that_is_not_an_object_declines(monkeypatch, capsys) -> None:
    """`mrs[0].get("iid")` on a heterogeneous array. The decline must not claim
    'no MR found' — one came back, it just could not be read."""
    rc, out = _render(monkeypatch, capsys, arg="fix/735",
                      overrides={"branch_mrs_open": json.dumps([None]),
                                 "branch_mrs": json.dumps([None])})
    assert rc == 1
    assert "could not read" in out
    assert "no MR found" not in out


def test_branch_lookup_declines_rather_than_falling_through_to_another_mr(
        monkeypatch, capsys) -> None:
    """The open-MR lookup returned something unreadable and the all-states
    lookup returns a *different* MR. Falling through renders !999 as though it
    were the answer to a question about the open MR — a wrong answer, which is
    worse than the crash this guard replaced."""
    rc, out = _render(monkeypatch, capsys, arg="fix/735",
                      overrides={"branch_mrs_open": json.dumps([None]),
                                 "branch_mrs": json.dumps([{"iid": 999}])})
    assert rc == 1
    assert "could not read" in out
    assert "!999" not in out


# ---------------------------------------------------------------------------
# `gl-mr:N:status` — the slim render, and the one site that already guarded its
# elements. It skipped them silently; it now says how many it dropped.
# ---------------------------------------------------------------------------

def test_pipeline_leg_lines_skips_unreadable_elements(monkeypatch) -> None:
    jobs = json.dumps([
        {"status": "failed", "name": "phpstan", "id": 7},
        None,
        "surprise",
    ])
    monkeypatch.setattr(mr, "_glab_api",
                        lambda *a, **kw: _cp(["glab"], 0, jobs))
    lines = mr._pipeline_leg_lines(136900)
    assert "  failed: phpstan (job #7)" in lines
    assert any("2 of 3 pipeline jobs had a shape supertool could not read" in ln
               for ln in lines)
    # #1607: the note says two were dropped, and the tally has to agree with
    # it. A tally of 1 beside a note about 3 is the undercount this file was
    # opened over, moved one line up.
    assert "  legs: 3 total: 0 passed, 1 failed, 0 pending, 2 unknown" in lines[0]


def test_pipeline_leg_lines_says_nothing_when_every_element_is_readable(
        monkeypatch) -> None:
    jobs = json.dumps([{"status": "failed", "name": "phpstan", "id": 7}])
    monkeypatch.setattr(mr, "_glab_api",
                        lambda *a, **kw: _cp(["glab"], 0, jobs))
    # No `!` disclosure line — nothing was dropped. The tally above the named
    # job is #1607 and is unconditional; the guard's own output is still the
    # empty set it was.
    assert mr._pipeline_leg_lines(136900)[1:] == ["  failed: phpstan (job #7)"]
    assert not any(ln.startswith("  !") for ln in mr._pipeline_leg_lines(136900))


def test_slim_render_survives_unreadable_pipeline_jobs(monkeypatch, capsys) -> None:
    """`gl-mr:N:status` is the poll-loop form — a crash here is a poll that
    stops answering."""
    bad = json.dumps([{"status": "failed", "name": "phpstan", "id": 7}, None])
    monkeypatch.setattr(mr.subprocess, "run",
                        _dispatch({"named_jobs": bad}, json.dumps(BASE_MR)))
    monkeypatch.setattr(sys, "argv", ["mr.py", str(IID), "status"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "pipeline: failed (#136900)" in out
    assert "1 of 2 pipeline jobs had a shape supertool could not read" in out
    assert "merged_at:" in out


# ---------------------------------------------------------------------------
# The helpers, directly — the shared contract the sites above are built on.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seq,kept,skipped", [
    ([], 0, 0),
    ([{"a": 1}], 1, 0),
    ([{"a": 1}, None, "x", ["y"], 3], 1, 4),
    ("not a list", 0, 0),
    (None, 0, 0),
    ({"a": 1}, 0, 0),
])
def test_dict_elements_partitions_and_counts(seq, kept, skipped) -> None:
    got_kept, got_skipped = mr._dict_elements(seq)
    assert len(got_kept) == kept
    assert got_skipped == skipped


@pytest.mark.parametrize("value,kept,bad,total", [
    (None, 0, 0, 0),
    ([], 0, 0, 0),
    ([{"a": 1}, None], 1, 1, 2),
    ("alice", 0, 1, 1),
    ({"a": 1}, 0, 1, 1),
    (7, 0, 1, 1),
])
def test_array_elements_counts_a_non_array_as_one_unreadable(value, kept, bad, total) -> None:
    got_kept, got_bad, got_total = mr._array_elements(value)
    assert (len(got_kept), got_bad, got_total) == (kept, bad, total)


def test_unreadable_is_silent_when_nothing_was_skipped() -> None:
    assert mr._unreadable(0, 12, "discussions") == ""


def test_unreadable_names_both_numbers_and_the_noun() -> None:
    line = mr._unreadable(3, 12, "discussions")
    assert "3 of 12 discussions" in line
    assert "could not read" in line


@pytest.mark.parametrize("value,expected", [
    ({"a": 1}, {"a": 1}),
    (None, {}),
    ("alice", {}),
    ([{"a": 1}], {}),
    (7, {}),
])
def test_as_dict_rejects_everything_that_is_not_an_object(value, expected) -> None:
    assert mr._as_dict(value) == expected
