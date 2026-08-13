"""#1496: `git-worktrees` called a branch with unpushed commits `published`.

Two defects, one call, both the house shape.

1. The row said `the branch is pushed and no open PR tracks it - the work is
   published but unproposed` for the live clone on `master` while
   `git rev-list --left-right --count origin/master...HEAD` said `0 1`. The
   render was collapsing two different questions: *does a remote-tracking ref
   exist* and *is the branch in sync with it*. Read in the direction that
   matters - somebody deciding whether a tree can be discarded - `published`
   reads as `safe to remove`.

2. The same call exited non-zero with no line in the body naming a failure, so
   a caller gating on the status and a caller reading the render disagreed
   about the same call. The exit code is a compression of occupancy into one
   integer; nothing said so where the reader was.

Hermetic: a bare "remote" plus a clone, in a tmp dir, no network.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "worktrees.py"
_spec = importlib.util.spec_from_file_location("git_worktrees_1496", PRESET)
assert _spec is not None and _spec.loader is not None
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

_gc_spec = importlib.util.spec_from_file_location(
    "git_common_1496", ROOT / "presets" / "git" / "_git_common.py")
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
    # `nopr` is passed as well; this is the belt to that braces, so no test
    # here can reach the network even if the flag stops meaning what it means.
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
    """Bare remote + a clone on `feature`, pushed and then moved ahead."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="st1496_")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.mine = os.path.join(self.tmp, "mine")
        assert _run(["init", "--bare", "remote.git"], self.tmp).returncode == 0
        assert _run(["clone", self.remote, "mine"], self.tmp).returncode == 0
        assert _run(["checkout", "-b", "feature"], self.mine).returncode == 0
        self.commit("a.txt", "base")
        assert _run(["push", "-u", "origin", "feature"],
                    self.mine).returncode == 0

    def commit(self, fname: str, msg: str) -> None:
        Path(self.mine, fname).write_text(msg, encoding="utf-8")
        assert _run(["add", fname], self.mine).returncode == 0
        assert _run(["commit", "-m", msg], self.mine).returncode == 0

    def in_repo(self, fn):
        prev = os.getcwd()
        prev_env = {k: os.environ.get(k) for k in _HERMETIC_ENV}
        os.chdir(self.mine)
        os.environ.update({k: v for k, v in _HERMETIC_ENV.items()
                           if v is not None})
        try:
            return fn()
        finally:
            os.chdir(prev)
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def drive(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(PRESET), *argv],
                              cwd=self.mine, env=_HERMETIC_ENV,
                              capture_output=True, text=True, timeout=180,
                              encoding="utf-8", errors="replace")

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture
def box():
    s = _Sandbox()
    try:
        yield s
    finally:
        s.close()


# -- 1. publication is three states, not two ------------------------------

def test_a_branch_ahead_of_its_remote_ref_is_not_reported_published() -> None:
    """The defect, live on `master` on 2026-08-12: one commit ahead, `published`."""
    got = wt.tracker_for("fix/1496", _answered(), {"fix/1496"},
                         sync=wt.Sync(2, ""))
    assert got.state == wt.TRACKER_NONE, got
    assert "the work is published" not in got.detail, got.detail
    assert "2 commit(s)" in got.detail, got.detail
    assert "NOT" in got.detail, got.detail


def test_an_in_sync_branch_is_still_reported_published() -> None:
    got = wt.tracker_for("fix/1496", _answered(), {"fix/1496"},
                         sync=wt.Sync(0, ""))
    assert got.state == wt.TRACKER_NONE, got
    assert "published but unproposed" in got.detail, got.detail


def test_a_sync_measurement_that_failed_declines_instead_of_publishing() -> None:
    """The third state. `git` not answering is not evidence the work is safe."""
    got = wt.tracker_for("fix/1496", _answered(), {"fix/1496"},
                         sync=wt.Sync(None, "git rev-list exited 128"))
    assert got.state == wt.TRACKER_NONE, got
    assert "the work is published" not in got.detail, got.detail
    assert "UNKNOWN" in got.detail, got.detail
    assert "git rev-list exited 128" in got.detail, got.detail


def test_an_unmeasured_sync_is_not_a_publication_claim() -> None:
    """No `sync` handed in at all is the same absence, one layer up."""
    got = wt.tracker_for("fix/1496", _answered(), {"fix/1496"})
    assert got.state == wt.TRACKER_NONE, got
    assert "no open PR" in got.token
    assert "the work is published" not in got.detail, got.detail


def test_the_row_line_itself_says_a_branch_has_unpushed_commits() -> None:
    """The evidence line is not enough: the row line is what gets scanned."""
    tracker = wt.tracker_for("fix/1496", _answered(), {"fix/1496"},
                             sync=wt.Sync(3, ""))
    entry = {"path": "/tmp/st-wt/1496", "branch": "fix/1496", "detached": False,
             "bare": False, "locked": None, "prunable": None, "gitdir": None}
    rows = [(entry, wt.Assessment(wt.STATE_UNKNOWN, ["no positive signal"]),
             tracker)]
    row_line = [ln for ln in wt.render(rows).splitlines()
                if ln.startswith(wt.STATE_UNKNOWN)][0]
    assert "unpushed" in row_line, row_line


# -- the measurement itself ------------------------------------------------

def test_remote_branch_names_keeps_the_full_ref_it_measured_against(box) -> None:
    """A stripped name cannot be handed to `rev-list`; the membership contract
    every caller relies on is unchanged."""
    names, why = box.in_repo(wt.remote_branch_names)
    assert why == "", why
    assert "feature" in names
    assert names["feature"] == "refs/remotes/origin/feature", names


def test_unpushed_for_counts_only_commits_absent_from_the_remote(box) -> None:
    in_sync = box.in_repo(lambda: wt.unpushed_for(
        "feature", "refs/remotes/origin/feature"))
    assert in_sync.ahead == 0, in_sync
    assert in_sync.why == "", in_sync
    box.commit("b.txt", "local work")
    box.commit("c.txt", "more local work")
    ahead = box.in_repo(lambda: wt.unpushed_for(
        "feature", "refs/remotes/origin/feature"))
    assert ahead.ahead == 2, ahead


def test_unpushed_for_declines_when_the_ref_does_not_resolve(box) -> None:
    """`None`, never `0`: an unanswerable count must not read as in sync."""
    got = box.in_repo(lambda: wt.unpushed_for(
        "feature", "refs/remotes/origin/no-such-branch"))
    assert got.ahead is None, got
    assert got.why, got


def test_an_absent_remote_ref_is_not_measured_at_all(box) -> None:
    got = box.in_repo(lambda: wt.unpushed_for("feature", ""))
    assert got.ahead is None, got
    assert got.why, got


def test_sync_for_is_the_wiring_between_the_two(box) -> None:
    """The glue `main` uses: the row's branch, the ref map, one count.

    `main` cannot be driven to the `published` line offline - the PR index has
    to have answered first - so the join is asserted here rather than left to
    the one line nothing covers.
    """
    names, _why = box.in_repo(wt.remote_branch_names)
    # `upstreams` and its `why` joined the signature with #1525 - the count has
    # to be taken against the remote the branch tracks, and `feature` here
    # tracks `origin/feature`, so the numbers below are unchanged.
    ups, up_why = box.in_repo(wt.upstream_refs)

    def sync(branch, mapping=None):
        return wt._sync_for(branch, names if mapping is None else mapping,
                            ups, up_why)

    assert box.in_repo(lambda: sync("feature")).ahead == 0
    box.commit("b.txt", "local work")
    assert box.in_repo(lambda: sync("feature")).ahead == 1
    assert sync("feature", {"feature"}) is None, "a bare set is not a map"
    assert sync("", names) is None
    assert sync("never-pushed") is None


# -- 2. the exit code names itself -----------------------------------------

def test_the_exit_code_is_attributed_in_the_body(box) -> None:
    """A non-zero naming nothing and a zero hiding a failure are both wrong.

    The state is not asserted: the cwd probe reads `/proc` or `lsof`, so a
    fresh tmp repo is `occupied` on some platforms and `cannot tell` on
    others. What is asserted is that whatever integer the op returns is named
    in the body, next to the state that produced it - which is the finding.
    """
    res = box.drive(box.mine, "nopr")
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    notes = [ln for ln in lines if ln.startswith("[exit ")]
    assert notes, res.stdout
    assert notes[0].startswith("[exit %d]" % res.returncode), (notes, res.returncode)
    states = (wt.STATE_OCCUPIED, wt.STATE_IDLE, wt.STATE_UNKNOWN)
    assert any(s in notes[0] for s in states), notes[0]
    assert "did not fail" in notes[0], notes[0]


def test_the_result_tally_is_still_the_last_line(box) -> None:
    """`gh-pr-merge` and every `tail -1` reader take the tally off the end.

    Driven without a PATH: the whole-board form always renders a tally, whereas
    the PATH filter is a `realpath` comparison and a runner whose tmp dir is a
    symlink or a short name is not the subject of this assertion.
    """
    res = box.drive("nopr")
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    assert lines[-1].startswith("[result]"), lines[-3:]


def test_a_refused_argument_names_its_exit_code_too(box) -> None:
    """Measured on master 2026-08-12: `worktrees.py --wat` exited 2 with the
    ERROR and no attribution of the status at all."""
    res = box.drive("--wat")
    assert res.returncode == 2, res.stdout + res.stderr
    notes = [ln for ln in res.stdout.splitlines() if ln.startswith("[exit ")]
    assert notes and notes[0].startswith("[exit 2]"), res.stdout
    assert "refused" in notes[0], notes[0]


def test_a_path_that_is_no_worktree_names_its_exit_code_too(box) -> None:
    """The other unattributed 2. Nothing was inspected, which is `cannot tell`
    and not a failure - and the code alone cannot say which."""
    outside = os.path.join(box.tmp, "elsewhere")
    os.makedirs(outside, exist_ok=True)
    res = box.drive(outside, "nopr")
    assert res.returncode == 2, res.stdout + res.stderr
    notes = [ln for ln in res.stdout.splitlines() if ln.startswith("[exit ")]
    assert notes and notes[0].startswith("[exit 2]"), res.stdout
    assert "cannot tell" in notes[0], notes[0]
    assert "did not fail" in notes[0], notes[0]


def test_a_whole_board_says_its_zero_is_not_a_verdict(box) -> None:
    """No PATH exits 0 whatever the rows found - which is unreadable unless the
    body says the code is about nothing."""
    res = box.drive("nopr")
    assert res.returncode == 0, res.stdout + res.stderr
    notes = [ln for ln in res.stdout.splitlines() if ln.startswith("[exit ")]
    assert notes, res.stdout
    assert "no PATH" in notes[0], notes[0]
    assert "not a verdict" in notes[0], notes[0]
