"""#812 — `gl-mr`'s sibling blocks get #720's third state.

#720 gave the approvals line one line in all three states: an answer, a
verified `none`, and a stated `UNKNOWN` naming why. The other blocks in the
same render were never brought along, and they fail in two different shapes:

* **Pipeline** and **Comments** print a *wrong but plausible* value —
  `Pipeline: none` and `## Comments (0)` — when nobody could read the
  endpoint. A reader cannot tell those from the real thing, and "no pipeline"
  is a reason to merge while "we could not read the pipeline" is a reason not
  to.
* **Unresolved threads**, **Failed jobs**, **Files** and slim's **job list**
  vanish outright, so the absence of the line reads as the absence of the
  fact.

Everything here drives `main()` end to end with `subprocess.run` stubbed at
the `glab` boundary, for the reason #720's suite gives: the half of the defect
that matters is what happens to the *rest* of the dashboard. A test that only
checked one block would pass on a version that killed the render below it.

`glab` is not installed in the sandbox this was written in, so every GitLab
path here is exercised through the stub and none of it is evidence about a
live server.
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
_spec = importlib.util.spec_from_file_location("gitlab_mr_812", PRESET_PATH)
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
    "source_branch": "fix/812",
    "target_branch": "master",
    "description": "",
    "reviewers": [],
    "assignees": [],
    "web_url": "https://gitlab.example/foo/-/merge_requests/20881",
}

TIMEOUT = subprocess.TimeoutExpired(cmd=["glab"], timeout=10)
BAD_JSON = (0, "<html>502 Bad Gateway</html>", "")
FORBIDDEN = (1, "", "ERROR\n403 Forbidden\n")


def _cp(argv: list[str], rc: int, stdout: str, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=argv, returncode=rc, stdout=stdout, stderr=stderr)


# Healthy default for every endpoint the render touches. A test names only the
# endpoint it is about; everything else answers normally, so a decline in the
# output can only have come from the endpoint under test.
DEFAULTS: dict[str, Any] = {
    "/pipelines?": (0, json.dumps([{"status": "success", "id": 136900}]), ""),
    "/discussions": (0, "[]", ""),
    "/diffs": (0, "[]", ""),
    "/notes": (0, "[]", ""),
    "/approvals": (0, json.dumps({"approved_by": []}), ""),
    "/jobs?": (0, "[]", ""),
}


def _dispatch(payload: dict, endpoints: dict[str, Any]) -> Any:
    """`subprocess.run` stub keyed on the endpoint substring.

    A value is either a `(returncode, stdout, stderr)` triple or an exception
    instance to raise — the two ways a `glab api` call fails to answer.
    """
    table = {**DEFAULTS, **endpoints}
    mr_json = json.dumps(payload)

    def run(cmd: Any, *a: Any, **kw: Any) -> Any:
        argv = list(cmd)
        if argv and argv[0] == "glab" and "api" in argv:
            endpoint = str(argv[-1])
            for key, value in table.items():
                if key in endpoint:
                    if isinstance(value, BaseException):
                        raise value
                    rc, out, err = value
                    return _cp(argv, rc, out, err)
            return _cp(argv, 0, "[]")
        if argv and argv[0] == "glab":
            return _cp(argv, 0, mr_json)
        return _cp(argv, 0, "")
    return run


def _render(monkeypatch, capsys, *, payload: dict | None = None,
            flags: list[str] | None = None, **endpoints: Any) -> str:
    """Run the op and return stdout. Keyword args are endpoint overrides.

    Keys are spelled with `_` for `/` and `?` (`pipelines_` → `/pipelines?`)
    so they can be passed as keywords.
    """
    remap = {"pipelines": "/pipelines?", "discussions": "/discussions",
             "diffs": "/diffs", "notes": "/notes", "approvals": "/approvals",
             "jobs": "/jobs?"}
    table = {remap[k]: v for k, v in endpoints.items()}
    monkeypatch.setattr(
        mr.subprocess, "run", _dispatch({**BASE_MR, **(payload or {})}, table))
    monkeypatch.setattr(sys, "argv", ["mr.py", str(IID), *(flags or [])])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    return out


def _assert_render_complete(out: str) -> None:
    """A block that could not answer must not take its neighbours with it."""
    assert "## Description" in out
    assert "## Comments" in out
    assert "Pipeline:" in out
    assert "Approved by:" in out
    assert "Unresolved threads:" in out


# ---------------------------------------------------------------------------
# The healthy render is unchanged — the guard costs nothing in the case that
# happens every day.
# ---------------------------------------------------------------------------

def test_healthy_render_says_unknown_nowhere(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, payload={"changes_count": "2"},
                  diffs=(0, json.dumps([{"new_path": "a.py", "old_path": "a.py"}]), ""))
    assert "UNKNOWN" not in out
    assert "  ! " not in out
    assert "Pipeline: success (#136900)" in out
    assert "Unresolved threads: 0 / 0" in out
    assert "## Comments (0)" in out
    assert "## Files (2)" in out


# ---------------------------------------------------------------------------
# 1. Pipeline — the wrong-but-plausible value the issue names
# ---------------------------------------------------------------------------

def test_pipeline_declines_when_fetch_fails_and_payload_has_none(
        monkeypatch, capsys) -> None:
    """The exact render the issue describes: `Pipeline: none` where the truth
    is that nobody could tell."""
    out = _render(monkeypatch, capsys, pipelines=TIMEOUT)
    assert "Pipeline: UNKNOWN" in out
    assert "timed out" in out
    assert "Pipeline: none" not in out
    _assert_render_complete(out)


def test_pipeline_none_survives_as_a_verified_answer(monkeypatch, capsys) -> None:
    """An endpoint that answers with an empty list is an *answer*. `none` here
    is correct and must not be replaced by a decline."""
    out = _render(monkeypatch, capsys, pipelines=(0, "[]", ""))
    assert "Pipeline: none" in out
    assert "Pipeline: UNKNOWN" not in out


def test_pipeline_fallback_to_payload_discloses_that_it_is_stale(
        monkeypatch, capsys) -> None:
    """`head_pipeline` is a real status, so it is printed — but the live check
    declined, and the file's own comment says that field can be stale. Printing
    it as though it were fresh is the same trade in a quieter form."""
    out = _render(monkeypatch, capsys,
                  payload={"head_pipeline": {"status": "success", "id": 1234}},
                  pipelines=TIMEOUT)
    assert "Pipeline: success (#1234)" in out
    assert "stale" in out
    assert "timed out" in out


def test_pipeline_decline_names_a_non_zero_exit_distinctly(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, pipelines=FORBIDDEN)
    assert "Pipeline: UNKNOWN" in out
    assert "403 Forbidden" in out


def test_pipeline_decline_survives_glab_missing(monkeypatch, capsys) -> None:
    """OSError is listed on its own authority — #507 was filed as a silent
    decline and the fatal thing inside it was an unlisted OSError. The current
    handler catches TimeoutExpired and JSONDecodeError only."""
    out = _render(monkeypatch, capsys, pipelines=OSError("EMFILE"))
    assert "Pipeline: UNKNOWN" in out
    assert "EMFILE" in out
    _assert_render_complete(out)


# ---------------------------------------------------------------------------
# 2. Unresolved threads — vanishes today
# ---------------------------------------------------------------------------

def test_threads_line_is_printed_when_the_fetch_times_out(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, discussions=TIMEOUT)
    assert "Unresolved threads: UNKNOWN" in out
    assert "timed out" in out
    _assert_render_complete(out)


def test_threads_line_is_printed_on_a_non_zero_exit(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, discussions=FORBIDDEN)
    assert "Unresolved threads: UNKNOWN" in out
    assert "403 Forbidden" in out


def test_threads_line_is_printed_on_unparseable_json(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, discussions=BAD_JSON)
    assert "Unresolved threads: UNKNOWN" in out
    assert "parseable" in out


def test_threads_line_is_printed_when_the_payload_is_not_an_array(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, discussions=(0, json.dumps({"a": 1}), ""))
    assert "Unresolved threads: UNKNOWN" in out
    assert "dict" in out


def test_timeout_and_unparseable_do_not_share_a_sentence(
        monkeypatch, capsys) -> None:
    """The issue's judgment call. One is a retry, the other is a bug report;
    collapsing them is a smaller version of the mistake being fixed."""
    timed_out = _render(monkeypatch, capsys, discussions=TIMEOUT)
    garbage = _render(monkeypatch, capsys, discussions=BAD_JSON)
    a = [ln for ln in timed_out.splitlines() if ln.startswith("Unresolved")][0]
    b = [ln for ln in garbage.splitlines() if ln.startswith("Unresolved")][0]
    assert a != b


# ---------------------------------------------------------------------------
# 3. Failed jobs — asked for exactly when the pipeline failed
# ---------------------------------------------------------------------------

FAILED_PIPE = (0, json.dumps([{"status": "failed", "id": 77}]), "")


def test_failed_jobs_declines_rather_than_vanishing(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, pipelines=FAILED_PIPE, jobs=TIMEOUT)
    assert "Pipeline: failed (#77)" in out
    assert "Failed jobs: UNKNOWN" in out
    assert "timed out" in out
    _assert_render_complete(out)


def test_failed_jobs_zero_is_stated_not_silent(monkeypatch, capsys) -> None:
    """A failed pipeline whose jobs API reports nothing failed is a real and
    surprising answer. Printing nothing renders it as though the block were
    never asked for."""
    out = _render(monkeypatch, capsys, pipelines=FAILED_PIPE, jobs=(0, "[]", ""))
    assert "Failed jobs" in out
    assert "UNKNOWN" not in out


def test_failed_jobs_block_absent_when_pipeline_did_not_fail(
        monkeypatch, capsys) -> None:
    """A section prints nothing only when it was never asked for."""
    out = _render(monkeypatch, capsys)
    assert "Failed jobs" not in out


# ---------------------------------------------------------------------------
# 4. Files — vanishes today
# ---------------------------------------------------------------------------

def test_files_block_declines_when_the_diffs_fetch_fails(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, payload={"changes_count": "18"},
                  diffs=TIMEOUT)
    assert "## Files" in out
    assert "timed out" in out
    _assert_render_complete(out)


def test_files_block_declines_on_a_non_zero_exit(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, payload={"changes_count": "18"},
                  diffs=FORBIDDEN)
    assert "## Files" in out
    assert "403 Forbidden" in out


def test_partial_file_list_is_not_advertised_as_a_display_cap(
        monkeypatch, capsys) -> None:
    """`changes_count` says 18, one page arrived with 2 and the next call
    failed. The overflow line used to blame the display cap and point at
    `:full` — advice that cannot work, for a shortfall the tool caused."""
    calls = {"n": 0}
    # A full page, so pagination is obliged to ask for a second one.
    ok = (0, json.dumps([{"new_path": f"f{i}.py"} for i in range(100)]), "")

    monkeypatch.setattr(sys, "argv", ["mr.py", str(IID), "full"])
    inner = _dispatch({**BASE_MR, "changes_count": "250"}, {"/diffs": ok})

    def run(cmd: Any, *a: Any, **k: Any) -> Any:
        argv = list(cmd)
        if argv and argv[0] == "glab" and "api" in argv and "/diffs" in str(argv[-1]):
            calls["n"] += 1
            if calls["n"] > 1:
                raise TIMEOUT
        return inner(cmd, *a, **k)

    monkeypatch.setattr(mr.subprocess, "run", run)
    assert mr.main() == 0
    out = capsys.readouterr().out
    assert "## Files" in out
    assert "timed out" in out
    assert "f0.py" in out


def test_files_block_absent_when_the_mr_changes_nothing(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, payload={"changes_count": "0"})
    assert "## Files" not in out


# ---------------------------------------------------------------------------
# 5. Comments — prints `(0)` today, which is a claim, not an absence
# ---------------------------------------------------------------------------

def test_comments_count_is_not_reported_as_zero_when_unreadable(
        monkeypatch, capsys) -> None:
    """The issue files this one as a vanishing section; it is not. The header
    is unconditional, so the failure renders as `## Comments (0)` — the
    `Pipeline: none` defect wearing a different hat, and the worse of the two."""
    out = _render(monkeypatch, capsys, notes=TIMEOUT)
    assert "## Comments (0)" not in out
    assert "## Comments" in out
    assert "timed out" in out


def test_comments_zero_survives_as_a_verified_answer(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, notes=(0, "[]", ""))
    assert "## Comments (0)" in out


def test_comments_decline_names_unparseable_json(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, notes=BAD_JSON)
    assert "## Comments (0)" not in out
    assert "parseable" in out


# ---------------------------------------------------------------------------
# 6. Slim mode carries the same two defects — the issue counts neither
# ---------------------------------------------------------------------------

def test_slim_pipeline_declines(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, flags=["status"], pipelines=TIMEOUT)
    assert "pipeline: none" not in out
    assert "UNKNOWN" in out
    assert "timed out" in out
    assert f"!{IID}" in out


def test_slim_named_jobs_decline(monkeypatch, capsys) -> None:
    """Slim is the poll-loop render — the one read most often and looked at
    least closely, which makes a silently missing job list worth more here."""
    out = _render(monkeypatch, capsys, flags=["status"],
                  pipelines=FAILED_PIPE, jobs=TIMEOUT)
    assert "pipeline: failed (#77)" in out
    assert "UNKNOWN" in out


def test_slim_names_an_empty_non_passing_job_list(monkeypatch, capsys) -> None:
    """The list is only fetched once the caller has decided this pipeline is
    worth naming legs for. Coming back with nothing to name is an answer worth
    one line — silence there is the same absence-for-an-absence trade."""
    out = _render(monkeypatch, capsys, flags=["status"],
                  pipelines=(0, json.dumps([{"status": "canceled", "id": 88}]), ""),
                  jobs=(0, "[]", ""))
    assert "pipeline: canceled (#88)" in out
    assert "none non-passing" in out
    assert "UNKNOWN" not in out


def test_a_complete_looking_file_list_still_says_it_may_be_short(
        monkeypatch, capsys) -> None:
    """The nastiest of the file-list shapes: `changes_count` happens to equal
    what arrived, so there is no `+N more` line to carry the warning and the
    block looks whole. It is not — a later page failed."""
    calls = {"n": 0}
    page = (0, json.dumps([{"new_path": f"f{i}.py"} for i in range(100)]), "")
    inner = _dispatch({**BASE_MR, "changes_count": "100"}, {"/diffs": page})

    def run(cmd: Any, *a: Any, **k: Any) -> Any:
        argv = list(cmd)
        if argv and argv[0] == "glab" and "api" in argv and "/diffs" in str(argv[-1]):
            calls["n"] += 1
            if calls["n"] > 1:
                raise TIMEOUT
        return inner(cmd, *a, **k)

    monkeypatch.setattr(mr.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["mr.py", str(IID), "full"])
    assert mr.main() == 0
    out = capsys.readouterr().out
    assert "## Files" in out
    assert "may be incomplete" in out
    assert "timed out" in out


def test_slim_healthy_render_is_unchanged(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, flags=["status"])
    assert "pipeline: success (#136900)" in out
    assert "UNKNOWN" not in out


# ---------------------------------------------------------------------------
# 7. The abstraction, exercised directly — the reason strings are the product
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outcome,expect", [
    (TIMEOUT, "timed out"),
    (BAD_JSON, "parseable"),
    (FORBIDDEN, "403 Forbidden"),
    (OSError("ENOMEM"), "ENOMEM"),
])
def test_fetch_json_names_each_failure_distinctly(monkeypatch, outcome, expect) -> None:
    def run(cmd: Any, *a: Any, **k: Any) -> Any:
        if isinstance(outcome, BaseException):
            raise outcome
        rc, out, err = outcome
        return _cp(list(cmd), rc, out, err)
    monkeypatch.setattr(mr.subprocess, "run", run)
    data, reason = mr._fetch_json("projects/:id/x", "widgets")
    assert data is None
    assert reason is not None
    assert expect in reason
    assert "widgets" in reason or "glab" in reason


def test_fetch_json_returns_the_payload_when_it_works(monkeypatch) -> None:
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda cmd, *a, **k: _cp(list(cmd), 0, json.dumps([1, 2]), ""))
    data, reason = mr._fetch_json("projects/:id/x", "widgets")
    assert data == [1, 2]
    assert reason is None


def test_approvals_line_keeps_its_720_wording(monkeypatch) -> None:
    """#720's messages are the contract this fix generalises, not a detail it
    is free to reword on the way past."""
    monkeypatch.setattr(
        mr.subprocess, "run",
        lambda cmd, *a, **k: (_ for _ in ()).throw(TIMEOUT))
    assert mr._approvals_line(IID) == "Approved by: UNKNOWN — approvals API timed out"
