"""Unit tests for commit.py _existing_mr_for_branch — a thin formatter over
presets/git/_common.query_open_mr. Patches happen on _common (where the
glab→gh lookup lives) and assert through commit's !iid/#iid formatting."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest import mock


PRESET = Path(__file__).parent.parent / "presets" / "git" / "commit.py"
_spec = importlib.util.spec_from_file_location("git_commit", PRESET)
assert _spec is not None and _spec.loader is not None
commit = importlib.util.module_from_spec(_spec)
# exec_module runs commit.py's `sys.path.insert(...)`, making `_common`
# importable below and registering it in sys.modules.
_spec.loader.exec_module(commit)

import _git_common as _common  # noqa: E402  — resolvable after commit.py inserted its dir


def _proc(stdout: str = "", returncode: int = 0):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr="")


_HOSTED_REMOTE = ("origin" + chr(9) + "https://gitlab.com/acme/x.git (fetch)" +
                  chr(10) + "origin" + chr(9) +
                  "https://gitlab.com/acme/x.git (push)" + chr(10))


def _runs(cli):
    """`subprocess.run` double: git answers about the repo, the CLI about the branch.

    Since #948 the lookup asks `git remote -v` before it spends a CLI call, and
    `_git` shares this same `subprocess.run`. Answering the git call with the
    CLI's script would report a repo with no remotes — no tracker, no MR — and
    each test below would stop reaching the arm it is named after.
    """
    def run(cmd, *_a, **_k):
        if list(cmd[:2]) == ["git", "remote"]:
            return _proc(_HOSTED_REMOTE)
        # `hasattr(..., "returncode")`, not `callable(...)`: a `mock.Mock`
        # stub result is itself callable, and calling it returns another Mock.
        return cli if hasattr(cli, "returncode") else cli(cmd)
    return run


def test_empty_branch_returns_empty() -> None:
    assert commit._existing_mr_for_branch("") == ""


def test_detached_head_returns_empty() -> None:
    """`git rev-parse --abbrev-ref HEAD` returns 'HEAD' when detached."""
    assert commit._existing_mr_for_branch("HEAD") == ""


def test_glab_match_returns_bang_iid() -> None:
    fake_which = mock.Mock(side_effect=lambda c: "/usr/bin/glab" if c == "glab" else None)
    fake_run = _runs(_proc('[{"iid": 21816, "title": "x"}]'))
    with mock.patch.object(_common.shutil, "which", fake_which), \
         mock.patch.object(_common.subprocess, "run", fake_run):
        assert commit._existing_mr_for_branch("feature/x") == "!21816"


def test_gh_fallback_when_no_glab() -> None:
    fake_which = mock.Mock(side_effect=lambda c: "/usr/bin/gh" if c == "gh" else None)
    fake_run = _runs(_proc('[{"number": 172}]'))
    with mock.patch.object(_common.shutil, "which", fake_which), \
         mock.patch.object(_common.subprocess, "run", fake_run):
        assert commit._existing_mr_for_branch("feature/x") == "#172"


def test_no_tool_available_returns_empty() -> None:
    with mock.patch.object(_common.shutil, "which", return_value=None):
        assert commit._existing_mr_for_branch("feature/x") == ""


def test_glab_empty_list_falls_through_to_gh() -> None:
    fake_which = mock.Mock(side_effect=lambda c: f"/usr/bin/{c}" if c in ("glab", "gh") else None)
    call_count = {"n": 0}

    def fake_run(_cmd):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _proc("[]")  # glab: no MR
        return _proc('[{"number": 9}]')  # gh: PR exists

    with mock.patch.object(_common.shutil, "which", fake_which), \
         mock.patch.object(_common.subprocess, "run", _runs(fake_run)):
        assert commit._existing_mr_for_branch("feature/x") == "#9"


def test_glab_timeout_falls_through() -> None:
    fake_which = mock.Mock(side_effect=lambda c: f"/usr/bin/{c}" if c in ("glab", "gh") else None)
    call_count = {"n": 0}

    def fake_run(_cmd):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="glab", timeout=5)
        return _proc('[{"number": 42}]')

    with mock.patch.object(_common.shutil, "which", fake_which), \
         mock.patch.object(_common.subprocess, "run", _runs(fake_run)):
        assert commit._existing_mr_for_branch("feature/x") == "#42"


def test_glab_malformed_json_falls_through() -> None:
    fake_which = mock.Mock(side_effect=lambda c: f"/usr/bin/{c}" if c in ("glab", "gh") else None)
    call_count = {"n": 0}

    def fake_run(_cmd):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _proc("[not json")
        return _proc('[{"number": 7}]')

    with mock.patch.object(_common.shutil, "which", fake_which), \
         mock.patch.object(_common.subprocess, "run", _runs(fake_run)):
        assert commit._existing_mr_for_branch("feature/x") == "#7"
