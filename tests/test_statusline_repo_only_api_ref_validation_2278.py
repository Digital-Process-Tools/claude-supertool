"""2278/#1035 -- three `gh api` call sites in `.oss/statusline.py` that used
to interpolate `repo` after a guard (`_gh_count` at :1466, `_gh_external_issue_count`
at :1548, `_latest_release` at :1831 in the pre-#2298 tree) now interpolate it
after the equivalent guard again.

This file previously asserted the opposite: `/oss:scaffold --apply` (#2298)
had replaced this oss-owned `.oss/statusline.py` wholesale with a copy of
claude-oss's own plugin file, which at the time did not ship `_malformed_repo`
at all, so every value in `MALFORMED_REPO` -- including `None` and `""` --
reached `_run` unchanged except through `_latest_release`'s own pre-existing,
unrelated `if not repo: return None`. The fix was filed upstream instead, as
claude-oss#1035, rather than re-forked locally.

That day is claude-oss 0.26.0 (#2350): `/oss:scaffold --apply` against that
release carries `_malformed_repo` again, and all three call sites -- `_gh_count`,
`_gh_external_issue_count` and `_latest_release` alike -- call it and return
before `_run` when it is True. `_latest_release` no longer carries a separate
falsy check at all; `_malformed_repo(None)` and `_malformed_repo("")` are both
True on their own (neither is a str matching `_REPO_RE`), so the previous split
between "refused by the old falsy check" and "now reaches `_run`" collapses --
every value in `MALFORMED_REPO` is refused at all three call sites now, and
`_latest_release` gets the same single parametrized test as the other two.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

import pytest

PRESET_PATH = Path(__file__).parent.parent / ".oss" / "statusline.py"
_spec = importlib.util.spec_from_file_location("statusline_2278", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
statusline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(statusline)


#: Same malformed shapes #2245's own fixture used for `repo` alone.
MALFORMED_REPO = [
    None,
    "",
    "not-a-repo-at-all",
    "owner/name/extra-segment",
    "has space/name",
]


def _recording_run():
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        return None

    return calls, fake_run


@pytest.mark.parametrize("repo", MALFORMED_REPO)
def test_gh_count_is_refused_before_the_api_call_for_a_malformed_repo(repo):
    calls, fake_run = _recording_run()
    with mock.patch.object(statusline, "_run", side_effect=fake_run):
        assert statusline._gh_count(repo, "issue") is None
    assert len(calls) == 0


@pytest.mark.parametrize("repo", MALFORMED_REPO)
def test_gh_external_issue_count_is_refused_before_the_api_call_for_a_malformed_repo(repo):
    calls, fake_run = _recording_run()
    with mock.patch.object(statusline, "_run", side_effect=fake_run):
        assert statusline._gh_external_issue_count(repo, 3) is None
    assert len(calls) == 0


@pytest.mark.parametrize("repo", MALFORMED_REPO)
def test_latest_release_is_refused_before_the_api_call_for_a_malformed_repo(repo):
    """`_malformed_repo` now covers this call site too, so every shape in
    `MALFORMED_REPO` -- falsy or merely shape-invalid -- is refused the same
    way, with no separate falsy-only case left to distinguish.
    """
    with mock.patch.object(statusline, "_run",
                            side_effect=AssertionError("gh must not be called")):
        assert statusline._latest_release(repo) is None


def test_gh_count_a_normal_repo_still_reaches_the_api_path():
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        return "0"

    with mock.patch.object(statusline, "_run", side_effect=fake_run):
        statusline._gh_count("owner/name", "issue")
    assert calls
    assert any("repo:owner/name" in part for part in calls[0])


def test_gh_external_issue_count_a_normal_repo_still_reaches_the_api_path():
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        return ""

    with mock.patch.object(statusline, "_run", side_effect=fake_run):
        statusline._gh_external_issue_count("owner/name", 0)
    assert calls
    assert any("repos/owner/name/issues" in part for part in calls[0])


def test_latest_release_a_normal_repo_still_reaches_the_api_path():
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        return None

    with mock.patch.object(statusline, "_run", side_effect=fake_run):
        statusline._latest_release("owner/name")
    assert calls
    assert any(
        "repos/owner/name/contents/.claude-plugin/plugin.json" in part
        for part in calls[0]
    )
