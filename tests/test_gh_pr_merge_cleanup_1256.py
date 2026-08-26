"""`gh-pr-merge:N:...|cleanup` runs the cleanup it used to hand back (#1256).

The op already computed the exact three commands and printed them to be retyped;
measured on this repo, 96 of 99 remote branches were merged and undeleted and 10
`st-wt/NNN` worktrees survived a day. The stated reason for not chaining — a
delete once ran after a merge had failed on a conflict — was answered by the
read-back gate: `MERGED` is read off the remote before anything here runs.

What these pin, and each of them is a constraint from the issue body rather than
a style choice:

* the remote branch goes through `gh api -X DELETE`, **never** `git push
  --delete` — the pre-push hook runs the whole suite per deletion, and 96
  branches that way is about three hours of pytest that looks like progress;
* a worktree that is not `idle` per `git-worktrees` is **refused**, and `cannot
  tell` counts as occupied — `idle` there has to be earned by a probe that
  positively looked;
* `git branch -d`, never `-D`. A squash-merged branch's merge cannot be
  confirmed locally, so `-d` declines: observed on `fix/1207`, whose PR #1212
  had squash-merged. That is a third state, printed — not a failure, and not a
  skip in silence;
* three states per item, per `docs/validators.md` §"Declining instead of
  guessing". A cleanup that could not run must never render as a cleanup that
  had nothing to do;
* the exit code stays a statement about the merge. A refused cleanup is not a
  failed merge.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_merge.py"
_spec = importlib.util.spec_from_file_location("github_pr_merge_cleanup", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


#: The PR head this file's PRs point at. Every delete below is gated on the
#: ref reading back at it (#1281) — a name alone stopped being enough.
HEAD_OID = "0123456789abcdef0123456789abcdef01234567"


class _Calls:
    """Records every outward command the cleanup arm issues."""

    def __init__(self, *, worktrees=None, state="idle", git_rc=None,
                 gh_rc=0, branch_exists=True, ref_sha=HEAD_OID):
        self.worktrees = worktrees if worktrees is not None else []
        self.state = state
        self.git_rc = git_rc or {}
        self.gh_rc = gh_rc
        self.branch_exists = branch_exists
        self.ref_sha = ref_sha
        self.git_calls: list = []
        self.gh_calls: list = []

    def git(self, args, timeout=30):
        self.git_calls.append(list(args))
        if args[:2] == ["rev-parse", "--verify"]:
            rc = 0 if self.branch_exists else 1
            return subprocess.CompletedProcess(args, rc, "", "")
        rc, err = self.git_rc.get(" ".join(args[:2]), (0, ""))
        return subprocess.CompletedProcess(args, rc, "", err)

    def gh(self, args, timeout=30):
        self.gh_calls.append(list(args))
        if "DELETE" not in args:
            # The read-back added by #1281: the ref has to be shown to be this
            # PR's head before the DELETE below is allowed to name it.
            body = json.dumps({"object": {"sha": self.ref_sha}})
            return subprocess.CompletedProcess(args, 0, body, "")
        return subprocess.CompletedProcess(
            args, self.gh_rc, "", "" if self.gh_rc == 0 else "404 Not Found")

    def deletes(self):
        return [c for c in self.gh_calls if "DELETE" in c]

    def worktrees_for(self, branch):
        return (list(self.worktrees), "")

    def worktree_state(self, path):
        return self.state


def _install(monkeypatch, c: _Calls):
    monkeypatch.setattr(m, "_git", c.git)
    monkeypatch.setattr(m, "_gh", c.gh)
    monkeypatch.setattr(m, "_worktrees_for_branch", c.worktrees_for)
    monkeypatch.setattr(m, "_worktree_state", c.worktree_state)
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)


def _row(rows, item):
    for name, state, detail in rows:
        if name == item:
            return (state, detail)
    raise AssertionError(f"no {item!r} row in {rows}")


def _cleanup(head, **kw):
    """`run_cleanup` carrying the provenance a same-repo PR carries (#1281).

    These three keywords default to *unestablished* in the op, and an
    unestablished head refuses every arm — which is the point of them. Stating
    them here keeps this file about what #1256 pinned; the guards themselves
    are pinned in `test_gh_pr_merge_cleanup_containment_1280_1281_1282.py`.
    """
    kw.setdefault("cross_repo", False)
    kw.setdefault("default_branch", "master")
    kw.setdefault("head_oid", HEAD_OID)
    return m.run_cleanup(head, **kw)


# ---------------------------------------------------------------------------
# the argument
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv,expect", [
    (["944"], (False, False)),
    (["944|force"], (True, False)),
    (["944|cleanup"], (False, True)),
    (["944|force|cleanup"], (True, True)),
    (["944", "squash|force|cleanup"], (True, True)),
    (["944", "force", "cleanup"], (True, True)),
])
def test_the_cleanup_token_is_parsed_alongside_force(argv, expect) -> None:
    number, _method, force, cleanup, err = m.parse_argv(argv)
    assert err == "", err
    assert number == "944"
    assert (force, cleanup) == expect


def test_an_unknown_token_is_still_refused_rather_than_ignored() -> None:
    _n, _m, _f, _c, err = m.parse_argv(["944", "cleanupp"])
    assert "cleanupp" in err


def test_the_method_survives_a_piped_token_list() -> None:
    _n, method, force, cleanup, err = m.parse_argv(["944", "rebase|force|cleanup"])
    assert (method, force, cleanup, err) == ("rebase", True, True, "")


# ---------------------------------------------------------------------------
# nothing runs unless the merge was read back off the remote
# ---------------------------------------------------------------------------

def test_an_unconfirmed_merge_skips_all_three_and_runs_nothing(monkeypatch):
    c = _Calls(worktrees=["/w/944"])
    _install(monkeypatch, c)
    rows = _cleanup("fix/924", merged=False)
    assert [s for _i, s, _d in rows] == [m.CLEAN_SKIPPED] * 3
    for _i, _s, detail in rows:
        assert "not confirmed" in detail
    assert c.git_calls == [] and c.gh_calls == []


# ---------------------------------------------------------------------------
# the remote branch — gh api, never git push --delete
# ---------------------------------------------------------------------------

def test_the_remote_branch_is_deleted_through_the_api(monkeypatch):
    c = _Calls()
    _install(monkeypatch, c)
    rows = _cleanup("fix/924", merged=True)
    state, detail = _row(rows, "remote branch")
    assert state == m.CLEAN_DONE, detail
    assert c.deletes() == [["api", "-X", "DELETE",
                            "repos/{owner}/{repo}/git/refs/heads/fix/924"]]
    # And the read-back came first, so the name was established before it was
    # used (#1281): a DELETE is never this op's first word about a ref.
    assert c.gh_calls[0] == ["api",
                             "repos/{owner}/{repo}/git/ref/heads/fix/924"]


def test_no_command_anywhere_pushes_a_deletion(monkeypatch):
    """The pre-push hook runs the entire suite per deletion (~3h for 96)."""
    c = _Calls(worktrees=["/w/944"])
    _install(monkeypatch, c)
    _cleanup("fix/924", merged=True)
    for call in c.git_calls:
        assert "push" not in call, call
        assert "--delete" not in call, call


def test_a_failing_api_delete_is_refused_with_its_reason(monkeypatch):
    c = _Calls(gh_rc=1)
    _install(monkeypatch, c)
    state, detail = _row(_cleanup("fix/924", merged=True), "remote branch")
    assert state == m.CLEAN_REFUSED
    assert "404" in detail


# ---------------------------------------------------------------------------
# the worktree — idle has to be earned
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["occupied", "cannot tell"])
def test_a_worktree_that_is_not_idle_is_refused_not_removed(monkeypatch, state):
    c = _Calls(worktrees=["/w/944"], state=state)
    _install(monkeypatch, c)
    verdict, detail = _row(_cleanup("fix/924", merged=True),
                           "local worktree")
    assert verdict == m.CLEAN_REFUSED
    assert state in detail
    assert not any(call[:2] == ["worktree", "remove"] for call in c.git_calls)


def test_an_idle_worktree_is_removed_without_force(monkeypatch):
    c = _Calls(worktrees=["/w/944"], state="idle")
    _install(monkeypatch, c)
    verdict, detail = _row(_cleanup("fix/924", merged=True),
                           "local worktree")
    assert verdict == m.CLEAN_DONE, detail
    assert ["worktree", "remove", "/w/944"] in c.git_calls
    for call in c.git_calls:
        assert "--force" not in call and "-f" not in call, call


def test_no_worktree_is_a_skip_naming_that_nothing_was_there(monkeypatch):
    c = _Calls(worktrees=[])
    _install(monkeypatch, c)
    verdict, detail = _row(_cleanup("fix/924", merged=True),
                           "local worktree")
    assert verdict == m.CLEAN_SKIPPED
    assert "no worktree" in detail


def test_several_worktrees_on_one_branch_are_refused(monkeypatch):
    c = _Calls(worktrees=["/w/a", "/w/b"], state="idle")
    _install(monkeypatch, c)
    verdict, detail = _row(_cleanup("fix/924", merged=True),
                           "local worktree")
    assert verdict == m.CLEAN_REFUSED
    assert "/w/a" in detail and "/w/b" in detail
    assert not any(call[:2] == ["worktree", "remove"] for call in c.git_calls)


def test_a_failing_removal_is_refused_with_gits_own_reason(monkeypatch):
    c = _Calls(worktrees=["/w/944"], state="idle",
               git_rc={"worktree remove": (1, "contains modified files")})
    _install(monkeypatch, c)
    verdict, detail = _row(_cleanup("fix/924", merged=True),
                           "local worktree")
    assert verdict == m.CLEAN_REFUSED
    assert "modified files" in detail


def test_the_worktree_is_removed_before_the_branch_is_deleted(monkeypatch):
    """`git branch -d` cannot delete a branch checked out in a worktree, so the
    order is load-bearing rather than incidental."""
    c = _Calls(worktrees=["/w/944"], state="idle")
    _install(monkeypatch, c)
    _cleanup("fix/924", merged=True)
    verbs = [call[:2] for call in c.git_calls]
    assert verbs.index(["worktree", "remove"]) < verbs.index(["branch", "-d"])


# ---------------------------------------------------------------------------
# the local branch — -d, and its refusal is a third state
# ---------------------------------------------------------------------------

def test_the_local_branch_is_deleted_with_lowercase_d(monkeypatch):
    c = _Calls()
    _install(monkeypatch, c)
    verdict, detail = _row(_cleanup("fix/924", merged=True),
                           "local branch")
    assert verdict == m.CLEAN_DONE, detail
    assert ["branch", "-d", "fix/924"] in c.git_calls
    for call in c.git_calls:
        assert "-D" not in call, call


def test_a_squash_merge_that_d_cannot_confirm_is_a_finding_not_a_force(
        monkeypatch):
    """Live evidence: `fix/1207` was declined by `-d` although PR #1212 had
    squash-merged. `-d` cannot confirm a squash, and declining is correct."""
    c = _Calls(git_rc={"branch -d": (1, "error: the branch 'fix/924' is not "
                                        "fully merged")})
    _install(monkeypatch, c)
    verdict, detail = _row(_cleanup("fix/924", merged=True),
                           "local branch")
    assert verdict == m.CLEAN_REFUSED
    assert "not fully merged" in detail
    assert "squash" in detail.lower()
    assert not any("-D" in call for call in c.git_calls)


def test_an_absent_local_branch_is_a_skip(monkeypatch):
    c = _Calls(branch_exists=False)
    _install(monkeypatch, c)
    verdict, detail = _row(_cleanup("fix/924", merged=True),
                           "local branch")
    assert verdict == m.CLEAN_SKIPPED
    assert "no local branch" in detail
    assert not any(call[:2] == ["branch", "-d"] for call in c.git_calls)


# ---------------------------------------------------------------------------
# a repo target means the local checkout is somebody else's repo
# ---------------------------------------------------------------------------

def test_a_repo_target_skips_the_local_half(monkeypatch):
    c = _Calls(worktrees=["/w/944"], state="idle")
    _install(monkeypatch, c)
    monkeypatch.setenv("SUPERTOOL_REPO", "other/repo")
    rows = _cleanup("fix/924", merged=True)
    for item in ("local worktree", "local branch"):
        verdict, detail = _row(rows, item)
        assert verdict == m.CLEAN_SKIPPED
        assert "other/repo" in detail
    assert _row(rows, "remote branch")[0] == m.CLEAN_DONE
    assert c.git_calls == []


# ---------------------------------------------------------------------------
# a name that cannot be handled safely is refused, not guessed at
# ---------------------------------------------------------------------------

def test_an_extraordinary_branch_name_refuses_every_item(monkeypatch):
    c = _Calls(worktrees=["/w/944"], state="idle")
    _install(monkeypatch, c)
    rows = _cleanup("-D;rm -rf /", merged=True)
    assert [s for _i, s, _d in rows] == [m.CLEAN_REFUSED] * 3
    assert c.git_calls == [] and c.gh_calls == []


# ---------------------------------------------------------------------------
# rendering: three states, and no state renders as another
# ---------------------------------------------------------------------------

def test_the_section_names_every_state_and_tallies_them() -> None:
    rows = [("local worktree", m.CLEAN_REFUSED, "cannot tell"),
            ("local branch", m.CLEAN_SKIPPED, "no local branch"),
            ("remote branch", m.CLEAN_DONE, "deleted")]
    text = "\n".join(m.render_cleanup(rows))
    assert "refused" in text and "skipped" in text and "done" in text
    assert "1 done, 1 refused, 1 skipped" in text


def test_a_cleanup_that_did_nothing_does_not_render_as_a_clean_sweep() -> None:
    rows = [("local worktree", m.CLEAN_REFUSED, "occupied"),
            ("local branch", m.CLEAN_REFUSED, "not fully merged"),
            ("remote branch", m.CLEAN_REFUSED, "404")]
    text = "\n".join(m.render_cleanup(rows))
    assert "0 done, 3 refused, 0 skipped" in text
    assert "still" in text.lower()


def test_a_changelog_fragment_exists() -> None:
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(1256)


def test_a_printed_command_never_leads_with_cd() -> None:
    """A permission rule matches from the START of the command string, so a
    leading `cd` makes an otherwise-allowed command unrunnable under a prefix
    allow-rule."""
    rows = [("local worktree", m.CLEAN_REFUSED, "occupied")]
    for line in m.render_cleanup(rows):
        assert " cd " not in line and not line.strip().startswith("cd ")
