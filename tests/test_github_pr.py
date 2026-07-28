"""Unit tests for presets/github/pr.py — slim status mode.

Mirrors test_gitlab_mr.py slim tests: gh-pr:NUMBER:status returns a tiny
~250B dashboard so "is it merged?" checks fit under the harness hook
cache cap.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr.py"
_spec = importlib.util.spec_from_file_location("github_pr", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)


def _fake_run(stdout: str, returncode: int = 0) -> Any:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=""
    )


def _pr_json_payload(**overrides: Any) -> str:
    base = {
        "number": 12,
        "title": "feat: slim mode",
        "state": "MERGED",
        "author": {"login": "max"},
        "headRefName": "max/gl-mr-status",
        "baseRefName": "master",
        "labels": [],
        "milestone": None,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "reviews": [],
        "mergeCommit": {"oid": "abc123def456789012345"},
        "additions": 100,
        "deletions": 5,
        "changedFiles": 3,
        "statusCheckRollup": [
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
        ],
        "url": "https://github.com/foo/bar/pull/12",
        "body": "",
        "comments": [],
    }
    base.update(overrides)
    return json.dumps(base)


def test_main_slim_status_mode_outputs_minimal_dashboard(monkeypatch, capsys) -> None:
    """gh-pr:NUMBER:status returns ~5 lines, fits under 500 bytes."""
    payload = _pr_json_payload()
    monkeypatch.setattr(
        pr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["pr.py", "12", "status"])
    rc = pr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "#12" in out
    assert "state: MERGED" in out
    assert "mergeable: MERGEABLE" in out
    assert "conflicts: no" in out
    assert "checks: 2 total: 2 passed, 0 failed, 0 pending" in out
    assert "review: APPROVED" in out
    assert "merge_commit: abc123def456" in out
    assert "url: https://github.com/foo/bar/pull/12" in out
    # Branch is identity, not detail — must be here so nobody escalates to full
    assert "branch: max/gl-mr-status -> master" in out
    # Full-dashboard sections must NOT appear
    assert "## Description" not in out
    assert "## Comments" not in out
    assert "Labels:" not in out
    assert "Changes:" not in out
    assert len(out) < 500


def test_main_slim_status_shows_non_default_base(monkeypatch, capsys) -> None:
    """Base branch matters as much as head: a stacked PR must say so."""
    payload = _pr_json_payload(state="OPEN", headRefName="max/part-2",
                               baseRefName="max/part-1")
    monkeypatch.setattr(
        pr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["pr.py", "12", "status"])
    rc = pr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "branch: max/part-2 -> max/part-1" in out


def test_main_slim_status_missing_branch_fields(monkeypatch, capsys) -> None:
    """Absent branch data degrades to '?' rather than crashing or vanishing."""
    payload = _pr_json_payload(headRefName=None, baseRefName=None)
    monkeypatch.setattr(
        pr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["pr.py", "12", "status"])
    rc = pr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "branch: ? -> ?" in out


def test_main_slim_status_with_conflicts(monkeypatch, capsys) -> None:
    payload = _pr_json_payload(state="OPEN", mergeable="CONFLICTING",
                                mergeCommit=None,
                                statusCheckRollup=[
                                    {"conclusion": "FAILURE", "status": "COMPLETED"},
                                    {"conclusion": None, "status": "IN_PROGRESS"},
                                ])
    monkeypatch.setattr(
        pr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["pr.py", "12", "status"])
    rc = pr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "state: OPEN" in out
    assert "conflicts: yes" in out
    assert "checks: 2 total: 0 passed, 1 failed, 1 pending" in out
    assert "merge_commit:" not in out


def test_main_full_mode_unaffected(monkeypatch, capsys) -> None:
    """No 2nd arg = full dashboard."""
    payload = _pr_json_payload()
    monkeypatch.setattr(
        pr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["pr.py", "12"])
    rc = pr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Branch:" in out
    assert "Checks:" in out


def test_main_slim_ignores_unknown_second_arg(monkeypatch, capsys) -> None:
    """Only literal 'status' triggers slim mode."""
    payload = _pr_json_payload()
    monkeypatch.setattr(
        pr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["pr.py", "12", "verbose"])
    rc = pr.main()
    out = capsys.readouterr().out
    assert rc == 0


# ---------------------------------------------------------------------------
# Always-print sections — empty case must show explicit marker
# ---------------------------------------------------------------------------

def test_main_full_mode_empty_sections_always_printed(monkeypatch, capsys) -> None:
    payload = _pr_json_payload(
        body="",
        comments=[],
        reviews=[],
        assignees=[],
        createdAt="2026-05-08T10:00:00Z",
        reviewThreads=[],
    )
    monkeypatch.setattr(
        pr.subprocess, "run",
        lambda *a, **kw: _fake_run(payload, returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["pr.py", "12"])
    rc = pr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Description\n_(empty)_" in out
    assert "## Comments (0)" in out
    assert "Reviews: none" in out
    assert "Assignees: none" in out
    assert "Created:" in out


def test_main_full_mode_with_assignees_age_threads(monkeypatch, capsys) -> None:
    payload = _pr_json_payload(
        assignees=[{"login": "alice"}, {"login": "bob"}],
        createdAt="2026-05-01T10:00:00Z",
        updatedAt="2026-05-08T10:00:00Z",
    )
    graphql_payload = json.dumps({
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
            {"isResolved": True},
            {"isResolved": False},
            {"isResolved": False},
        ]}}}}
    })

    def dispatch(*a, **kw):
        argv = a[0] if a else kw.get("args", [])
        if "graphql" in argv:
            return _fake_run(graphql_payload, returncode=0)
        return _fake_run(payload, returncode=0)

    monkeypatch.setattr(pr.subprocess, "run", dispatch)
    monkeypatch.setattr(sys, "argv", ["pr.py", "12"])
    rc = pr.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Assignees: alice, bob" in out
    assert "Created:" in out
    assert "Updated:" in out
    assert "Unresolved threads: 2 / 3" in out


def test_relative_age_formats() -> None:
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    assert pr._relative_age((now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")).endswith("m ago")
    assert pr._relative_age((now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")).endswith("h ago")
    assert pr._relative_age((now - timedelta(days=3)).isoformat().replace("+00:00", "Z")).endswith("d ago")
    assert pr._relative_age("") == "?"
