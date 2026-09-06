"""2245/#1035 -- `repo`/`branch` are now refused, before the `gh api` call,
when either cannot safely fill `"repos/{}/commits/{}/...".format(repo, branch)`.

This file previously documented the opposite as an accepted gap: `/oss:scaffold
--apply` (#2298) had replaced this oss-owned `.oss/statusline.py` wholesale with
a copy of claude-oss's own plugin file, which at the time did not carry the
guard this repo's source copy of the same functions has always had. That gap
was filed upstream as claude-oss#1035 rather than re-forked locally, and this
file was repointed at the unguarded plugin behaviour so it would fail loudly
the day the guard was reintroduced there.

That day is claude-oss 0.26.0 (#2350): `/oss:scaffold --apply` against that
release carries `_malformed_repo`, `_BRANCH_UNSAFE_RE` and `_malformed_api_ref`
(`.oss/statusline.py`, citing #1035/#1051 in its own comments), and
`_reading_from_check_runs`/`_reading_from_combined_status` both call
`_malformed_api_ref(repo, branch)` and return `None` before `_run` when it is
True. This file is repointed again, at the now-current guarded state.
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


#: Same malformed shapes the guard refuses -- a `..` segment, a `?`
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
def test_malformed_repo_or_branch_is_refused_before_the_api_call(repo, branch):
    """`_malformed_api_ref` refuses every shape above before either function
    reaches `_run` -- no `gh api` call is ever made for a malformed pair.
    """
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        return None

    with mock.patch.object(statusline, "_run", side_effect=fake_run):
        assert statusline._reading_from_check_runs(repo, branch) is None
        assert statusline._reading_from_combined_status(repo, branch) is None
    assert len(calls) == 0


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
