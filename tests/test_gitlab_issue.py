"""Tests for presets/gitlab/issue.py — :full flag fetches untruncated body+comments."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "issue.py"
_spec = importlib.util.spec_from_file_location("gitlab_issue", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issue)


def _fake_run(stdout: str, returncode: int = 0) -> Any:
    return subprocess.CompletedProcess(
        args=["glab"], returncode=returncode, stdout=stdout, stderr=""
    )


def _install_fakes(monkeypatch, *, description: str, comments: list[dict]) -> None:
    """Stub glab calls: issue view, related MRs (empty), notes."""
    issue_payload = json.dumps({
        "title": "Plan",
        "state": "opened",
        "labels": [],
        "milestone": None,
        "assignees": [],
        "author": {"username": "florian"},
        "iid": 12345,
        "web_url": "",
        "description": description,
        "project_id": 1,
    })
    notes_payload = json.dumps(comments)
    related_payload = json.dumps([])

    def fake_glab(args, timeout=10):
        return _fake_run(issue_payload)

    def fake_glab_api(endpoint, timeout=10):
        if "related_merge_requests" in endpoint:
            return _fake_run(related_payload)
        if "/notes" in endpoint:
            return _fake_run(notes_payload)
        return _fake_run("[]")

    monkeypatch.setattr(issue, "_glab", fake_glab)
    monkeypatch.setattr(issue, "_glab_api", fake_glab_api)
    monkeypatch.setattr(issue, "_download_images", lambda urls, n: [])


def test_default_truncates_long_description(monkeypatch, capsys) -> None:
    long_desc = "x" * (issue.DESCRIPTION_MAX + 500)
    _install_fakes(monkeypatch, description=long_desc, comments=[])
    monkeypatch.setattr(sys, "argv", ["issue.py", "12345"])
    rc = issue.main()
    out = capsys.readouterr().out
    assert rc == 0
    # Measure the description text itself — everything up to the truncation
    # marker that #698 added at the cut. Splitting on "## Description" alone
    # also swallows that marker and the Comments header, which is a
    # measurement artefact, not description content. The fence #694 puts around
    # remote text is the same kind of artefact: it is supertool's own output
    # sitting inside the slice, and counting it against the body cap would make
    # this test measure the marker rather than the description.
    body = out.split("## Description")[1] if "## Description" in out else ""
    body = body.split("…[")[0]
    body = body.replace(issue._untrusted.open_marker(), "")
    body = body.replace(issue._untrusted.close_marker(), "")
    assert len(body.strip()) <= issue.DESCRIPTION_MAX


def test_full_flag_keeps_full_description(monkeypatch, capsys) -> None:
    long_desc = "x" * (issue.DESCRIPTION_MAX + 500)
    _install_fakes(monkeypatch, description=long_desc, comments=[])
    monkeypatch.setattr(sys, "argv", ["issue.py", "12345", "full"])
    rc = issue.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "x" * (issue.DESCRIPTION_MAX + 500) in out


def _comment(author: str, body: str) -> dict:
    return {
        "system": False,
        "author": {"username": author},
        "body": body,
        "created_at": "2026-05-07T20:00:00Z",
    }


def test_default_caps_at_10_comments(monkeypatch, capsys) -> None:
    comments = [_comment(f"u{i}", f"msg-{i}") for i in range(15)]
    _install_fakes(monkeypatch, description="d", comments=comments)
    monkeypatch.setattr(sys, "argv", ["issue.py", "12345"])
    issue.main()
    out = capsys.readouterr().out
    assert "msg-14" in out  # last
    assert "msg-0" not in out  # earliest dropped
    assert "use :full" in out  # nudge


def test_full_flag_shows_all_comments(monkeypatch, capsys) -> None:
    comments = [_comment(f"u{i}", f"msg-{i}") for i in range(15)]
    _install_fakes(monkeypatch, description="d", comments=comments)
    monkeypatch.setattr(sys, "argv", ["issue.py", "12345", "full"])
    issue.main()
    out = capsys.readouterr().out
    assert "msg-0" in out
    assert "msg-14" in out


def test_full_flag_keeps_long_comment_body(monkeypatch, capsys) -> None:
    long_body = "y" * (issue.COMMENT_MAX + 500)
    comments = [_comment("florian", long_body)]
    _install_fakes(monkeypatch, description="d", comments=comments)
    monkeypatch.setattr(sys, "argv", ["issue.py", "12345", "full"])
    issue.main()
    out = capsys.readouterr().out
    assert long_body in out
    assert "truncated" not in out


def test_default_truncates_long_comment_body(monkeypatch, capsys) -> None:
    long_body = "y" * (issue.COMMENT_MAX + 500)
    comments = [_comment("florian", long_body)]
    _install_fakes(monkeypatch, description="d", comments=comments)
    monkeypatch.setattr(sys, "argv", ["issue.py", "12345"])
    issue.main()
    out = capsys.readouterr().out
    assert "truncated" in out
