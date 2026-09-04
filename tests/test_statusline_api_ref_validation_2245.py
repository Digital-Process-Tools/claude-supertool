"""2245 -- `repo`/`branch` are validated before either new gh-api reading
function interpolates them into a GitHub API path.

`_reading_from_check_runs` and `_reading_from_combined_status` build
``"repos/{}/commits/{}/...".format(repo, branch)`` directly from `.oss.json`'s
`repo`/`default_branch`, which reach `gather()` (and these two functions) with
no upstream validation. This file's own module docstring already carries a
validator for the `repo` shape -- `_REPO_RE`, applied by `_expected_watch_name`
-- but neither new function routed through it before this fix. Assertions are
that a malformed value is refused BEFORE `_run` (the `gh` subprocess call) is
even invoked -- mocked to raise if reached, so a call that still slips through
fails loudly rather than silently succeeding against a real `gh` binary.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

import pytest

PRESET_PATH = Path(__file__).parent.parent / ".oss" / "statusline.py"
_spec = importlib.util.spec_from_file_location("statusline_2245", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
statusline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(statusline)


#: Each pair is malformed in exactly one of `repo`/`branch` -- a `..` segment,
#: a `?` that would open a query string early, embedded whitespace, or a
#: `repo` that is not the plain `owner/name` shape `_REPO_RE` already pins.
MALFORMED_REPO_OR_BRANCH = [
    ("owner/name", "main/../../../etc"),
    ("owner/name", "main?x=1"),
    ("owner/name", "has space"),
    ("not-a-repo-at-all", "main"),
    ("owner/name/extra-segment", "main"),
    ("owner/name", ""),
    (None, "main"),
]


@pytest.mark.parametrize("repo,branch", MALFORMED_REPO_OR_BRANCH)
def test_malformed_repo_or_branch_is_refused_before_the_api_call(repo, branch):
    with mock.patch.object(statusline, "_run",
                            side_effect=AssertionError("gh must not be called")):
        assert statusline._reading_from_check_runs(repo, branch) is None
        assert statusline._reading_from_combined_status(repo, branch) is None


def test_a_normal_repo_and_branch_still_reach_the_api_path():
    """Must-not-fire control: a well-formed pair is not swept up by the guard,
    including a branch name carrying a slash -- legitimate (`feature/x`) and
    the reason the check does not simply forbid `/` the way `_REPO_RE` does
    for `repo`.
    """
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        return None

    with mock.patch.object(statusline, "_run", side_effect=fake_run):
        assert statusline._reading_from_check_runs("owner/name", "feature/x") is None
        assert statusline._reading_from_combined_status("owner/name", "feature/x") is None
    assert len(calls) == 2
    assert "repos/owner/name/commits/feature/x/check-runs" in calls[0]
    assert "repos/owner/name/commits/feature/x/status" in calls[1]
