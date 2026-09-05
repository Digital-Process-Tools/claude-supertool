"""#1607 item 2 -- `gl-mr:N:status` names no approval state at all.

Approval state is reachable through the MR's own `/approvals` endpoint and
`gl-mr`'s plain (non-`:status`) view already fetches it through
`_approvals_line` -- but `:status` is the poll-loop render a maintainer calls
ad hoc to decide whether an MR is mergeable, and it skipped the fetch
entirely. A 2026-08-15 comment on #1607 corrected the original framing: the
`gitlab-mr` watch poller does NOT call `gl-mr:N:status` at all (it reads the
MR endpoint directly -- presets/watch/sources/gitlab-mr/poller.py), so the
cost is paid only by a maintainer's own ad hoc `:status` call, on the same
#815 "state it, don't smuggle it" argument the leg tally in `_pipeline_leg_lines`
already uses to justify its own unconditional extra round trip.

`_approvals_line` already carries a real three-state contract (approved /
none / UNKNOWN with a reason); `:status` now reuses it rather than inventing
a second one, so "no approvals" can never be printed for "did not ask".
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
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


mr = _load("presets/gitlab/mr.py", "gitlab_mr_status_approvals_1607")


def _mr_payload(**overrides: Any) -> dict:
    base = {
        "iid": 42,
        "state": "opened",
        "merge_status": "can_be_merged",
        "has_conflicts": False,
        "source_branch": "feature/x",
        "target_branch": "master",
        "head_pipeline": {"status": "success", "id": 999},
        "merged_at": None,
        "merge_commit_sha": "",
        "web_url": "https://gitlab.example/foo/-/merge_requests/42",
    }
    base.update(overrides)
    return base


def _run_status(monkeypatch, mr_payload: dict, approvals_response):
    """Route by URL: the MR fetch and the approvals fetch get different bodies.

    `approvals_response` is either a dict body (200) or ("error", stderr) to
    simulate a failed `glab api` call.
    """
    def fake_run(args, **kw):
        url = args[2] if len(args) > 2 else ""
        if "/approvals" in url:
            if isinstance(approvals_response, tuple):
                _, stderr = approvals_response
                return subprocess.CompletedProcess(args=args, returncode=1,
                                                    stdout="", stderr=stderr)
            return subprocess.CompletedProcess(
                args=args, returncode=0,
                stdout=json.dumps(approvals_response), stderr="")
        # Everything else (MR fetch, pipeline lookup) answers with the MR body.
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(mr_payload), stderr="")

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["mr.py", "42", "status"])
    return mr.main()


# ---------------------------------------------------------------------------
# The defect: :status renders nothing about approvals at all
# ---------------------------------------------------------------------------

def test_status_names_an_approver_when_one_exists(monkeypatch, capsys) -> None:
    rc = _run_status(monkeypatch, _mr_payload(),
                      {"approved_by": [{"user": {"username": "alice"}}]})
    out = capsys.readouterr().out
    assert rc == 0
    # Slim's own lowercase key convention (#628/#1607) -- `_approvals_line`'s
    # pinned "Approved by: ..." wording is what the FULL dashboard prints;
    # `:status` reformats only the prefix, never the value or its states.
    assert "approved_by: alice" in out, out


def test_status_says_none_when_nobody_has_approved(monkeypatch, capsys) -> None:
    """Must-fire twin: absence of approvers must still print a real line,
    not just vanish -- distinct from 'never asked'."""
    rc = _run_status(monkeypatch, _mr_payload(), {"approved_by": []})
    out = capsys.readouterr().out
    assert rc == 0
    assert "approved_by: none" in out, out


# ---------------------------------------------------------------------------
# The third state: could not look is not "no approvals"
# ---------------------------------------------------------------------------

def test_status_reports_unknown_rather_than_no_approvals_on_a_failed_fetch(
        monkeypatch, capsys) -> None:
    rc = _run_status(monkeypatch, _mr_payload(),
                      ("error", "500 Internal Server Error"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "approved_by: UNKNOWN" in out, out
    # The failure this exists to prevent: a fetch that could not run must
    # never render as "approved_by: none", which asserts the opposite fact.
    assert "approved_by: none" not in out, out
