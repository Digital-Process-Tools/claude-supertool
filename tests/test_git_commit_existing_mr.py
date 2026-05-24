"""Unit tests for presets/git/commit.py _existing_mr_for_branch helper."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest import mock


PRESET = Path(__file__).parent.parent / "presets" / "git" / "commit.py"
_spec = importlib.util.spec_from_file_location("git_commit", PRESET)
assert _spec is not None and _spec.loader is not None
commit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit)


def _proc(stdout: str = "", returncode: int = 0):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr="")


def test_empty_branch_returns_empty() -> None:
    assert commit._existing_mr_for_branch("") == ""


def test_glab_match_returns_bang_iid() -> None:
    fake_which = mock.Mock(side_effect=lambda c: "/usr/bin/glab" if c == "glab" else None)
    fake_run = mock.Mock(return_value=_proc('[{"iid": 21816, "title": "x"}]'))
    with mock.patch.object(commit.shutil, "which", fake_which), \
         mock.patch.object(commit.subprocess, "run", fake_run):
        assert commit._existing_mr_for_branch("feature/x") == "!21816"


def test_gh_fallback_when_no_glab() -> None:
    fake_which = mock.Mock(side_effect=lambda c: "/usr/bin/gh" if c == "gh" else None)
    fake_run = mock.Mock(return_value=_proc('[{"number": 172}]'))
    with mock.patch.object(commit.shutil, "which", fake_which), \
         mock.patch.object(commit.subprocess, "run", fake_run):
        assert commit._existing_mr_for_branch("feature/x") == "#172"


def test_no_tool_available_returns_empty() -> None:
    with mock.patch.object(commit.shutil, "which", return_value=None):
        assert commit._existing_mr_for_branch("feature/x") == ""


def test_glab_empty_list_falls_through_to_gh() -> None:
    fake_which = mock.Mock(side_effect=lambda c: f"/usr/bin/{c}" if c in ("glab", "gh") else None)
    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _proc("[]")  # glab: no MR
        return _proc('[{"number": 9}]')  # gh: PR exists

    with mock.patch.object(commit.shutil, "which", fake_which), \
         mock.patch.object(commit.subprocess, "run", side_effect=fake_run):
        assert commit._existing_mr_for_branch("feature/x") == "#9"


def test_glab_timeout_falls_through() -> None:
    fake_which = mock.Mock(side_effect=lambda c: f"/usr/bin/{c}" if c in ("glab", "gh") else None)
    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="glab", timeout=5)
        return _proc('[{"number": 42}]')

    with mock.patch.object(commit.shutil, "which", fake_which), \
         mock.patch.object(commit.subprocess, "run", side_effect=fake_run):
        assert commit._existing_mr_for_branch("feature/x") == "#42"


def test_glab_malformed_json_falls_through() -> None:
    fake_which = mock.Mock(side_effect=lambda c: f"/usr/bin/{c}" if c in ("glab", "gh") else None)
    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _proc("[not json")
        return _proc('[{"number": 7}]')

    with mock.patch.object(commit.shutil, "which", fake_which), \
         mock.patch.object(commit.subprocess, "run", side_effect=fake_run):
        assert commit._existing_mr_for_branch("feature/x") == "#7"
