"""2278 -- three more `gh api` call sites in `.oss/statusline.py` that used
to interpolate `repo` after a guard (`_gh_count` at :1466, `_gh_external_issue_count`
at :1548, `_latest_release` at :1831 in the pre-#2298 tree) now interpolate it
with no such check, or with a narrower one than before, and that is a
recorded, accepted gap rather than a bug this file guards against.

This file previously asserted the guards refused a malformed `repo` before
`_run` was invoked. `/oss:scaffold --apply` replaced this oss-owned
`.oss/statusline.py` wholesale with the plugin's own copy (#2298), which does
not ship `_malformed_repo`. The fix is filed upstream instead, as
claude-oss#1035, rather than re-forked locally.

`_gh_count` and `_gh_external_issue_count` carry no repo check at all any
more -- every value in `MALFORMED_REPO`, including `None` and `""`, now
reaches `_run`. `_latest_release` still carries its own pre-existing
`if not repo: return None`, unrelated to the removed guard, so `None` and
`""` are still refused there; the three shape-invalid values are not.
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

#: The subset of `MALFORMED_REPO` still refused by `_latest_release`'s own
#: pre-existing, unrelated `if not repo` falsy check.
_LATEST_RELEASE_STILL_REFUSED = [None, ""]

#: The subset that now reaches `_run` there -- shape-invalid but truthy.
_LATEST_RELEASE_NOW_REACHES = [
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
def test_gh_count_still_reaches_the_api_call_for_a_malformed_repo(repo):
    calls, fake_run = _recording_run()
    with mock.patch.object(statusline, "_run", side_effect=fake_run):
        assert statusline._gh_count(repo, "issue") is None
    assert len(calls) == 1


@pytest.mark.parametrize("repo", MALFORMED_REPO)
def test_gh_external_issue_count_still_reaches_the_api_call_for_a_malformed_repo(repo):
    calls, fake_run = _recording_run()
    with mock.patch.object(statusline, "_run", side_effect=fake_run):
        assert statusline._gh_external_issue_count(repo, 3) is None
    assert len(calls) == 1


@pytest.mark.parametrize("repo", _LATEST_RELEASE_STILL_REFUSED)
def test_latest_release_still_refuses_a_falsy_repo_before_the_api_call(repo):
    with mock.patch.object(statusline, "_run",
                            side_effect=AssertionError("gh must not be called")):
        assert statusline._latest_release(repo) is None


@pytest.mark.parametrize("repo", _LATEST_RELEASE_NOW_REACHES)
def test_latest_release_now_reaches_the_api_call_for_a_shape_invalid_repo(repo):
    calls, fake_run = _recording_run()
    with mock.patch.object(statusline, "_run", side_effect=fake_run):
        assert statusline._latest_release(repo) is None
    assert len(calls) == 1


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
