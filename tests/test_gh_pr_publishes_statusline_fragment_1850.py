"""`gh-pr` publishes the statusline's `gh-pr` fragment as a side effect (#1850).

Same pattern as `tests/test_gh_pr_mirror_write_through_2472.py`: `_gh` is
stubbed so the API call itself is never made, and the assertion is that a
NORMAL read leaves a fragment behind for `statusline` to render later --
never a second, independently-computed verdict (share the model, not the
op: the fragment carries `_checks.summarize_github`'s own output verbatim).

TDD: RED against a `pr.py` with no fragment wiring (confirmed by temporarily
reverting the `_statusline_fragments.publish` call and re-running this file --
see the developer report for the transcript), GREEN once `main()` calls it.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "presets"))
import _statusline_fragments as _fragments  # noqa: E402

PRESET_PATH = REPO / "presets" / "github" / "pr.py"
_spec = importlib.util.spec_from_file_location("github_pr_statusline_1850", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)


def _fake_gh_result(stdout: str) -> Any:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


def _pr_payload(*, number=1850, checks=None) -> str:
    return json.dumps({
        "number": number, "title": "statusline fragment test", "state": "OPEN",
        "author": {"login": "florian"}, "headRefName": "fix/1850", "baseRefName": "master",
        "labels": [], "milestone": None, "reviewDecision": None, "reviews": [],
        "mergeCommit": None, "mergedAt": None, "mergeable": "MERGEABLE", "isDraft": False,
        "url": "https://github.com/o/r/pull/1850", "body": "body", "comments": [],
        "additions": 1, "deletions": 1, "changedFiles": 1,
        "statusCheckRollup": checks or [], "assignees": [],
        "createdAt": "2026-09-10T00:00:00Z", "updatedAt": "2026-09-10T00:00:00Z",
        "headRefOid": "abc123",
    })


def test_a_plain_read_publishes_the_gh_pr_fragment(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SUPERTOOL_STATUSLINE_CACHE_DIR", str(tmp_path / "cache"))
    checks = [{"conclusion": "SUCCESS"}, {"conclusion": "SUCCESS"}]

    def fake_gh(args, timeout=10):
        return _fake_gh_result(_pr_payload(checks=checks))

    monkeypatch.setattr(pr, "_gh", fake_gh)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pr.py", "1850"])

    rc = pr.main()

    assert rc == 0
    data, state = _fragments.read("gh-pr", str(tmp_path))
    assert state == "ok", (data, capsys.readouterr())
    assert data["summary"] == pr._checks.summarize_github(checks)
    assert data["number"] == 1850
    assert data["branch"] == "fix/1850"


def test_a_publish_failure_never_fails_the_read(monkeypatch, tmp_path):
    """Best-effort (#1850): a cache directory `os.makedirs` cannot create
    must not turn a working `gh-pr` call into a failing one."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setenv("SUPERTOOL_STATUSLINE_CACHE_DIR", str(blocker / "cache"))

    def fake_gh(args, timeout=10):
        return _fake_gh_result(_pr_payload())

    monkeypatch.setattr(pr, "_gh", fake_gh)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pr.py", "1850"])

    rc = pr.main()

    assert rc == 0
