"""Unit tests for presets/git/_git_common.py — shared git helpers."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest import mock


PRESET = Path(__file__).parent.parent / "presets" / "git" / "_git_common.py"
_spec = importlib.util.spec_from_file_location("git_common_mod", PRESET)
assert _spec is not None and _spec.loader is not None
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)


def _proc(stdout: str = "", returncode: int = 0):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr="")


# ── _first_error_line ────────────────────────────────────────────────────

def test_first_error_line_picks_error_keyword() -> None:
    text = "Running pre-commit\nok 12 files\nfatal: hook rejected\nbye"
    assert common._first_error_line(text) == "fatal: hook rejected"


def test_first_error_line_picks_emoji_marker() -> None:
    assert "❌" in common._first_error_line("step 1\n❌ Push blocked\n")


def test_first_error_line_picks_rejected_bracket() -> None:
    text = "To origin\n ! [rejected] main -> main (non-fast-forward)\ndone"
    assert "! [rejected]" in common._first_error_line(text)


def test_first_error_line_falls_back_to_last_nonempty() -> None:
    assert common._first_error_line("a\nb\n\n") == "b"


def test_first_error_line_empty_input() -> None:
    assert common._first_error_line("") == ""
    assert common._first_error_line("\n\n") == ""


def test_first_error_line_skips_green_success_banner() -> None:
    # '0 errors' contains 'error' — must not be picked over the real failure.
    text = "✅ Prettier done. 0 errors.\nerror: failed to push some refs"
    assert common._first_error_line(text) == "error: failed to push some refs"


def test_first_error_line_pure_success_returns_empty() -> None:
    assert common._first_error_line("✅ format done. 0 errors.") == ""


def test_first_error_line_hard_error_wins_over_success_marker() -> None:
    # A line carrying a hard error keyword is surfaced even if it also has a
    # success phrase — suppressing a real error is the worse failure.
    line = "Fatal: Uncaught RuntimeException: pushed successfully."
    assert common._first_error_line(line) == line


def test_looks_like_success_markers() -> None:
    assert common._looks_like_success("✅ done")
    assert common._looks_like_success("formatted, 0 errors")
    assert common._looks_like_success("branch pushed successfully")
    assert not common._looks_like_success("error: failed to push")
    assert not common._looks_like_success("")


def test_looks_like_success_substring_false_negative_1704() -> None:
    """A success line must not be demoted by a keyword hiding inside a longer
    word. `_looks_like_success` suppresses (the mirror of #1669's `_git_common`
    error test, which selects), so its failure mode runs the other way: a real
    success line reads as not-success because 'fatal' is a substring of
    'nonfatal' (#1704 instance 2).
    """
    assert common._looks_like_success(
        "✅ nonfatal warnings resolved, pushed successfully")


# ── query_open_mr ────────────────────────────────────────────────────────

_HOSTED_REMOTE = ("origin" + chr(9) + "https://gitlab.com/acme/x.git (fetch)" +
                  chr(10) + "origin" + chr(9) +
                  "https://gitlab.com/acme/x.git (push)" + chr(10))


class _GitRemoteProc:
    """A `Popen` double standing in for `_git`'s own "remote -v" call (#2033
    moved `_git` off `subprocess.run` onto `Popen` — see
    `tests/test_git_common_stranded_lock_2033.py`)."""

    def __init__(self, stdout: str) -> None:
        self.returncode = 0
        self._stdout = stdout

    def communicate(self, timeout=None):
        return self._stdout, ""


def _runs(cli):
    """`subprocess.run`/`Popen` double: git answers about the repo, the CLI
    about the branch.

    Since #948 the lookup asks `git remote -v` first — a repo where no remote
    names a host has no tracker, and that is knowable without a CLI that may
    not be authenticated. `_git` reaches git through `subprocess.Popen`
    (#2033); `query_open_mr`'s CLI probes still go through `subprocess.run`
    directly. A double that handed the CLI's script to the git call would
    answer "no remotes" and short-circuit the very arm each test below is
    named after, so the two seams are patched separately below and this
    function only builds the CLI half.

    `cli` is a `CompletedProcess`-alike, or a callable taking argv.
    """
    def run(cmd, *_a, **_k):
        # `hasattr(..., "returncode")`, not `callable(...)`: a `mock.Mock`
        # stub result is itself callable, and calling it returns another Mock.
        return cli if hasattr(cli, "returncode") else cli(cmd)
    return run


def _git_remote_popen(cmd, **_kw):
    # `_git` prepends `--no-optional-locks` ahead of the subcommand (#1945),
    # so the discriminator can no longer assume the subcommand sits at
    # cmd[1] -- it looks for "remote" anywhere in a `git` argv instead of at
    # a fixed position. This double only ever stands in for that one call —
    # `query_open_mr`'s glab/gh probes never reach `Popen`.
    assert cmd and cmd[0] == "git" and "remote" in cmd, cmd
    return _GitRemoteProc(_HOSTED_REMOTE)

def test_query_empty_branch_returns_none() -> None:
    assert common.query_open_mr("") is None
    assert common.query_open_mr("HEAD") is None


def test_query_glab_match_returns_gitlab_fields() -> None:
    fake_which = mock.Mock(side_effect=lambda c: "/usr/bin/glab" if c == "glab" else None)
    fake_run = _runs(_proc(
        '[{"iid": 21816, "target_branch": "master", '
        '"pipeline": {"status": "running"}}]'))
    with mock.patch.object(common.shutil, "which", fake_which), \
         mock.patch.object(common.subprocess, "run", fake_run), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        mr = common.query_open_mr("feature/x")
    assert mr == {"source": "gitlab", "iid": 21816,
                  "target": "master", "pipeline": "running",
                  "pipeline_id": None, "pipeline_url": None,
                  "merge_status": None}


def test_query_glab_no_pipeline_yields_none_status() -> None:
    fake_which = mock.Mock(side_effect=lambda c: "/usr/bin/glab" if c == "glab" else None)
    fake_run = _runs(_proc('[{"iid": 5, "target_branch": "main"}]'))
    with mock.patch.object(common.shutil, "which", fake_which), \
         mock.patch.object(common.subprocess, "run", fake_run), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        mr = common.query_open_mr("feature/x")
    assert mr is not None and mr["pipeline"] is None


def test_query_gh_fallback_returns_github_fields() -> None:
    fake_which = mock.Mock(side_effect=lambda c: "/usr/bin/gh" if c == "gh" else None)
    fake_run = _runs(_proc('[{"number": 172, "baseRefName": "main"}]'))
    with mock.patch.object(common.shutil, "which", fake_which), \
         mock.patch.object(common.subprocess, "run", fake_run), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        mr = common.query_open_mr("feature/x")
    assert mr == {"source": "github", "iid": 172,
                  "target": "main", "pipeline": None,
                  "pipeline_id": None, "pipeline_url": None,
                  "merge_status": None}


def test_query_no_tool_returns_none() -> None:
    with mock.patch.object(common.shutil, "which", return_value=None):
        assert common.query_open_mr("feature/x") is None


def test_query_glab_empty_falls_through_to_gh() -> None:
    fake_which = mock.Mock(side_effect=lambda c: f"/usr/bin/{c}" if c in ("glab", "gh") else None)
    call_count = {"n": 0}

    def fake_run(_cmd):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _proc("[]")
        return _proc('[{"number": 9, "baseRefName": "dev"}]')

    with mock.patch.object(common.shutil, "which", fake_which), \
         mock.patch.object(common.subprocess, "run", _runs(fake_run)), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        mr = common.query_open_mr("feature/x")
    assert mr is not None and mr["source"] == "github" and mr["iid"] == 9


def test_query_glab_timeout_falls_through() -> None:
    fake_which = mock.Mock(side_effect=lambda c: f"/usr/bin/{c}" if c in ("glab", "gh") else None)
    call_count = {"n": 0}

    def fake_run(_cmd):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="glab", timeout=5)
        return _proc('[{"number": 42, "baseRefName": "main"}]')

    with mock.patch.object(common.shutil, "which", fake_which), \
         mock.patch.object(common.subprocess, "run", _runs(fake_run)), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        mr = common.query_open_mr("feature/x")
    assert mr is not None and mr["iid"] == 42


def test_query_glab_malformed_json_falls_through() -> None:
    fake_which = mock.Mock(side_effect=lambda c: f"/usr/bin/{c}" if c in ("glab", "gh") else None)
    call_count = {"n": 0}

    def fake_run(_cmd):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _proc("[not json")
        return _proc('[{"number": 7, "baseRefName": "main"}]')

    with mock.patch.object(common.shutil, "which", fake_which), \
         mock.patch.object(common.subprocess, "run", _runs(fake_run)), \
         mock.patch.object(common.subprocess, "Popen", _git_remote_popen):
        mr = common.query_open_mr("feature/x")
    assert mr is not None and mr["iid"] == 7
