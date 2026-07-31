"""Unit tests for presets/git/push.py — receipt + MR line + guards."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest import mock


PRESET = Path(__file__).parent.parent / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


def _proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)


def _porcelain(ref: str, summary: str) -> str:
    """`git push --porcelain` stdout for a rejected ref.

    Since #641 the rejection reason is read off this channel — git's own
    machine-readable per-ref status, on stdout — and not off the merged
    stdout+stderr, which a pre-push hook writes to as well. The fixtures still
    carry the human-readable `! [rejected] feat -> feat (...)` on stderr,
    because that is what the receipt dumps for the caller to read; it is simply
    no longer what any decision is made on.
    """
    return f"To origin\n!\trefs/heads/{ref}:refs/heads/{ref}\t{summary}\nDone\n"


# ── _open_mr_line ────────────────────────────────────────────────────────

def test_mr_line_gitlab_with_pipeline() -> None:
    assert push._open_mr_line({
        "source": "gitlab", "iid": 42, "target": "master", "pipeline": "running"
    }) == "MR !42 → master | pipeline: running"


def test_mr_line_gitlab_no_pipeline_says_triggered() -> None:
    assert push._open_mr_line({
        "source": "gitlab", "iid": 42, "target": "master", "pipeline": None
    }) == "MR !42 → master | pipeline: triggered"


def test_mr_line_gitlab_includes_pipeline_id_and_url() -> None:
    line = push._open_mr_line({
        "source": "gitlab", "iid": 42, "target": "master", "pipeline": "running",
        "pipeline_id": 147316, "pipeline_url": "https://gl/x/-/pipelines/147316"})
    assert "pipeline: running #147316" in line
    assert "https://gl/x/-/pipelines/147316" in line


def test_mr_line_github() -> None:
    assert push._open_mr_line({
        "source": "github", "iid": 7, "target": "main", "pipeline": None
    }) == "PR #7 → main | checks triggered"


def test_mr_line_none_is_empty() -> None:
    assert push._open_mr_line(None) == ""


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


def test_main_non_ff_rebase_clean_pushes(capsys) -> None:
    """Non-ff → fetch, surface incoming commits, rebase clean → re-push."""
    rejected = ("To origin\n ! [rejected] feat -> feat (non-fast-forward)\n"
                "error: failed to push some refs")
    porc = _porcelain("feat", "[rejected] (non-fast-forward)")
    pushes = iter([_proc(porc, 1, rejected), _proc("", 0)])  # first non-ff, then ok

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
            return _proc("localhead000\n", 0)
        if args[0] == "ls-remote":
            return _proc("remotehead999\trefs/heads/feat\n", 0)  # != HEAD
        if args[0] == "fetch":
            return _proc("", 0)
        if args[:2] == ["log", "--format=%h %an: %s"]:
            return _proc("bbb2222 cj.adams: fix wallet rounding\n", 0)
        if args[:2] == ["rev-list", "--count"] and "FETCH_HEAD..HEAD" in args:
            return _proc("1\n", 0)
        if args[:2] == ["rebase", "FETCH_HEAD"]:
            return _proc("Successfully rebased\n", 0)
        if args[0] == "push":
            return next(pushes)
        if args[:2] == ["rev-list", "--left-right"]:
            return _proc("0\t0\n", 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git), \
         mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "fetching to rebase" in out
    assert "Remote added 1 commit(s) you lack; replaying 1 of yours" in out
    assert "cj.adams: fix wallet rounding" in out
    assert "Rebase clean" in out
    assert "pushed ✓ (rebased onto remote)" in out
    assert "REJECTED" not in out


def test_main_non_ff_rebase_conflict_leaves_paused_and_points_to_git_conflicts(capsys) -> None:
    """Non-ff → rebase conflicts → leave paused, list files, recommend git-conflicts."""
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
            return _proc("localhead000\n", 0)
        if args[0] == "ls-remote":
            return _proc("remotehead999\trefs/heads/feat\n", 0)
        if args[0] == "fetch":
            return _proc("", 0)
        if args[:2] == ["log", "--format=%h %an: %s"]:
            return _proc("ddd4444 cj.adams: refactor wallet\n", 0)
        if args[:2] == ["rev-list", "--count"] and "FETCH_HEAD..HEAD" in args:
            return _proc("2\n", 0)
        if args[:2] == ["rebase", "FETCH_HEAD"]:
            return _proc("", 1, "CONFLICT (content): Merge conflict in src/foo.php")
        if args[:2] == ["diff", "--name-only"]:
            return _proc("src/foo.php\n", 0)
        if args[:2] == ["rebase", "--abort"]:
            raise AssertionError("must NOT abort — leave paused for git-conflicts")
        if args[0] == "push":
            return _proc(_porcelain("feat", "[rejected] (non-fast-forward)"),
                         1, rejected)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "REBASE PAUSED" in out
    assert "src/foo.php" in out
    assert "Remote added 1 commit(s) you lack; replaying 2 of yours" in out
    assert "cj.adams: refactor wallet" in out  # who I'd be forcing over
    assert "check the author" in out
    assert "git-conflicts" in out
    assert "rebase --continue" in out
    assert "rebase --abort" in out  # cancel path is surfaced explicitly
    assert "cancel" in out
    assert "force-with-lease" in out


def test_main_protected_branch_rejection_no_rebase_hint(capsys) -> None:
    """Server-side rejection (protected branch) must NOT advise pull --rebase."""
    rejected = ("To origin\n ! [remote rejected] main -> main "
                "(protected branch hook declined)\nerror: failed to push some refs")
    porc = _porcelain("main", "[remote rejected] (protected branch hook declined)")

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
            return _proc(porc, 1, rejected)
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


# ── flag parsing ─────────────────────────────────────────────────────────

def test_parse_flags_collects_known_ignores_rest() -> None:
    assert push._parse_flags(["force-with-lease", "no-verify"]) == {
        "force-with-lease", "no-verify"}
    assert push._parse_flags(["FORCE-WITH-LEASE"]) == {"force-with-lease"}
    assert push._parse_flags(["bogus", "42", ""]) == set()


def test_split_upstream() -> None:
    assert push._split_upstream("origin/feat", "feat") == ("origin", "feat")
    assert push._split_upstream("up/team/x", "x") == ("up", "team/x")
    assert push._split_upstream("", "feat") == ("origin", "feat")


# ── _first_error_line skips success banners (issue #297) ─────────────────

def test_first_error_line_skips_green_success_line() -> None:
    text = ("To origin\n"
            "✅ Formatting done. 0 errors.\n"
            "error: failed to push some refs to 'origin'")
    assert push._first_error_line(text) == (
        "error: failed to push some refs to 'origin'")


def test_first_error_line_surfaces_fatal_despite_success_phrase() -> None:
    # Hard error keyword wins over a success phrase — don't hide a real fatal.
    text = "Fatal error: Uncaught RuntimeException: branch pushed successfully."
    assert push._first_error_line(text) == text


# ── hook amended HEAD + pushed, exited non-zero (issue #297) ─────────────

def test_main_hook_amended_head_reports_pushed(capsys) -> None:
    """Non-zero push, but the remote already matches the rewritten HEAD."""
    heads = iter(["old0000aaaa", "new1111bbbb"])  # before, after-amend

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
            return _proc(next(heads) + "\n", 0)
        if args[0] == "push":
            return _proc("", 1, "! [rejected]\n✅ format done. 0 errors.")
        if args[0] == "ls-remote":
            return _proc("new1111bbbb\trefs/heads/feat\n", 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git), \
         mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "PUSHED (pre-push hook amended HEAD)" in out
    assert "Local HEAD rewritten old0000 → new1111" in out
    assert "now at new1111" in out
    assert "REJECTED" not in out


def test_main_hook_pushed_without_amend_reports_pushed(capsys) -> None:
    """Non-zero push, HEAD unchanged, but remote already matches it."""
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
            return _proc("same000cccc\n", 0)
        if args[0] == "push":
            return _proc("", 1, "hook exited 1")
        if args[0] == "ls-remote":
            return _proc("same000cccc\trefs/heads/feat\n", 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git), \
         mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "remote already matches HEAD" in out
    assert "Local HEAD rewritten" not in out
    assert "REJECTED" not in out


def test_main_force_with_lease_and_no_verify_flags(capsys) -> None:
    """Flags reach the git push invocation and the receipt header."""
    calls: list[list[str]] = []

    def fake_git(args, timeout=30):
        calls.append(list(args))
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git\n", 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "@{upstream}" not in args:
            return _proc("feat\n", 0)
        if args[0] == "rev-parse" and "@{upstream}" in args:
            return _proc("origin/feat\n", 0)
        if args[0] == "rev-parse" and args[1] == "--short":
            return _proc("aaa1111\n", 0)
        if args[:2] == ["rev-parse", "HEAD"]:
            return _proc("h0000000\n", 0)
        if args[0] == "push":
            return _proc("", 0)
        if args[:2] == ["rev-list", "--left-right"]:
            return _proc("0\t0\n", 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git), \
         mock.patch.object(push, "query_open_mr", return_value=None), \
         mock.patch.object(push.sys, "argv",
                           ["push.py", "force-with-lease", "no-verify"]):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    push_calls = [c for c in calls if c and c[0] == "push"]
    assert push_calls and "--force-with-lease" in push_calls[0]
    assert "--no-verify" in push_calls[0]
    assert "Flags: force-with-lease, no-verify" in out


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
    assert ["push", "--porcelain", "-u", "origin", "HEAD"] in calls
    assert "branch created" in out


# ── post-push advisories ─────────────────────────────────────────────────

def test_watch_target_maps_source() -> None:
    assert push._watch_target({"source": "gitlab", "iid": 42}) == ("gitlab-mr", "42")
    assert push._watch_target({"source": "github", "iid": 7}) == ("github-pr", "7")
    assert push._watch_target(None) is None
    assert push._watch_target({"source": "gitlab", "iid": "?"}) is None


def test_uncommitted_leftovers_lists_porcelain() -> None:
    with mock.patch.object(push, "_git", return_value=_proc(" M a.py\n?? b.py\n")):
        assert push._uncommitted_leftovers() == [" M a.py", "?? b.py"]


def test_discarded_by_force_lists_commits() -> None:
    with mock.patch.object(push, "_git",
                           return_value=_proc("abc cj.adams: x\ndef theminh: y\n")):
        assert push._discarded_by_force("old123") == [
            "abc cj.adams: x", "def theminh: y"]


def test_discarded_by_force_empty_sha() -> None:
    assert push._discarded_by_force("") == []


def _advisory_git(rev_list_count: str = "", porcelain: str = ""):
    def fake_git(args, timeout=30):
        if args[:2] == ["rev-list", "--count"]:
            return _proc(rev_list_count, 0)
        if args[:2] == ["status", "--porcelain"]:
            return _proc(porcelain, 0)
        return _proc("", 0)
    return fake_git


def test_advisories_mergeability_warn(capsys) -> None:
    mr = {"source": "gitlab", "iid": 42, "target": "master",
          "merge_status": "cannot_be_merged"}
    with mock.patch.object(push, "_git", side_effect=_advisory_git()):
        push._post_push_advisories(mr, set())
    out = capsys.readouterr().out
    assert "conflicts with master" in out
    assert "Watch pipeline: ./supertool 'watch:gitlab-mr:42'" in out


def test_advisories_behind_target_warn(capsys) -> None:
    mr = {"source": "gitlab", "iid": 42, "target": "master"}
    with mock.patch.object(push, "_git",
                           side_effect=_advisory_git(rev_list_count="3\n")):
        push._post_push_advisories(mr, set())
    out = capsys.readouterr().out
    assert "3 commit(s) behind origin/master" in out


def test_advisories_uncommitted_leftovers_warn(capsys) -> None:
    mr = {"source": "gitlab", "iid": 42, "target": "master"}
    with mock.patch.object(push, "_git",
                           side_effect=_advisory_git(porcelain=" M x.py\n?? y.py\n")):
        push._post_push_advisories(mr, set())
    out = capsys.readouterr().out
    assert "2 change(s) NOT in this push" in out
    # #623: the count is the signal; the listing used to bury the push verdict
    # under it, so the files moved behind a named escape hatch.
    assert " M x.py" not in out
    assert "git-status:full" in out


def test_advisories_autowatch_flag_spawns(capsys) -> None:
    mr = {"source": "gitlab", "iid": 42, "target": "master"}
    with mock.patch.object(push, "_git", side_effect=_advisory_git()), \
         mock.patch.object(push, "_spawn_watch", return_value=True) as sp:
        push._post_push_advisories(mr, {"watch"})
    out = capsys.readouterr().out
    sp.assert_called_once_with("gitlab-mr", "42")
    assert "Watching →" in out
    assert "Watch pipeline" not in out


def test_main_non_ff_fetch_failure_rejects_not_false_clean(capsys) -> None:
    """Non-ff, but the fetch fails → REJECTED with the real cause, no false clean."""
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
            return _proc("localhead000\n", 0)
        if args[0] == "ls-remote":
            return _proc("remotehead999\trefs/heads/feat\n", 0)
        if args[0] == "fetch":
            return _proc("", 1, "fatal: unable to access 'origin': could not resolve host")
        if args[0] == "push":
            return _proc(_porcelain("feat", "[rejected] (non-fast-forward)"),
                         1, rejected)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc != 0
    assert "fetch of origin/feat failed" in out
    assert "Rebase clean" not in out
    assert "REBASE PAUSED" not in out


def test_main_force_with_lease_stale_gives_correct_hint(capsys) -> None:
    """A stale-lease rejection must NOT get the 'protected branch' hint."""
    rejected = ("To origin\n ! [rejected] feat -> feat (stale info)\n"
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
            return _proc("localhead000\n", 0)
        if args[0] == "ls-remote":
            return _proc("remotehead999\trefs/heads/feat\n", 0)
        if args[0] == "push":
            return _proc(_porcelain("feat", "[rejected] (stale info)"),
                         1, rejected)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git), \
         mock.patch.object(push.sys, "argv", ["push.py", "force-with-lease"]):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc != 0
    assert "lease is stale" in out
    assert "protected branch" not in out
    assert "REBASE PAUSED" not in out  # force suppresses auto-rebase


def test_main_non_ff_rebase_cannot_start_aborts_not_phantom_paused(capsys) -> None:
    """Rebase fails to start (no unmerged paths) → abort + 'could not start', not PAUSED."""
    rejected = ("To origin\n ! [rejected] feat -> feat (non-fast-forward)\n"
                "error: failed to push some refs")
    aborted: list[bool] = []

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
            return _proc("localhead000\n", 0)
        if args[0] == "ls-remote":
            return _proc("remotehead999\trefs/heads/feat\n", 0)
        if args[0] == "fetch":
            return _proc("", 0)
        if args[:2] == ["log", "--format=%h %an: %s"]:
            return _proc("", 0)
        if args[:2] == ["rev-list", "--count"]:
            return _proc("0\n", 0)
        if args[:2] == ["rebase", "FETCH_HEAD"]:
            return _proc("", 128, "fatal: invalid upstream 'origin/feat'")
        if args[:2] == ["diff", "--name-only"]:
            return _proc("", 0)  # no unmerged paths — rebase never started
        if args[:2] == ["rebase", "--abort"]:
            aborted.append(True)
            return _proc("", 0)
        if args[0] == "push":
            return _proc(_porcelain("feat", "[rejected] (non-fast-forward)"),
                         1, rejected)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc != 0
    assert "could not start" in out
    assert "REBASE PAUSED" not in out
    assert aborted, "must restore a clean tree when the rebase never started"


def test_main_hook_amended_head_surfaces_advisories(capsys) -> None:
    """Hook-amend success path carries the post-push advisories (watch, mergeability)."""
    heads = iter(["old0000aaaa", "new1111bbbb"])

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
            return _proc(next(heads) + "\n", 0)
        if args[0] == "push":
            return _proc("", 1, "! [rejected]\n✅ done. 0 errors.")
        if args[0] == "ls-remote":
            return _proc("new1111bbbb\trefs/heads/feat\n", 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git), \
         mock.patch.object(push, "query_open_mr", return_value={
             "source": "gitlab", "iid": 42, "target": "master",
             "merge_status": "cannot_be_merged"}):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "PUSHED (pre-push hook amended HEAD)" in out
    assert "watch:gitlab-mr:42" in out
    assert "conflicts with master" in out


def test_main_force_aftermath_lists_discarded(capsys) -> None:
    """force-with-lease success → reports the remote commits it overwrote."""
    shas = iter(["aaa1111", "bbb2222"])  # remote before, after

    def fake_git(args, timeout=30):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc(".git\n", 0)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "@{upstream}" not in args:
            return _proc("feat\n", 0)
        if args[0] == "rev-parse" and "@{upstream}" in args:
            return _proc("origin/feat\n", 0)
        if args[0] == "rev-parse" and args[1] == "--short":
            return _proc(next(shas) + "\n", 0)
        if args[:2] == ["rev-parse", "HEAD"]:
            return _proc("head000\n", 0)
        if args[0] == "push":
            return _proc("", 0)
        if args[:3] == ["log", "--format=%h %an: %s", "aaa1111"]:
            return _proc("zzz9999 cj.adams: work I just nuked\n", 0)
        if args[:2] == ["rev-list", "--count"] and "@{upstream}" in args[-1]:
            return _proc("", 0)
        if args[:2] == ["rev-list", "--left-right"]:
            return _proc("0\t0\n", 0)
        if args[:2] == ["status", "--porcelain"]:
            return _proc("", 0)
        return _proc("", 0)

    with mock.patch.object(push, "_git", side_effect=fake_git), \
         mock.patch.object(push, "query_open_mr", return_value=None), \
         mock.patch.object(push.sys, "argv", ["push.py", "force-with-lease"]):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Force discarded 1 remote commit(s)" in out
    assert "cj.adams: work I just nuked" in out


# ── push subprocess timeout — verdict comes from the remote (issue #399) ──

def _timeout_git(remote_sha: str, head: str = "head000aaaa",
                 heads: object = None):
    """fake _git where `push` blows its budget; ls-remote answers `remote_sha`."""
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
            return _proc((next(heads) if heads is not None else head) + "\n", 0)
        if args[0] == "push":
            raise subprocess.TimeoutExpired(cmd="git push", timeout=timeout)
        if args[0] == "ls-remote":
            return _proc(f"{remote_sha}\trefs/heads/feat\n", 0) if remote_sha else _proc("", 1)
        return _proc("", 0)
    return fake_git


def test_main_push_timeout_with_remote_at_head_reports_pushed(capsys) -> None:
    """The push blew its budget but the ref landed — that is a success, not a FAIL.

    A slow pre-push hook (static analysis over every changed file) can outlast
    the budget after git has already handed the refs to the remote. Reporting
    failure sends the caller into re-push / force-push recovery for a push that
    already landed.
    """
    with mock.patch.object(push, "_git", side_effect=_timeout_git("head000aaaa")), \
         mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Status: pushed ✓ (push timed out locally; remote ref matches HEAD)" in out
    assert "Remote origin/feat now at head000" in out
    assert "REJECTED" not in out
    assert "TIMED OUT ✗" not in out


def test_main_push_timeout_after_hook_amend_reports_rewritten_head(capsys) -> None:
    """Hook amended HEAD then timed out: verify against the NEW head, report it."""
    heads = iter(["old0000aaaa", "new1111bbbb"])  # before, after-amend
    with mock.patch.object(push, "_git",
                           side_effect=_timeout_git("new1111bbbb", heads=heads)), \
         mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Status: pushed ✓ (push timed out locally; remote ref matches HEAD)" in out
    assert "Local HEAD rewritten old0000 → new1111" in out
    assert "Remote origin/feat now at new1111" in out


def test_main_push_timeout_with_remote_behind_reports_timeout_not_pushed(capsys) -> None:
    """Remote did NOT move → honest 'timed out, unverified', never a clean success."""
    with mock.patch.object(push, "_git", side_effect=_timeout_git("stale999ccc")), \
         mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "Status: PUSH TIMED OUT ✗ — remote ref does NOT match local HEAD" in out
    assert "local HEAD head000 | remote origin/feat at stale99" in out
    assert "git fetch" in out
    assert "Status: pushed ✓" not in out


def test_main_push_timeout_unreadable_remote_reports_unknown(capsys) -> None:
    """ls-remote itself fails → say the remote state is unknown, don't guess."""
    with mock.patch.object(push, "_git", side_effect=_timeout_git("")), \
         mock.patch.object(push, "query_open_mr", return_value=None):
        rc = push.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "Status: PUSH TIMED OUT ✗ — remote ref does NOT match local HEAD" in out
    assert "remote origin/feat at unknown" in out
    assert "Status: pushed ✓" not in out


def test_live_remote_sha_survives_ls_remote_timeout() -> None:
    """The verification probe must not turn into the crash it exists to prevent."""
    def fake_git(args, timeout=30):
        raise subprocess.TimeoutExpired(cmd="git ls-remote", timeout=timeout)

    with mock.patch.object(push, "_git", side_effect=fake_git):
        assert push._live_remote_sha("origin", "feat") == ""


def test_push_budget_is_strictly_below_the_op_timeout_cap() -> None:
    """push.py must own its timeout — the op cap killing it first loses the verdict.

    Equal budgets (both 120s) meant supertool's cap always fired first, so the
    verification path below could never run and every slow push was reported as
    a bare `FAIL (timeout …)`.
    """
    preset = Path(__file__).parent.parent / "presets" / "git.json"
    op_timeout = json.loads(preset.read_text(encoding="utf-8"))["ops"]["git-push"]["timeout"]
    assert push._PUSH_TIMEOUT < op_timeout
