"""The cleanup arm deleted three things nobody had established (#1280/#1281/#1282).

Each of the three is the same shape: a value arrived from somewhere, and the
code downstream of it acted as though someone had checked it.

* **#1281** — `headRefName` is named by whoever opened the PR, and opening one
  from a fork needs no permission here. The DELETE went to the **base** repo, so
  a fork branch called `master` deleted ours; the receipt then said
  `recoverable: GitHub keeps refs/pull/N/head`, which is false in exactly that
  case, because the ref deleted is not that PR's head.
* **#1280** — `git worktree remove` without `--force` still deletes **ignored**
  files. The refusal text justified safety on the absence of that flag, which is
  the one sentence that is not true: a wrong safety claim terminates the next
  reader's search.
* **#1282** — `git-worktrees:PATH` printed `0 idle` and exited 0 when the path
  matched more than one worktree. The exit code was the authorization for a
  directory removal, so the consumer read the half that was wrong.

The through-line for the fixes: an exit code is one integer standing in for a
render with three states, and a refname is a name until something says where it
points.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_merge.py"
_spec = importlib.util.spec_from_file_location("gh_pr_merge_containment", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

WORKTREES_PY = Path(__file__).parent.parent / "presets" / "git" / "worktrees.py"

OID = "0123456789abcdef0123456789abcdef01234567"


class Shim:
    """Every outward command the cleanup arm issues, and canned answers."""

    def __init__(self, *, worktrees=(), state="idle", dirt="", ref_sha=OID,
                 ref_missing=False, remove_rc=0, branch_exists=False,
                 delete_rc=0):
        self.worktrees = list(worktrees)
        self.state = state
        self.dirt = dirt
        self.ref_sha = ref_sha
        self.ref_missing = ref_missing
        self.remove_rc = remove_rc
        self.branch_exists = branch_exists
        self.delete_rc = delete_rc
        self.git_calls: list = []
        self.gh_calls: list = []

    def git(self, args, timeout=30):
        args = list(args)
        self.git_calls.append(args)
        if "status" in args:
            return subprocess.CompletedProcess(args, 0, self.dirt, "")
        if args[:2] == ["rev-parse", "--verify"]:
            return subprocess.CompletedProcess(
                args, 0 if self.branch_exists else 1, "", "")
        if args[:2] == ["worktree", "remove"]:
            return subprocess.CompletedProcess(
                args, self.remove_rc, "", "" if self.remove_rc == 0 else "busy")
        return subprocess.CompletedProcess(args, 0, "", "")

    def gh(self, args, timeout=30):
        args = list(args)
        self.gh_calls.append(args)
        if "-X" in args and "DELETE" in args:
            return subprocess.CompletedProcess(
                args, self.delete_rc, "",
                "" if self.delete_rc == 0 else "422 Unprocessable")
        if self.ref_missing:
            return subprocess.CompletedProcess(args, 1, "", "404 Not Found")
        body = json.dumps({"ref": "refs/heads/x",
                           "object": {"sha": self.ref_sha, "type": "commit"}})
        return subprocess.CompletedProcess(args, 0, body, "")

    def worktrees_for(self, branch):
        return (list(self.worktrees), "")

    def worktree_state(self, path):
        return self.state

    def deletes(self):
        return [a for a in self.gh_calls if "DELETE" in a]


def install(monkeypatch, s: Shim, *, real_state=False):
    monkeypatch.setattr(m, "_git", s.git)
    monkeypatch.setattr(m, "_gh", s.gh)
    monkeypatch.setattr(m, "_worktrees_for_branch", s.worktrees_for)
    if not real_state:
        monkeypatch.setattr(m, "_worktree_state", s.worktree_state)
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)


def clean(head, **kw):
    """`run_cleanup` with the provenance a same-repo PR would carry."""
    kw.setdefault("merged", True)
    kw.setdefault("cross_repo", False)
    kw.setdefault("default_branch", "master")
    kw.setdefault("head_oid", OID)
    return m.run_cleanup(head, **kw)


def states(rows):
    return {item: state for item, state, _ in rows}


def details(rows):
    return " | ".join(d for _, _, d in rows)


# ---------------------------------------------------------------------------
# #1281 — the head is a name from an untrusted source
# ---------------------------------------------------------------------------

def test_the_pr_fields_ask_whether_the_head_is_in_this_repository() -> None:
    assert "isCrossRepository" in m._PR_FIELDS


def test_a_fork_branch_named_master_deletes_nothing(monkeypatch) -> None:
    s = Shim(worktrees=["/w/master"], branch_exists=True)
    install(monkeypatch, s)
    rows = clean("master", cross_repo=True)
    assert set(states(rows).values()) == {m.CLEAN_REFUSED}
    assert s.deletes() == []
    assert not [c for c in s.git_calls if c[:2] == ["worktree", "remove"]]
    assert not [c for c in s.git_calls if c[:2] == ["branch", "-d"]]


def test_an_unestablished_head_repository_is_refused_not_assumed_ours(
        monkeypatch) -> None:
    s = Shim(worktrees=["/w/x"], branch_exists=True)
    install(monkeypatch, s)
    rows = clean("fix/1281", cross_repo=None)
    assert set(states(rows).values()) == {m.CLEAN_REFUSED}
    assert s.deletes() == []


@pytest.mark.parametrize("head", ["master", "main", "develop"])
def test_the_default_branch_is_never_a_cleanup_target(monkeypatch, head) -> None:
    s = Shim(worktrees=[f"/w/{head}"], branch_exists=True)
    install(monkeypatch, s)
    rows = clean(head, default_branch=head)
    assert set(states(rows).values()) == {m.CLEAN_REFUSED}
    assert s.deletes() == []


def test_an_unknown_default_branch_refuses_rather_than_guessing(
        monkeypatch) -> None:
    s = Shim(branch_exists=True)
    install(monkeypatch, s)
    rows = clean("fix/1281", default_branch="")
    assert set(states(rows).values()) == {m.CLEAN_REFUSED}
    assert s.deletes() == []


def test_the_remote_ref_must_point_at_this_prs_head_before_it_is_deleted(
        monkeypatch) -> None:
    s = Shim(ref_sha="ffffffffffffffffffffffffffffffffffffffff")
    install(monkeypatch, s)
    rows = clean("develop")
    assert states(rows)["remote branch"] == m.CLEAN_REFUSED
    assert s.deletes() == []
    assert "head" in details(rows)


def test_a_ref_the_api_cannot_read_is_refused_not_deleted(monkeypatch) -> None:
    s = Shim(ref_missing=True)
    install(monkeypatch, s)
    rows = clean("fix/1281")
    assert states(rows)["remote branch"] == m.CLEAN_REFUSED
    assert s.deletes() == []


def test_a_verified_head_is_still_deleted_and_the_receipt_says_why(
        monkeypatch) -> None:
    s = Shim()
    install(monkeypatch, s)
    rows = clean("fix/1281")
    assert states(rows)["remote branch"] == m.CLEAN_DONE
    assert len(s.deletes()) == 1
    assert "git/refs/heads/fix/1281" in " ".join(s.deletes()[0])
    detail = [d for i, _, d in rows if i == "remote branch"][0]
    assert OID[:7] in detail and "refs/pull" in detail


def test_the_recoverability_claim_is_never_printed_for_a_ref_not_verified(
        monkeypatch) -> None:
    s = Shim(ref_sha="ffffffffffffffffffffffffffffffffffffffff")
    install(monkeypatch, s)
    assert "refs/pull" not in details(clean("master"))


# ---------------------------------------------------------------------------
# #1280 — `--force` never governed ignored files
# ---------------------------------------------------------------------------

def test_a_worktree_holding_an_ignored_file_is_refused_not_removed(
        monkeypatch) -> None:
    s = Shim(worktrees=["/w/fix"], dirt="!! .env\n!! venv/\n")
    install(monkeypatch, s)
    rows = clean("fix/1280")
    assert states(rows)["local worktree"] == m.CLEAN_REFUSED
    assert not [c for c in s.git_calls if c[:2] == ["worktree", "remove"]]
    detail = [d for i, _, d in rows if i == "local worktree"][0]
    assert ".env" in detail


def test_the_tree_is_asked_about_ignored_files_specifically(monkeypatch) -> None:
    s = Shim(worktrees=["/w/fix"])
    install(monkeypatch, s)
    clean("fix/1280")
    asked = [c for c in s.git_calls if "status" in c]
    assert asked, "the tree was removed without anyone looking inside it"
    assert "--ignored" in asked[0] and "/w/fix" in asked[0]


def test_a_removal_says_it_checked_rather_than_being_as_quiet_as_a_dangerous_one(
        monkeypatch) -> None:
    s = Shim(worktrees=["/w/fix"], dirt="")
    install(monkeypatch, s)
    rows = clean("fix/1280")
    assert states(rows)["local worktree"] == m.CLEAN_DONE
    detail = [d for i, _, d in rows if i == "local worktree"][0]
    assert "ignored" in detail


def test_no_message_claims_safety_from_the_absence_of_force(monkeypatch) -> None:
    s = Shim(worktrees=["/w/fix"], remove_rc=1)
    install(monkeypatch, s)
    rows = clean("fix/1280")
    assert "no --force is ever passed here" not in details(rows)
    src = MOD_PATH.read_text(encoding="utf-8")
    assert "no --force is ever passed here" not in src


# ---------------------------------------------------------------------------
# #1282 — a render with three states, collapsed into one integer
# ---------------------------------------------------------------------------

def _probe(monkeypatch, stdout, returncode):
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, returncode, stdout, "")
    monkeypatch.setattr(m.subprocess, "run", fake_run)


BOARD_MULTI = """# git-worktrees (3)

occupied     master   /w
occupied     outer    /w/outer
occupied     inner    /w/outer/inner

[result] 3 occupied, 0 idle, 0 cannot tell — 'cannot tell' is NOT 'idle'
"""

BOARD_ONE_IDLE = """# git-worktrees (1)

idle         fix/1  /w/outer

[result] 0 occupied, 1 idle, 0 cannot tell — 'cannot tell' is NOT 'idle'
"""

BOARD_NOT_A_WORKTREE = "# git-worktrees\n\ncannot tell   /w/outer\n"


def test_a_board_printing_zero_idle_is_not_idle_however_it_exited(
        monkeypatch) -> None:
    _probe(monkeypatch, BOARD_MULTI, 0)
    assert not m._worktree_state("/w/outer").startswith("idle")


def test_one_idle_row_is_idle(monkeypatch) -> None:
    _probe(monkeypatch, BOARD_ONE_IDLE, 0)
    assert m._worktree_state("/w/outer") == "idle"


def test_a_board_with_no_tally_line_cannot_tell(monkeypatch) -> None:
    _probe(monkeypatch, BOARD_NOT_A_WORKTREE, 0)
    assert m._worktree_state("/w/outer").startswith("cannot tell")


def test_a_multi_match_path_does_not_exit_idle(tmp_path) -> None:
    root = tmp_path / "main"
    root.mkdir()

    def git(*args):
        # encoding/errors are not optional: the Windows runners' locale codec
        # is cp1252, the decode raises inside subprocess's reader thread, and
        # communicate() then hands back None (#856). Pinned by the register
        # test, which caught this file on the first full run.
        return subprocess.run(["git", *args], cwd=str(root),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    git("init", "-q", ".")
    git("config", "user.email", "a@b.c")
    git("config", "user.name", "t")
    (root / "f.txt").write_text("x", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-qm", "init")
    outer = root / "outer"
    git("worktree", "add", "-q", "-b", "outer", str(outer))
    git("worktree", "add", "-q", "-b", "inner", str(outer / "inner"))

    r = subprocess.run([sys.executable, str(WORKTREES_PY), str(outer), "nopr"],
                       cwd=str(outer), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert "0 idle" in r.stdout
    assert r.returncode != 0, (
        "a path matching three worktrees exited idle: " + r.stdout)


def test_a_changelog_fragment_exists() -> None:
    from _changelog_findable import assert_change_is_findable
    for issue in (1280, 1281, 1282):
        assert_change_is_findable(issue)
