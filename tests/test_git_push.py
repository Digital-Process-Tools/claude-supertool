"""Unit tests for presets/git/push.py — receipt + MR line + guards."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock


PRESET = Path(__file__).parent.parent / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


def _proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)


# ── _open_mr_line ────────────────────────────────────────────────────────

def test_mr_line_gitlab_with_pipeline() -> None:
    with mock.patch.object(push, "query_open_mr", return_value={
            "source": "gitlab", "iid": 42, "target": "master", "pipeline": "running"}):
        assert push._open_mr_line("b") == "MR !42 → master | pipeline: running"


def test_mr_line_gitlab_no_pipeline_says_triggered() -> None:
    with mock.patch.object(push, "query_open_mr", return_value={
            "source": "gitlab", "iid": 42, "target": "master", "pipeline": None}):
        assert push._open_mr_line("b") == "MR !42 → master | pipeline: triggered"


def test_mr_line_github() -> None:
    with mock.patch.object(push, "query_open_mr", return_value={
            "source": "github", "iid": 7, "target": "main", "pipeline": None}):
        assert push._open_mr_line("b") == "PR #7 → main | checks triggered"


def test_mr_line_none_is_empty() -> None:
    with mock.patch.object(push, "query_open_mr", return_value=None):
        assert push._open_mr_line("b") == ""


# ── _remote_sha ──────────────────────────────────────────────────────────

def test_remote_sha_empty_ref() -> None:
    assert push._remote_sha("") == ""


def test_remote_sha_resolves() -> None:
    with mock.patch.object(push, "_git", return_value=_proc("abc1234\n")):
        assert push._remote_sha("origin/x") == "abc1234"


# ── main() guards ────────────────────────────────────────────────────────

def test_main_not_a_repo(capsys) -> None:
    with mock.patch.object(push, "_git", return_value=_proc(returncode=1)):
        rc = push.main()
    assert rc == 1
    assert "not inside a git repository" in capsys.readouterr().out


def test_main_detached_head(capsys) -> None:
    def fake_git(args, timeout=30):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git\n", 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return _proc("HEAD\n", 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git):
        rc = push.main()
    assert rc == 1
    assert "detached HEAD" in capsys.readouterr().out


def test_main_rejected_push_surfaces_hint(capsys) -> None:
    rejected = ("To origin\n ! [rejected] feat -> feat (non-fast-forward)\n"
                "error: failed to push some refs")

    def fake_git(args, timeout=30):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git\n", 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "@{upstream}" not in args:
            return _proc("feat\n", 0)
        if args[0] == "rev-parse" and "@{upstream}" in args:
            return _proc("origin/feat\n", 0)
        if args[0] == "rev-parse" and args[1] == "--short":
            return _proc("aaa1111\n", 0)
        if args[0] == "push":
            return _proc("", 1, rejected)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "PUSH REJECTED" in out
    assert "force-with-lease" in out
    assert "pull --rebase" in out


def test_main_protected_branch_rejection_no_rebase_hint(capsys) -> None:
    """Server-side rejection (protected branch) must NOT advise pull --rebase."""
    rejected = ("To origin\n ! [remote rejected] main -> main "
                "(protected branch hook declined)\nerror: failed to push some refs")

    def fake_git(args, timeout=30):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git\n", 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "@{upstream}" not in args:
            return _proc("main\n", 0)
        if args[0] == "rev-parse" and "@{upstream}" in args:
            return _proc("origin/main\n", 0)
        if args[0] == "rev-parse" and args[1] == "--short":
            return _proc("aaa1111\n", 0)
        if args[0] == "push":
            return _proc("", 1, rejected)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "PUSH REJECTED" in out
    assert "server-side rule" in out
    assert "pull --rebase" not in out
    assert "force-with-lease" not in out


def test_main_up_to_date_when_remote_unchanged(capsys) -> None:
    """Push succeeds but remote SHA is unchanged → 'Already up to date'."""
    def fake_git(args, timeout=30):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git\n", 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "@{upstream}" not in args:
            return _proc("feat\n", 0)
        if args[0] == "rev-parse" and "@{upstream}" in args:
            return _proc("origin/feat\n", 0)
        if args[0] == "rev-parse" and args[1] == "--short":
            return _proc("aaa1111\n", 0)  # same before and after
        if args[0] == "push":
            return _proc("", 0)
        if args[:2] == ["rev-list", "--left-right"]:
            return _proc("0\t0\n", 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git), \
         mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Already up to date — nothing to push" in out


def test_main_success_receipt_with_mr(capsys) -> None:
    """Happy path: upstream set, remote advances, MR line appended."""
    shas = iter(["aaa1111", "bbb2222"])  # before, after

    def fake_git(args, timeout=30):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git\n", 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "@{upstream}" not in args:
            return _proc("feat\n", 0)
        if args[0] == "rev-parse" and "@{upstream}" in args:
            return _proc("origin/feat\n", 0)
        if args[0] == "rev-parse" and args[1] == "--short":
            return _proc(next(shas) + "\n", 0)
        if args[0] == "push":
            return _proc("", 0)
        if args[:2] == ["rev-list", "--count"]:
            return _proc("1\n", 0)
        if args[:2] == ["rev-list", "--left-right"]:
            return _proc("0\t0\n", 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git), \
         mock.patch.object(push, "query_open_mr", return_value={
             "source": "gitlab", "iid": 99, "target": "master", "pipeline": "created"}):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Status: pushed ✓" in out
    assert "aaa1111 → bbb2222 (1 commit(s))" in out
    assert "vs upstream: in sync" in out
    assert "MR !99 → master | pipeline: created" in out


def test_main_first_push_sets_upstream(capsys) -> None:
    """No upstream initially → push -u; receipt notes branch created."""
    calls: list[list[str]] = []

    def fake_git(args, timeout=30):
        calls.append(list(args))
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git\n", 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "@{upstream}" not in args:
            return _proc("feat\n", 0)
        if args[0] == "rev-parse" and "@{upstream}" in args:
            # No upstream the first call, set after push
            return _proc("origin/feat\n", 0) if any(c[0] == "push" for c in calls) else _proc("", 1)
        if args[0] == "rev-parse" and args[1] == "--short":
            return _proc("ccc3333\n", 0)
        if args[0] == "push":
            return _proc("", 0)
        if args[:2] == ["rev-list", "--left-right"]:
            return _proc("0\t0\n", 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git), \
         mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert ["push", "-u", "origin", "HEAD"] in calls
    assert "branch created" in out
