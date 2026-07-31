"""Issue #623 — the push verdict must survive `| tail -N`.

These tests read the layer a caller actually reads: the *last few lines* of
rendered stdout. The reported defect is that `git-push` ends on an untracked
file list, so `tail -6` shows nothing about whether the push landed.

Every failure mode gets its own test, because the failure cases are the ones
that must survive truncation — success is the easy case. In particular a push
that did NOT happen (up-to-date, rejected, paused, timed out, never attempted)
must never render like one that did.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest import mock


PRESET = Path(__file__).parent.parent / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push_623", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


HEAD_SHA = "bbb2222cccccccccccccccccccccccccccccccc"
OLD_SHA = "aaa1111cccccccccccccccccccccccccccccccc"

# 23 test-generated junk files — the working-tree shape from the issue report.
JUNK = "\n".join(
    f"?? static/placeholder/placeholder-{i:03d}.zip" for i in range(23)
) + "\n"


def _proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)


def _tail(out: str, n: int) -> list[str]:
    return [ln for ln in out.splitlines() if ln.strip()][-n:]


def _base_git(*, push_result, ls_remote: str = "", status: str = "",
              shas=("aaa1111", "bbb2222"), head: str = HEAD_SHA):
    """fake _git for a repo on branch 'feat' with upstream origin/feat."""
    sha_iter = iter(shas)

    def fake_git(args, timeout=30):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git\n", 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "@{upstream}" not in args:
            return _proc("feat\n", 0)
        if args[0] == "rev-parse" and "@{upstream}" in args:
            return _proc("origin/feat\n", 0)
        if args[0] == "rev-parse" and args[1] == "--short":
            try:
                return _proc(next(sha_iter) + "\n", 0)
            except StopIteration:
                return _proc(shas[-1] + "\n", 0)
        if args[:2] == ["rev-parse", "HEAD"]:
            return _proc(head + "\n", 0)
        if args[0] == "ls-remote":
            if not ls_remote:
                return _proc("", 0)
            return _proc(f"{ls_remote}\trefs/heads/feat\n", 0)
        if args[0] == "push":
            if isinstance(push_result, Exception):
                raise push_result
            return push_result
        if args[:2] == ["status", "--porcelain"]:
            return _proc(status, 0)
        if args[:2] == ["rev-list", "--count"]:
            return _proc("1\n", 0)
        if args[:2] == ["rev-list", "--left-right"]:
            return _proc("0\t0\n", 0)
        return _proc("", 0)

    return fake_git


# ── the reported defect ──────────────────────────────────────────────────

def test_success_verdict_survives_tail_under_untracked_junk(capsys) -> None:
    """#623: with 23 untracked files, `tail -6` must still name the verdict."""
    with mock.patch.object(push, "_git",
                           side_effect=_base_git(push_result=_proc("", 0),
                                                 ls_remote=HEAD_SHA,
                                                 status=JUNK)), \
            mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    tail = "\n".join(_tail(out, 6))
    assert "PUSHED" in tail, f"verdict scrolled off the tail:\n{tail}"
    assert "bbb2222" in tail, f"post-push sha scrolled off the tail:\n{tail}"
    assert "placeholder-" not in tail, f"tail is junk files:\n{tail}"


def test_untracked_files_demoted_to_a_count_with_escape_hatch(capsys) -> None:
    """Information relocated, not lost: count + how to get the list."""
    with mock.patch.object(push, "_git",
                           side_effect=_base_git(push_result=_proc("", 0),
                                                 ls_remote=HEAD_SHA,
                                                 status=JUNK)), \
            mock.patch.object(push, "query_open_mr", return_value=None):
        push.main()
    out = capsys.readouterr().out
    assert "23 change(s) NOT in this push" in out
    assert "git-status:full" in out
    assert "placeholder-000.zip" not in out


# ── the remote sha is reported only when it was actually read ────────────

def test_result_line_marks_sha_verified_when_ls_remote_matches_head(capsys) -> None:
    with mock.patch.object(push, "_git",
                           side_effect=_base_git(push_result=_proc("", 0),
                                                 ls_remote=HEAD_SHA)), \
            mock.patch.object(push, "query_open_mr", return_value=None):
        push.main()
    out = capsys.readouterr().out
    assert "verified" in out.lower()
    assert "unverified" not in out.lower()


def test_result_line_says_unverified_when_ls_remote_is_silent(capsys) -> None:
    """No answer from the remote is said out loud, never defaulted to success."""
    with mock.patch.object(push, "_git",
                           side_effect=_base_git(push_result=_proc("", 0),
                                                 ls_remote="")), \
            mock.patch.object(push, "query_open_mr", return_value=None):
        push.main()
    out = capsys.readouterr().out
    assert "unverified" in out.lower()


# ── a push that did NOT happen must not render like one that did ─────────

def test_already_up_to_date_tail_says_not_pushed(capsys) -> None:
    with mock.patch.object(push, "_git",
                           side_effect=_base_git(push_result=_proc("", 0),
                                                 ls_remote=HEAD_SHA,
                                                 status=JUNK,
                                                 shas=("aaa1111", "aaa1111"))), \
            mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    tail = "\n".join(_tail(out, 4))
    assert "NOT PUSHED" in tail, f"up-to-date rendered like a push:\n{tail}"


def test_rejected_by_hook_verdict_survives_long_git_output(capsys) -> None:
    """A pre-push hook dump must not bury the verdict."""
    hook_noise = "\n".join(f"phpcs: checking file {i}.php" for i in range(40))
    rejected = (f"{hook_noise}\nerror: failed to push some refs to 'origin'\n"
                "hint: pre-push hook declined")
    with mock.patch.object(push, "_git",
                           side_effect=_base_git(
                               push_result=_proc("", 1, rejected),
                               ls_remote=OLD_SHA)), \
            mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc != 0
    tail = "\n".join(_tail(out, 3))
    assert "NOT PUSHED" in tail, f"rejection buried under hook output:\n{tail}"


def test_network_error_verdict_survives_tail(capsys) -> None:
    err = ("fatal: unable to access 'https://gitlab/x.git': "
           "Could not resolve host: gitlab")
    with mock.patch.object(push, "_git",
                           side_effect=_base_git(push_result=_proc("", 128, err))), \
            mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc != 0
    tail = "\n".join(_tail(out, 3))
    assert "NOT PUSHED" in tail, f"network failure buried:\n{tail}"


def test_non_fast_forward_rebase_conflict_verdict_survives_tail(capsys) -> None:
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
        if args[:2] == ["rev-parse", "HEAD"]:
            return _proc(HEAD_SHA + "\n", 0)
        if args[0] == "ls-remote":
            return _proc(f"{OLD_SHA}\trefs/heads/feat\n", 0)
        if args[0] == "push":
            return _proc("", 1, rejected)
        if args[0] == "fetch":
            return _proc("", 0)
        if args[:2] == ["rebase", "FETCH_HEAD"]:
            return _proc("", 1, "CONFLICT (content): Merge conflict in a.py")
        if args[:2] == ["diff", "--name-only"]:
            return _proc("a.py\n", 0)
        if args[:2] == ["log", "--format=%h %an: %s"]:
            return _proc("ccc3333 cj.adams: fix\n", 0)
        if args[:2] == ["rev-list", "--count"]:
            return _proc("1\n", 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git), \
            mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc != 0
    tail = "\n".join(_tail(out, 3))
    assert "NOT PUSHED" in tail, f"paused rebase buried under the how-to:\n{tail}"
    assert "REBASE PAUSED" in tail


def test_push_timeout_unverified_verdict_survives_tail(capsys) -> None:
    """Remote did not move: reported as unverified, and it says NOT PUSHED."""
    boom = subprocess.TimeoutExpired(cmd="git push", timeout=300)
    with mock.patch.object(push, "_git",
                           side_effect=_base_git(push_result=boom,
                                                 ls_remote=OLD_SHA)), \
            mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc != 0
    tail = "\n".join(_tail(out, 3))
    assert "NOT PUSHED" in tail, f"timeout verdict buried:\n{tail}"
    assert "UNVERIFIED" in tail


# ── a push that was never attempted ──────────────────────────────────────

def test_detached_head_says_no_push_was_attempted(capsys) -> None:
    def fake_git(args, timeout=30):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git\n", 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return _proc("HEAD\n", 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 1
    tail = "\n".join(_tail(out, 2))
    assert "NOT PUSHED" in tail
    assert "no push attempted" in tail


def test_not_a_repo_says_no_push_was_attempted(capsys) -> None:
    with mock.patch.object(push, "_git", return_value=_proc(returncode=1)):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 1
    tail = "\n".join(_tail(out, 2))
    assert "NOT PUSHED" in tail
    assert "no push attempted" in tail
