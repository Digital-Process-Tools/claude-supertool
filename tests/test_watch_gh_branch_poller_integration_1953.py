"""Integration test for `_snapshot` (#1953): the real `branch._gh` boundary,
mocked at the subprocess call rather than at any of `branch`'s own functions
-- so this exercises the actual composition `_snapshot` builds, the same one
#1951 asks to be proven equivalent to `gh-branch`'s own.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gh-branch" / "poller.py"
_spec = importlib.util.spec_from_file_location("gh_branch_poller_integration", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)

branch = poller.branch


def _proc(returncode=0, stdout=""):
    r = mock.Mock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = ""
    return r


def _fake_gh(argv, timeout=20):
    if argv[:2] == ["gh", "api"] and "commits/" in argv[2]:
        return _proc(0, json.dumps({
            "sha": "c391c1333b6793f4fc2e5a2cc830024fd834ffe1",
            "commit": {"committer": {"date": "2026-08-25T12:00:00Z"}},
        }))
    if argv[:3] == ["gh", "run", "list"]:
        return _proc(0, "[]")
    if argv[:3] == ["gh", "repo", "view"]:
        return _proc(0, json.dumps({"nameWithOwner": "OWNER/REPO",
                                    "defaultBranchRef": {"name": "main"}}))
    raise AssertionError(f"unexpected gh call: {argv}")


def test_snapshot_reaches_no_run_through_the_real_gh_boundary() -> None:
    """A commit with a resolvable sha and an empty run list is `NO_RUN`,
    reached through `branch._head_commit` -> `branch._run_list` ->
    `branch.verdict` exactly as `gh-branch`'s own `main()` reaches it -- the
    same composition, mocked at the one seam (`_gh`) both share."""
    with mock.patch.object(branch, "_gh", side_effect=_fake_gh), \
         mock.patch("time.time", return_value=1798000000):
        state, sentence, sha, error = poller._snapshot("main")
    assert error == "", error
    assert state == branch.NO_RUN, (state, sentence)
    assert sha == "c391c1333b6793f4fc2e5a2cc830024fd834ffe1"


def test_snapshot_reports_the_lookup_failure_when_the_commit_cannot_resolve() -> None:
    def _fail(argv, timeout=20):
        if argv[:2] == ["gh", "api"]:
            return _proc(1, "")
        raise AssertionError(f"unexpected gh call: {argv}")
    with mock.patch.object(branch, "_gh", side_effect=_fail):
        state, sentence, sha, error = poller._snapshot("main")
    assert state == ""
    assert error, "a commit that could not resolve must report an error, not a state"
