"""2245 -- `repo`/`branch` reach `_reading_from_check_runs` and
`_reading_from_combined_status` with no validation, and that is now a
recorded, accepted gap rather than a bug this file guards against.

This file previously asserted the opposite: that `_malformed_repo` and
`_malformed_api_ref` refused a malformed value before either function
interpolated it into `"repos/{}/commits/{}/...".format(repo, branch)`. Those
two guards do not exist in this repository's copy of `.oss/statusline.py`
any more -- `/oss:scaffold --apply` replaced this oss-owned file wholesale
with the plugin's own copy (#2298), which does not ship them. The guards are
filed upstream instead, as claude-oss#1035, rather than re-forked locally --
re-adding them here would recreate the exact fork that PR existed to end.

So this file is repointed at the state the PR's own body documents and
accepts: both `repo` and `branch`, malformed or not, reach `_run` unchanged.
`repo`/`branch` come from a tracked `.oss.json`, reachable only by a
contributor editing that config and not by anything remote -- the risk
`_malformed_repo`/`_malformed_api_ref` used to close.
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


#: Same malformed shapes the guard used to refuse -- a `..` segment, a `?`
#: that would open a query string early, embedded whitespace, or a `repo`
#: that is not the plain `owner/name` shape.
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
def test_malformed_repo_or_branch_still_reaches_the_api_call(repo, branch):
    """Documents the accepted gap: no `_run` call is refused. Mocked to
    capture the call args rather than raise, so this fails loudly (not
    silently) the day a guard is reintroduced and starts short-circuiting
    these before `_run`.
    """
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        return None

    with mock.patch.object(statusline, "_run", side_effect=fake_run):
        assert statusline._reading_from_check_runs(repo, branch) is None
        assert statusline._reading_from_combined_status(repo, branch) is None
    assert len(calls) == 2


def test_a_normal_repo_and_branch_still_reach_the_api_path():
    """Must-not-fire control: a well-formed pair is not treated any
    differently, including a branch name carrying a slash -- legitimate
    (`feature/x`).
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
