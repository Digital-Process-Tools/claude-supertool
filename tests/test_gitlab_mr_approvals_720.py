"""gl-mr approvals — three states, and the render survives all of them (#720).

GitLab documents `GET /projects/:id/merge_requests/:iid/approvals` as returning
a JSON **object** carrying `approved_by`, on every tier including Free. So every
state exercised here is something other than a healthy GitLab answering, and
none of them mean "nobody approved this MR".

The tests run `main()` end to end rather than stubbing the parse, because the
half of the defect that matters is what happens to the *rest* of the dashboard:
`Approved by:` sits above the threads, pipeline, files, description and comments
sections, and an exception raised there takes all of them with it. A test that
only checked the approvals line would pass on a version that still killed the
render two lines later.
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
_spec = importlib.util.spec_from_file_location("gitlab_mr_720", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)


MR_PAYLOAD = json.dumps({
    "iid": 20881,
    "title": "Something",
    "state": "opened",
    "merge_status": "can_be_merged",
    "has_conflicts": False,
    "source_branch": "fix/720",
    "target_branch": "master",
    "description": "",
    "reviewers": [],
    "assignees": [],
    "head_pipeline": {"status": "success", "id": 136900},
    "web_url": "https://gitlab.example/foo/-/merge_requests/20881",
})


def _cp(argv: list[str], rc: int, stdout: str, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(args=argv, returncode=rc, stdout=stdout, stderr=stderr)


def _dispatch(approvals: Any) -> Any:
    """subprocess.run stub.

    `glab mr view` gets the MR payload, `glab api .../approvals` gets whatever
    this test is about, every other `glab api` call gets an empty JSON list, and
    git calls get a clean empty answer. `approvals` is either a
    `(returncode, stdout, stderr)` triple or an exception instance to raise.
    """
    def run(cmd: Any, *a: Any, **kw: Any) -> Any:
        argv = list(cmd)
        if "api" in argv and str(argv[-1]).endswith("/approvals"):
            if isinstance(approvals, BaseException):
                raise approvals
            rc, out, err = approvals
            return _cp(argv, rc, out, err)
        if "api" in argv:
            return _cp(argv, 0, "[]")
        if argv and argv[0] == "glab":
            return _cp(argv, 0, MR_PAYLOAD)
        return _cp(argv, 0, "")
    return run


def _render(monkeypatch, capsys, approvals: Any) -> str:
    monkeypatch.setattr(mr.subprocess, "run", _dispatch(approvals))
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    rc = mr.main()
    out = capsys.readouterr().out
    assert rc == 0
    return out


def _assert_render_complete(out: str) -> None:
    """Everything that renders *after* the approvals line must still be there."""
    assert "## Description" in out
    assert "## Comments" in out
    assert "Pipeline:" in out


# ---------------------------------------------------------------------------
# The two answers approvals is allowed to give
# ---------------------------------------------------------------------------

def test_approvals_lists_approvers(monkeypatch, capsys) -> None:
    body = json.dumps({"approved_by": [
        {"user": {"username": "alice"}},
        {"user": {"username": "bob"}},
    ]})
    out = _render(monkeypatch, capsys, (0, body, ""))
    assert "Approved by: alice, bob" in out
    assert "UNKNOWN" not in out


def test_approvals_empty_list_is_a_verified_none(monkeypatch, capsys) -> None:
    """An object that says the list is empty is an *answer*, and stays 'none'."""
    out = _render(monkeypatch, capsys, (0, json.dumps({"approved_by": []}), ""))
    assert "Approved by: none" in out


# ---------------------------------------------------------------------------
# The third state — could not tell. Was two silences and one crash.
# ---------------------------------------------------------------------------

def test_non_dict_payload_declines_instead_of_crashing(monkeypatch, capsys) -> None:
    """A JSON array where an object was documented: `.get` on a list raised
    AttributeError and killed every section below this one."""
    out = _render(monkeypatch, capsys, (0, "[]", ""))
    assert "Approved by: UNKNOWN" in out
    assert "list" in out
    assert "Approved by: none" not in out
    _assert_render_complete(out)


def test_json_null_payload_declines_instead_of_crashing(monkeypatch, capsys) -> None:
    """`null` parses fine and is not a dict — same crash, different shape."""
    out = _render(monkeypatch, capsys, (0, "null", ""))
    assert "Approved by: UNKNOWN" in out
    _assert_render_complete(out)


def test_api_failure_declines_instead_of_vanishing(monkeypatch, capsys) -> None:
    """Reproduced on this machine in one command: an unauthenticated `glab api`
    exits 1 with empty stdout, and the whole `Approved by:` line used to
    disappear — while `Reviewers:` and `Assignees:` next to it print `none`
    precisely so that absence is signal."""
    out = _render(monkeypatch, capsys, (1, "", "ERROR\n\nUnauthenticated.\n"))
    assert "Approved by: UNKNOWN" in out
    assert "Unauthenticated." in out
    assert "Approved by: none" not in out
    _assert_render_complete(out)


def test_unparseable_body_declines_instead_of_vanishing(monkeypatch, capsys) -> None:
    """An HTML login page from an SSO gateway, 200 and all."""
    out = _render(monkeypatch, capsys, (0, "<html>login</html>", ""))
    assert "Approved by: UNKNOWN" in out
    assert "JSON" in out
    _assert_render_complete(out)


def test_timeout_declines_instead_of_vanishing(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys,
                  subprocess.TimeoutExpired(cmd="glab", timeout=10))
    assert "Approved by: UNKNOWN" in out
    assert "timed out" in out
    _assert_render_complete(out)


def test_object_without_approved_by_is_not_reported_as_none(monkeypatch, capsys) -> None:
    """`approved_by` is documented as always present. An object without it is
    not an approvals response — defaulting to `[]` printed 'none', which is a
    wrong answer rather than a missing one."""
    out = _render(monkeypatch, capsys, (0, json.dumps({"message": "404 Not found"}), ""))
    assert "Approved by: UNKNOWN" in out
    assert "approved_by" in out
    assert "Approved by: none" not in out
    _assert_render_complete(out)


def test_approved_by_wrong_type_is_not_reported_as_none(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, (0, json.dumps({"approved_by": {}}), ""))
    assert "Approved by: UNKNOWN" in out
    assert "Approved by: none" not in out


def test_malformed_approver_entries_do_not_crash(monkeypatch, capsys) -> None:
    """A list of the right shape with junk inside degrades to '?' per entry —
    the count is still information, and it is not a decline."""
    body = json.dumps({"approved_by": ["nope", {"user": None}, {"user": {"username": "carol"}}]})
    out = _render(monkeypatch, capsys, (0, body, ""))
    assert "Approved by: ?, ?, carol" in out
    _assert_render_complete(out)


# ---------------------------------------------------------------------------
# The helper on its own — the line is a value, so it can be asserted exactly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("approvals,expected_fragment", [
    ((0, json.dumps({"approved_by": []}), ""), "Approved by: none"),
    ((0, "[]", ""), "UNKNOWN"),
    ((1, "", "boom"), "UNKNOWN"),
])
def test_approvals_line_never_raises(monkeypatch, approvals, expected_fragment) -> None:
    monkeypatch.setattr(mr.subprocess, "run", _dispatch(approvals))
    line = mr._approvals_line(20881)
    assert expected_fragment in line
    assert "\n" not in line


def test_approvals_line_survives_oserror(monkeypatch) -> None:
    """#507's precedent: the fatal failure hiding inside the quiet one was an
    OSError nobody had listed. A decline that only covers the exceptions
    already named is the same bug waiting for a different errno."""
    def boom(*a: Any, **kw: Any) -> Any:
        raise OSError("[Errno 24] Too many open files")
    monkeypatch.setattr(mr.subprocess, "run", boom)
    line = mr._approvals_line(20881)
    assert "UNKNOWN" in line
    assert "Too many open files" in line


# ---------------------------------------------------------------------------
# Same file, same class — the linked-issue lookup 150 lines below (#720 sweep)
# ---------------------------------------------------------------------------

def _issue_dispatch(issue: Any) -> Any:
    def run(cmd: Any, *a: Any, **kw: Any) -> Any:
        argv = list(cmd)
        endpoint = str(argv[-1])
        if "api" in argv and "/issues/" in endpoint:
            if isinstance(issue, BaseException):
                raise issue
            rc, out, err = issue
            return _cp(argv, rc, out, err)
        if "api" in argv and endpoint.endswith("/approvals"):
            return _cp(argv, 0, json.dumps({"approved_by": []}))
        if "api" in argv:
            return _cp(argv, 0, "[]")
        if argv and argv[0] == "glab":
            return _cp(argv, 0, json.dumps(
                dict(json.loads(MR_PAYLOAD), description="fixes #12345")))
        return _cp(argv, 0, "")
    return run


def _render_issue(monkeypatch, capsys, issue: Any) -> str:
    monkeypatch.setattr(mr.subprocess, "run", _issue_dispatch(issue))
    monkeypatch.setattr(sys, "argv", ["mr.py", "20881"])
    assert mr.main() == 0
    return capsys.readouterr().out


def test_linked_issue_non_dict_payload_does_not_kill_the_render(monkeypatch, capsys) -> None:
    out = _render_issue(monkeypatch, capsys, (0, "[]", ""))
    assert "#12345" in out
    assert "unavailable" in out
    _assert_render_complete(out)


def test_linked_issue_api_failure_is_disclosed(monkeypatch, capsys) -> None:
    """rc != 0 printed nothing at all — the MR names an issue and the section
    it promises simply is not there."""
    out = _render_issue(monkeypatch, capsys, (1, "", "ERROR\n\nUnauthenticated.\n"))
    assert "#12345" in out
    assert "unavailable" in out
    _assert_render_complete(out)


def test_linked_issue_renders_normally(monkeypatch, capsys) -> None:
    body = json.dumps({
        "title": "The bug", "state": "opened",
        "labels": ["bug", "p1"], "assignees": [{"username": "alice"}],
    })
    out = _render_issue(monkeypatch, capsys, (0, body, ""))
    assert "## Issue #12345 — The bug" in out
    assert "State: opened | Labels: bug, p1 | Assignees: alice" in out
