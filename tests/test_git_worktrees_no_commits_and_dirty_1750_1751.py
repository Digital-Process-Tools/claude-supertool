"""#1750 + #1751 — the two verdict columns the reap board decides on.

Both are this repo's standing defect class landing on the one op whose job is to
say which tree is safe to delete: **an absence produced by the tool, read as an
absence in the world.**

#1750 — a worktree whose branch has never committed rendered `[merged]`, the
same cell a finished-and-landed branch gets. "Every commit is already an
ancestor of master" is vacuously true of nothing.

#1751 — a detached worktree rendered `merged: n/a` and `idle` at exit 0, and
**nothing anywhere answered whether it held uncommitted work** — the one fact a
`git worktree remove` destroys.

## The measurement that shaped the fix, and why the issue's own test does not work

#1750 proposes `git rev-list master..<branch>` being empty as "the direct test".
It cannot work, because it is the *same predicate* the op already runs: a branch
is an ancestor of the base **if and only if** `base..branch` is empty. Measured
on git 2.46.2, in a repo holding `feat/real` (three commits, merged `--no-ff`)
and `fix/new` (created from master, zero commits):

    for-each-ref --merged master  ->  feat/real, fix/new, master
    rev-list --count master..feat/real  ->  0
    rev-list --count master..fix/new    ->  0

Identical. The commit graph cannot separate "landed" from "never started",
because both leave the branch tip reachable from the base. The **reflog** can,
and it is local, free and offline — the same repo, same run:

    feat/real@{0} commit: real work
    feat/real@{1} branch: Created from HEAD
    fix/new@{0}   branch: Created from master

Confirmed against the live fleet the same day: `fix/1750` carried one entry
(`branch: Created from origin/master`), `fix/1708`/`fix/1743`/`fix/1748` each
carried `commit:` entries.

So the ancestry arm keeps its `merged`, and only **positive** evidence that the
branch never moved downgrades it. A reflog that does not answer is the third
state, never a silent fall-through to `merged`.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _changelog_findable import assert_change_is_findable  # noqa: E402

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "worktrees.py"
_spec = importlib.util.spec_from_file_location("git_worktrees_1750_1751", PRESET)
assert _spec is not None and _spec.loader is not None
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

NUL = chr(0)
LF = chr(10)


def _committed(answer):
    """A stub reflog probe. `True` / `False` / `None`, the three states."""
    return lambda branch: answer


# ── #1750: the merge column ──────────────────────────────────────────────

def test_a_branch_that_never_committed_is_not_rendered_merged() -> None:
    """The defect. Seven of eight live trees rendered this cell on 62bc3f0."""
    got = wt.merged_for("fix/1750", {"fix/1750"}, None,
                        ever_committed=_committed(False))
    assert got.state == wt.MERGED_NO_COMMITS, got
    assert "merged" not in got.token, got.token
    assert "no commits" in got.token, got.token
    # The sentence that was true and misleading must not survive either.
    assert "every commit is already an ancestor" not in got.detail, got.detail


def test_a_genuinely_merged_ancestor_still_says_merged() -> None:
    """The must-fire control for the case above, in the same fixture.

    Without this, `no commits yet` everywhere would pass the test above while
    destroying the column. A branch that DID commit and is an ancestor is
    merged for certain, offline, and must keep saying so.
    """
    got = wt.merged_for("feat/real", {"feat/real"}, None,
                        ever_committed=_committed(True))
    assert got.state == wt.MERGED_YES, got
    assert got.token == "merged", got.token
    assert "ancestor" in got.detail, got.detail


def test_a_reflog_that_did_not_answer_is_unknown_and_never_merged() -> None:
    """The third state, and the one that decides whether this is a real fix.

    A probe that could not look must not return the shape of a clean result.
    `merged` here is exactly the absence-read-as-an-answer this issue is about,
    one layer down.
    """
    got = wt.merged_for("fix/x", {"fix/x"}, None,
                        ever_committed=_committed(None))
    assert got.state == wt.MERGED_UNKNOWN, got
    assert "unknown" in got.token.lower(), got.token
    assert got.token != "merged"


def test_the_no_commits_row_says_the_work_would_be_uncommitted() -> None:
    """The render has to be actionable, not merely not-wrong."""
    got = wt.merged_for("fix/1750", {"fix/1750"}, None,
                        ever_committed=_committed(False))
    low = got.detail.lower()
    assert "reflog" in low, got.detail
    assert "uncommitted" in low, got.detail


def test_a_non_ancestor_never_consults_the_reflog() -> None:
    """The probe is scoped to the one arm that needs it.

    A squash-merged branch is not an ancestor and is answered by the PR page;
    spending a git call per row on branches it cannot inform is cost with no
    answer attached.
    """
    calls = []

    def probe(branch):
        calls.append(branch)
        return False

    got = wt.merged_for("fix/1216", set(), None, ever_committed=probe)
    assert got.state == wt.MERGED_UNKNOWN, got
    assert calls == [], calls


# ── the reflog reader itself ─────────────────────────────────────────────

def _reflog(stdout, rc=0):
    def runner(args):
        return subprocess.CompletedProcess(args, rc, stdout, "")
    return runner


def test_only_a_creation_entry_means_the_branch_never_moved() -> None:
    got = wt.branch_ever_committed(
        "fix/1750", runner=_reflog("branch: Created from origin/master" + LF))
    assert got is False, got


def test_a_commit_entry_means_the_branch_moved() -> None:
    got = wt.branch_ever_committed("fix/1748", runner=_reflog(
        "commit: A superscript in a count now refuses (#1748)" + LF
        + "branch: Created from origin/master" + LF))
    assert got is True, got


def test_a_reflog_git_could_not_read_is_none_not_false() -> None:
    """rc != 0 and an empty log are both `cannot look`, never `never moved`.

    `core.logAllRefUpdates` off, a bare repo, or a fresh clone that did not
    create the branch locally all land here. Returning `False` would render
    `no commits yet` about a branch that may hold a year of work.
    """
    assert wt.branch_ever_committed("b", runner=_reflog("", rc=128)) is None
    assert wt.branch_ever_committed("b", runner=_reflog("")) is None
    assert wt.branch_ever_committed("b", runner=_reflog("   " + LF)) is None


def test_an_empty_branch_name_is_never_probed() -> None:
    def boom(args):
        raise AssertionError("a detached tree has no branch to ask about")
    assert wt.branch_ever_committed("", runner=boom) is None


# ── #1751: the dirty column ──────────────────────────────────────────────

def _status(stdout, rc=0):
    def runner(args):
        return subprocess.CompletedProcess(args, rc, stdout, "")
    return runner


def test_a_tree_with_uncommitted_work_reports_dirty_with_a_count() -> None:
    got = wt.dirty_for("/tmp/w", runner=_status(
        " M presets/git/worktrees.py" + NUL + "?? scratch.txt" + NUL))
    assert got.state == wt.DIRTY_DIRTY, got
    assert got.count == 2, got
    assert "dirty" in got.token, got.token
    assert "2" in got.token, got.token


def test_a_clean_tree_reports_clean_out_loud() -> None:
    """The must-fire control, and #1229's lesson applied to a new column.

    A row carrying nothing reads as the reassuring answer. `clean` is a
    rendered word here, not the absence of one.
    """
    got = wt.dirty_for("/tmp/w", runner=_status(""))
    assert got.state == wt.DIRTY_CLEAN, got
    assert got.count == 0, got
    assert "clean" in got.token, got.token


def test_a_status_that_did_not_answer_is_unknown_and_never_clean() -> None:
    """The load-bearing third state.

    A timeout, a missing directory or a git that failed must not render as the
    green light. This is the whole reason the column is worth adding: `clean`
    earned by silence is what `idle` earned by silence already was.
    """
    for rc in (1, 128, 124):
        got = wt.dirty_for("/tmp/w", runner=_status("", rc=rc))
        assert got.state == wt.DIRTY_UNKNOWN, (rc, got)
        assert "clean" not in got.token, (rc, got.token)
        assert "unknown" in got.token.lower(), (rc, got.token)
        assert "NOT" in got.detail, got.detail


def test_a_rename_record_is_counted_once() -> None:
    """`-z` gives a rename TWO NUL fields for ONE record.

    Counting fields reports two changes for one renamed file. The count is
    printed, so it has to be the number of changes and not the number of
    fields.
    """
    got = wt.dirty_for("/tmp/w", runner=_status(
        "R  new.py" + NUL + "old.py" + NUL + " M other.py" + NUL))
    assert got.count == 2, got
    assert got.state == wt.DIRTY_DIRTY, got


def test_the_scan_is_read_only_and_takes_no_lock() -> None:
    """`git status` refreshes the index, and this op promises it writes nothing.

    Without `--no-optional-locks` the scan can write `.git/index` inside a tree
    a live agent is holding — and perturb the mtime probe this same op reads
    one column to the left.
    """
    seen = {}

    def runner(args):
        seen["args"] = list(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    wt.dirty_for("/tmp/w", runner=runner)
    assert "--no-optional-locks" in seen["args"], seen["args"]
    assert "-z" in seen["args"], seen["args"]
    assert "--porcelain" in seen["args"], seen["args"]
    # #1290/#1295: without this pin, a tree whose only uncommitted work is
    # UNTRACKED reports no records under `status.showUntrackedFiles=no` — a
    # user display preference turning a destructive decision green.
    assert "status.showUntrackedFiles=normal" in seen["args"], seen["args"]


# ── #1751: the exit code the reap is gated on ────────────────────────────

def test_a_dirty_idle_tree_does_not_exit_zero() -> None:
    assert wt.EXIT_DIRTY != wt.EXIT_IDLE
    assert wt.exit_code_for(wt.STATE_IDLE, wt.DIRTY_DIRTY) == wt.EXIT_DIRTY


def test_a_clean_idle_tree_still_exits_zero() -> None:
    """The must-fire control: the green light must remain reachable."""
    assert wt.exit_code_for(wt.STATE_IDLE, wt.DIRTY_CLEAN) == wt.EXIT_IDLE


def test_an_unreadable_dirty_scan_cannot_certify_the_tree() -> None:
    assert wt.exit_code_for(wt.STATE_IDLE, wt.DIRTY_UNKNOWN) == wt.EXIT_UNKNOWN


def test_occupancy_still_outranks_the_new_column() -> None:
    """Occupied is the more urgent fact and keeps its own integer."""
    assert wt.exit_code_for(wt.STATE_OCCUPIED, wt.DIRTY_DIRTY) == wt.EXIT_OCCUPIED
    assert wt.exit_code_for(wt.STATE_UNKNOWN, wt.DIRTY_CLEAN) == wt.EXIT_UNKNOWN


def test_the_registry_declares_the_new_code_and_does_not_call_it_clean() -> None:
    """A code supertool does not know is read as a refusal; one it calls clean
    is read as permission. This has to be in `values` and out of `clean`."""
    import json
    entry = json.loads((ROOT / "presets" / "git.json").read_text(
        encoding="utf-8"))["ops"]["git-worktrees"]
    decl = entry["exitStatus"]
    assert wt.EXIT_DIRTY in decl["values"], decl
    assert wt.EXIT_DIRTY not in decl["clean"], decl
    assert decl["clean"] == [0], decl


# ── end to end, against real git ─────────────────────────────────────────

def _git(*args, cwd):
    return subprocess.run(["git"] + list(args), cwd=str(cwd),
                          capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")


def _fleet(tmp_path):
    """A repo holding every case the two issues name, built with real git."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "master", ".", cwd=repo)
    _git("config", "user.email", "t@t.invalid", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "a.txt").write_text("a" + LF)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "seed", cwd=repo)

    # A branch that did real work and landed with a real merge.
    _git("checkout", "-q", "-b", "feat/real", cwd=repo)
    (repo / "b.txt").write_text("b" + LF)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "real work", cwd=repo)
    _git("checkout", "-q", "master", cwd=repo)
    _git("merge", "-q", "--no-ff", "feat/real", "-m", "merge", cwd=repo)

    landed = tmp_path / "landed"
    _git("worktree", "add", "-q", str(landed), "feat/real", cwd=repo)
    # A branch created and never committed to — the #1750 case.
    fresh = tmp_path / "fresh"
    _git("worktree", "add", "-q", "-b", "fix/fresh", str(fresh), "master",
         cwd=repo)
    return repo, landed, fresh


def _board(cwd, *args):
    env = dict(os.environ)
    env["SUPERTOOL_WORKTREE_PR"] = "0"
    # A fixture built milliseconds ago is inside the default 15m activity
    # window, so every tree in it reads `occupied` and no exit-code assertion
    # below could ever reach its subject. Both windows down to a second, and
    # `_settle` below spends it.
    env["SUPERTOOL_WORKTREE_ACTIVE_WINDOW"] = "1"
    env["SUPERTOOL_WORKTREE_IDLE_QUIET"] = "1"
    return subprocess.run([sys.executable, str(PRESET), *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=120, env=env,
                          encoding="utf-8", errors="replace")


def _settle():
    time.sleep(2.0)


BACKSLASH = chr(92)


def _row(out, needle):
    """The row line for a worktree path, matched across path separators.

    `git worktree list --porcelain` reports POSIX-style paths on Windows too,
    while `tmp_path` there is a native path separated by backslashes — so a raw
    `str(path) in line` finds nothing on one leg of the matrix, and every
    assertion below it then fails for a reason that has nothing to do with the
    columns under test.
    """
    want = str(needle).replace(BACKSLASH, "/")
    for line in out.splitlines():
        if line[:1] in (" ", ""):
            continue
        if want in line.replace(BACKSLASH, "/"):
            return line
    return ""


def _tags(row):
    """The `[a, b]` group a row carries, as a set."""
    if "[" not in row or "]" not in row:
        return set()
    inner = row[row.index("[") + 1:row.rindex("]")]
    return {t.strip() for t in inner.split(",")}


def _require_idle(res, path):
    """The occupancy probe has to have cooperated before its integer means anything.

    `lsof` can time out on a loaded runner, and the honest answer there is
    `cannot tell` — a real verdict about the tree, and not one this test is
    about. Skipping loudly beats asserting a code the fixture did not earn,
    and beats weakening the assertion to one a no-op implementation passes.
    """
    row = _row(res.stdout, path)
    if not row.startswith("idle"):
        import pytest
        pytest.skip("occupancy probe did not return `idle` on this runner "
                    f"(row: {row!r}) — the exit code under test is only "
                    "reachable from `idle`")


def test_end_to_end_a_fresh_branch_and_a_landed_one_get_different_cells(
        tmp_path) -> None:
    """The whole point, against real git rather than a stub.

    These two rows were the identical cell on the live board. If they render
    the same thing here the fix did nothing.
    """
    repo, landed, fresh = _fleet(tmp_path)
    _settle()
    out = _board(repo).stdout
    landed_row = _row(out, str(landed))
    fresh_row = _row(out, str(fresh))
    assert landed_row, out
    assert fresh_row, out

    # The control: a branch that did commit and landed keeps its cell.
    assert "merged" in _tags(landed_row), out
    assert "no commits yet" not in _tags(landed_row), out
    # The defect: the brand-new branch must no longer share it.
    assert "merged" not in _tags(fresh_row), out
    assert "no commits yet" in _tags(fresh_row), out


def test_end_to_end_a_detached_tree_with_work_is_not_a_green_light(
        tmp_path) -> None:
    """#1751 exactly: detached, so the merge column is structurally absent."""
    repo, landed, fresh = _fleet(tmp_path)
    det = tmp_path / "det"
    _git("worktree", "add", "-q", "--detach", str(det), "master", cwd=repo)
    (det / "unsaved.txt").write_text("work nobody has committed" + LF)
    _settle()

    res = _board(repo, str(det))
    row = _row(res.stdout, str(det))
    assert row, res.stdout
    # The merge column still cannot answer — that is structural, not a bug.
    assert "merged: n/a" in res.stdout, res.stdout
    assert "dirty: 1" in _tags(row), res.stdout
    _require_idle(res, str(det))
    assert res.returncode == wt.EXIT_DIRTY, (res.returncode, res.stdout)


def test_end_to_end_a_clean_detached_tree_still_answers_clean(tmp_path) -> None:
    """The must-fire control for the row above.

    A column that said `dirty` unconditionally would pass the test above and be
    worthless. This tree holds nothing, and the board has to say so out loud.
    """
    repo, landed, fresh = _fleet(tmp_path)
    # NOT named `clean-*`: the path is printed on the row, so a directory name
    # carrying the word makes `"clean" in stdout` pass with no column at all.
    det = tmp_path / "quiet"
    _git("worktree", "add", "-q", "--detach", str(det), "master", cwd=repo)
    _settle()
    res = _board(repo, str(det))
    row = _row(res.stdout, str(det))
    assert row, res.stdout
    assert "clean" in _tags(row), res.stdout
    assert "dirty" not in res.stdout, res.stdout
    _require_idle(res, str(det))
    assert res.returncode == wt.EXIT_IDLE, (res.returncode, res.stdout)


def test_the_changes_are_findable() -> None:
    assert_change_is_findable(1750, ROOT)
    assert_change_is_findable(1751, ROOT)
