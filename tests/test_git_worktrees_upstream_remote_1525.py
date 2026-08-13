"""#1525 - the publication count must be about the remote the branch tracks.

`remote_branch_names` preferred `origin` unconditionally and `_sync_for`
measured against whatever that preference produced, so on a fork layout
(upstream `fork/X`, an `origin/X` at a different commit) the row answered a
question nobody asked - and never named the ref, so the reader could not tell.

Observed on a hermetic two-remote sandbox before the fix: branch `feat` tracks
`fork/feat` and is one commit ahead of it, and the row read *the branch is
pushed, in sync with its remote ref ... the work is published but unproposed*.

Four states, and the receipt has to keep them apart:

tracks a remote, ref present  measured against THAT remote, named in the row
tracks nothing                measured against the same-named ref, said so
upstream ref gone here        NOT measured - deleted upstream, or never fetched
upstreams unreadable          NOT measured - the tool failing to look

Hermetic: two bare "remotes" plus a clone, in a tmp dir, no network.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "worktrees.py"
_spec = importlib.util.spec_from_file_location("git_worktrees_1525", PRESET)
assert _spec is not None and _spec.loader is not None
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

_gc_spec = importlib.util.spec_from_file_location(
    "git_common_1525", ROOT / "presets" / "git" / "_git_common.py")
assert _gc_spec is not None and _gc_spec.loader is not None
gc = importlib.util.module_from_spec(_gc_spec)
_gc_spec.loader.exec_module(gc)

_HERMETIC_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
    "SUPERTOOL_WORKTREE_PR": "0",
}


def _run(args: list, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, env=_HERMETIC_ENV,
                          capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")


def _answered(*prs) -> "gc.PrIndex":
    return gc.PrIndex({p["headRefName"]: p for p in prs}, truncated=False,
                      limit=100)


class _Sandbox:
    """A clone with two remotes and one branch per state under test.

    ``feat``    upstream ``fork/feat``, one commit ahead of it, and
                ``origin/feat`` carrying every commit - the fork layout the
                issue is about, arranged so the two remotes disagree.
    ``solo``    pushed to origin, upstream deliberately unset.
    ``ghost``   upstream ``fork/ghost``, deleted on the fork and pruned here,
                while ``origin/ghost`` still exists.
    """

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st1525_")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.fork = os.path.join(self.tmp, "fork.git")
        self.mine = os.path.join(self.tmp, "mine")
        assert _run(["init", "--bare", "origin.git"], self.tmp).returncode == 0
        assert _run(["init", "--bare", "fork.git"], self.tmp).returncode == 0
        assert _run(["clone", self.origin, "mine"], self.tmp).returncode == 0
        assert _run(["remote", "add", "fork", self.fork], self.mine).returncode == 0
        self.commit("a.txt", "base")
        assert _run(["push", "-u", "origin", "master"], self.mine).returncode == 0

        assert _run(["checkout", "-b", "feat"], self.mine).returncode == 0
        self.commit("a.txt", "two")
        assert _run(["push", "fork", "feat"], self.mine).returncode == 0
        assert _run(["branch", "--set-upstream-to=fork/feat", "feat"],
                    self.mine).returncode == 0
        self.commit("a.txt", "three")
        assert _run(["push", "origin", "feat"], self.mine).returncode == 0

        assert _run(["checkout", "-b", "solo", "master"], self.mine).returncode == 0
        self.commit("a.txt", "solo")
        assert _run(["push", "origin", "solo"], self.mine).returncode == 0
        _run(["config", "--unset", "branch.solo.remote"], self.mine)
        _run(["config", "--unset", "branch.solo.merge"], self.mine)

        assert _run(["checkout", "-b", "ghost", "master"], self.mine).returncode == 0
        self.commit("a.txt", "ghost")
        assert _run(["push", "fork", "ghost"], self.mine).returncode == 0
        assert _run(["branch", "--set-upstream-to=fork/ghost", "ghost"],
                    self.mine).returncode == 0
        assert _run(["push", "origin", "ghost"], self.mine).returncode == 0
        assert _run(["push", "fork", "--delete", "ghost"], self.mine).returncode == 0
        assert _run(["fetch", "fork", "--prune"], self.mine).returncode == 0
        assert _run(["checkout", "feat"], self.mine).returncode == 0

    def commit(self, fname: str, msg: str) -> None:
        Path(self.mine, fname).write_text(msg, encoding="utf-8")
        assert _run(["add", fname], self.mine).returncode == 0
        assert _run(["commit", "-m", msg], self.mine).returncode == 0

    def in_repo(self, fn):
        prev = os.getcwd()
        os.chdir(self.mine)
        try:
            return fn()
        finally:
            os.chdir(prev)

    def state(self, branch: str):
        """`(sync, tracker)` for one branch, the way `main` wires them."""
        def go():
            names, why = wt.remote_branch_names()
            ups, up_why = wt.upstream_refs()
            sync = wt._sync_for(branch, names, ups, up_why)
            return sync, wt.tracker_for(branch, _answered(), names, why, sync=sync)
        return self.in_repo(go)

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def box():
    sandbox = _Sandbox()
    yield sandbox
    sandbox.close()


# -- 1. the remote the row is actually about -------------------------------

def test_upstream_refs_reads_what_each_branch_tracks(box) -> None:
    ups, why = box.in_repo(wt.upstream_refs)
    assert why == "", why
    assert ups["feat"] == ("refs/remotes/fork/feat", "fork"), ups
    assert ups["master"] == ("refs/remotes/origin/master", "origin"), ups
    # Config-derived, so a pruned upstream is still reported as tracked - which
    # is the point: the branch tracks a ref that is not here.
    assert ups["ghost"] == ("refs/remotes/fork/ghost", "fork"), ups
    assert ups["solo"] == ("", ""), ups


def test_a_fork_branch_is_measured_against_its_own_remote(box) -> None:
    """The whole issue. `origin/feat` has every commit; `fork/feat` is behind.

    Before the fix this answered `0` - against a remote the row was never
    about - and called the work published.
    """
    sync, tracker = box.state("feat")
    assert sync.ahead == 1, sync
    assert sync.ref == "refs/remotes/fork/feat", sync
    assert "fork/feat" in tracker.detail, tracker.detail
    assert "NOT published" in tracker.detail, tracker.detail


def test_the_count_names_the_ref_it_was_taken_against(box) -> None:
    """A count against an unnamed remote cannot be checked by the reader."""
    _sync, tracker = box.state("feat")
    assert "refs/remotes/fork/feat" in tracker.detail, tracker.detail


def test_a_branch_in_sync_with_its_upstream_names_that_ref_too(box) -> None:
    _sync, tracker = box.state("master")
    assert "refs/remotes/origin/master" in tracker.detail, tracker.detail
    assert "published" in tracker.detail, tracker.detail


# -- 2. tracks nothing is a third state, not a zero ------------------------

def test_a_branch_with_no_upstream_says_the_ref_was_chosen_by_name(box) -> None:
    """`solo` tracks nothing. The same-named ref is still worth measuring -
    the commits really are on it - but the row may not present it as the
    branch's own remote, because nothing establishes that it is."""
    sync, tracker = box.state("solo")
    assert sync.ahead == 0, sync
    assert sync.ref == "refs/remotes/origin/solo", sync
    assert "refs/remotes/origin/solo" in tracker.detail, tracker.detail
    assert "no upstream" in tracker.detail, tracker.detail


# -- 3. an upstream that is not here is NOT measured ------------------------

def test_a_deleted_upstream_declines_instead_of_measuring_another_remote(box) -> None:
    """`ghost` tracks `fork/ghost`, which is gone. `origin/ghost` exists and
    carries every commit - and measuring against it would answer `0 commits
    missing` about a remote this branch has never had anything to do with."""
    sync, tracker = box.state("ghost")
    assert sync.ahead is None, sync
    assert "fork/ghost" in sync.why, sync.why
    assert "published but unproposed" not in tracker.detail, tracker.detail


def test_not_measured_is_visibly_different_from_measured_and_clean(box) -> None:
    """The row token, not just the evidence line: `ghost` and `master` are
    both `no open PR` with no unpushed count, and they are not the same fact.
    """
    _s, unmeasured = box.state("ghost")
    _s2, in_sync = box.state("master")
    assert unmeasured.token != in_sync.token, unmeasured.token
    assert "not measured" in unmeasured.token, unmeasured.token


def test_the_row_line_itself_says_the_count_was_not_taken(box) -> None:
    """The evidence line is not enough: the row line is what gets scanned, and
    it is where `not measured` and `in sync` used to be the same six words."""
    _sync, tracker = box.state("ghost")
    entry = {"path": "/tmp/st-wt/1525", "branch": "ghost", "detached": False,
             "bare": False, "locked": None, "prunable": None, "gitdir": None}
    rows = [(entry, wt.Assessment(wt.STATE_UNKNOWN, ["no positive signal"]),
             tracker)]
    row_line = [ln for ln in wt.render(rows).splitlines()
                if ln.startswith(wt.STATE_UNKNOWN)][0]
    assert "not measured" in row_line, row_line


def test_upstreams_that_could_not_be_read_decline_rather_than_guess(box) -> None:
    """`None` is the tool failing to look. It must not fall back to the
    origin-preferred ref, which is the guess the issue is about."""
    def go():
        names, _why = wt.remote_branch_names()
        return wt._sync_for("feat", names, None, "git for-each-ref exited 128")
    sync = box.in_repo(go)
    assert sync is not None and sync.ahead is None, sync
    assert "git for-each-ref exited 128" in sync.why, sync.why


def test_an_upstream_pointing_at_a_different_branch_is_not_measured(box) -> None:
    """`git worktree add -b X ... master` can leave X tracking `origin/master`.
    That ref resolves and is on a real remote, and it is still not a remote
    copy of X - measuring against it is #1496's mistake with a fresh face."""
    def go():
        names, _why = wt.remote_branch_names()
        return wt._sync_for(
            "feat", names, {"feat": ("refs/remotes/origin/master", "origin")}, "")
    sync = box.in_repo(go)
    assert sync.ahead is None, sync
    assert "origin/master" in sync.why, sync.why
