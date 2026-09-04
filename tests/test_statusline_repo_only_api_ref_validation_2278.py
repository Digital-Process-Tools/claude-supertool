"""2278 -- three more `gh api` call sites in `.oss/statusline.py` interpolate
the same unvalidated `repo` value #2245's guard left uncovered: `_gh_count`
(:1466), `_gh_external_issue_count` (:1548 -- via `total`'s own repo-scoped
call, but the interpolation is in the function's own `gh api` argument) and
`_latest_release` (:1831). Each is refused before `_run` is invoked -- mocked
to raise if reached, so a call that slips through fails loudly.
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


@pytest.mark.parametrize("repo", MALFORMED_REPO)
def test_gh_count_refuses_a_malformed_repo_before_the_api_call(repo):
    with mock.patch.object(statusline, "_run",
                            side_effect=AssertionError("gh must not be called")):
        assert statusline._gh_count(repo, "issue") is None


@pytest.mark.parametrize("repo", MALFORMED_REPO)
def test_gh_external_issue_count_refuses_a_malformed_repo_before_the_api_call(repo):
    with mock.patch.object(statusline, "_run",
                            side_effect=AssertionError("gh must not be called")):
        assert statusline._gh_external_issue_count(repo, 3) is None


@pytest.mark.parametrize("repo", MALFORMED_REPO)
def test_latest_release_refuses_a_malformed_repo_before_the_api_call(repo):
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
