"""#1229 — `[merged]` was decided by ancestry, and this repo squash-merges.

`for-each-ref --merged <base>` is an **ancestry** test. A squash merge writes a
new commit with no parent link back to the branch, so a fully-merged branch is
not an ancestor of `master` and never earned the tag.

Measured on the live fleet, 2026-08-10: **24** worktree branches, **8** carried
the ancestry tag (three of those eight being brand-new branches sitting at
`master`, i.e. trivially ancestors and holding nothing), and **16** had a merged
PR on GitHub. The label was wrong on 16 rows, always in the direction that keeps
a stale tree alive forever.

The shape of the defect is this repo's standing one: **an absent tag renders as
plain absence.** There was no `not merged` column, so a row simply carried
nothing — which reads as unmerged work in the op a maintainer uses to decide
which worktrees are safe to reap. Reading those rows the natural way argues for
re-opening PRs that are already merged.

So: three states per row, each naming the method it was decided by.

  merged          — ancestry, or a merged PR whose head is this branch
  not merged      — both were consulted and neither said yes
  merged unknown  — one of them could not answer, and which one is stated

**Ancestry is kept, as a positive only.** A branch that IS an ancestor of the
base is merged for certain, offline, with no network — so it answers first and
for free. It is unsound only as a *negative*, which is exactly the reading this
issue removes. Dropping it entirely would make the offline op (`nopr`,
`SUPERTOOL_WORKTREE_PR=0`) unable to say `merged` about anything at all.

**A second `gh` call is added, and it has to be.** The existing one is
`gh pr list --state open`; a merged PR is by construction absent from it, so the
merged head refs are not obtainable from the call already being made. The new
one is `--state merged` with a single JSON field, on the same timeout, and it
runs only when the tracker column is on.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
from _changelog_findable import assert_change_is_findable  # noqa: E402

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "worktrees.py"
_spec = importlib.util.spec_from_file_location("git_worktrees_1229", PRESET)
assert _spec is not None and _spec.loader is not None
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

_gc_spec = importlib.util.spec_from_file_location(
    "git_common_1229", ROOT / "presets" / "git" / "_git_common.py")
assert _gc_spec is not None and _gc_spec.loader is not None
gc = importlib.util.module_from_spec(_gc_spec)
_gc_spec.loader.exec_module(gc)


def _merged_index(*heads, truncated: bool = False, limit: int = 400):
    return gc.PrIndex({h: {"number": n} for n, h in enumerate(heads, start=1)},
                      truncated=truncated, limit=limit)


def _declined(reason: str):
    return gc.PrIndex(None, reason=reason)


def _verdict(branch, *, ancestors=frozenset(), ancestors_why="",
             base="master", merged_prs=None, ever_committed=None):
    return wt.merged_for(branch, ancestors, merged_prs,
                         ancestors_why=ancestors_why, base=base,
                         ever_committed=ever_committed)


# ── the three states ─────────────────────────────────────────────────────

def test_a_squash_merged_branch_is_merged_even_though_it_is_no_ancestor() -> None:
    """The defect itself. `fix/1216` merged as a PR; ancestry cannot see it."""
    got = _verdict("fix/1216", merged_prs=_merged_index("fix/1216"))
    assert got.state == wt.MERGED_YES, got
    assert "merged" in got.token
    assert "not merged" not in got.token
    assert "#1" in got.detail, got.detail


def test_ancestry_still_answers_yes_on_its_own_with_no_network() -> None:
    """Kept as a positive: an ancestor is merged for certain, offline.

    #1750 narrowed what ancestry alone proves and did not remove this. Being an
    ancestor is `base..branch` is empty, which a branch that never committed
    satisfies by holding nothing — so the reflog now decides between the two,
    and it is stubbed here to the case this test is about: a branch that DID
    commit. `test_git_worktrees_no_commits_and_dirty_1750_1751.py` owns the
    other two arms.
    """
    got = _verdict("chore/x", ancestors={"chore/x"}, merged_prs=None,
                   ever_committed=lambda branch: True)
    assert got.state == wt.MERGED_YES, got
    assert "ancestor" in got.detail, got.detail


def test_a_branch_neither_side_claims_is_explicitly_not_merged() -> None:
    """`not merged` is a rendered word, not the absence of one."""
    got = _verdict("fix/live", merged_prs=_merged_index("fix/other"))
    assert got.state == wt.MERGED_NO, got
    assert "not merged" in got.token, got.token


# ── the third state, which is the whole point ────────────────────────────

def test_a_failed_merged_pr_lookup_is_unknown_and_never_not_merged() -> None:
    """The regression that would otherwise ship silently.

    A lookup that did not run must not render as the world's answer. Rendered
    as `not merged`, it argues a merged tree still holds work.
    """
    got = _verdict("fix/1216",
                   merged_prs=_declined("gh exited 4: authentication required"))
    assert got.state == wt.MERGED_UNKNOWN, got
    assert "unknown" in got.token.lower(), got.token
    assert "not merged" not in got.token
    assert "not merged" not in got.detail
    assert "authentication required" in got.detail, got.detail


def test_a_capped_merged_pr_search_declines_for_every_branch() -> None:
    """A short answer must not reach a row as an answered map.

    `query_merged_prs_for_branches` converts its own cap into a declined
    lookup rather than handing back a map that is quietly incomplete, so the
    row renders `merge unknown` with the cap named — and does so for every
    branch, including ones the short page happened to contain. A partial map
    is indistinguishable from a complete one at the call site.
    """
    got = _verdict("fix/old", merged_prs=_declined(
        "the merged-PR search hit its 20-item cap, so its answer is incomplete"))
    assert got.state == wt.MERGED_UNKNOWN, got
    assert "cap" in got.detail, got.detail
    assert "not merged" not in got.detail


def test_the_offline_op_says_unknown_rather_than_not_merged() -> None:
    """`nopr` / `SUPERTOOL_WORKTREE_PR=0` makes no lookup at all.

    Offline, ancestry is the only signal and it is sound in one direction
    only, so every non-ancestor is `unknown` — never `not merged`.
    """
    got = _verdict("fix/1216", ancestors={"master"}, merged_prs=None)
    assert got.state == wt.MERGED_UNKNOWN, got
    assert "not merged" not in got.token
    assert "nopr" in got.detail or "not looked up" in got.detail, got.detail


def test_a_failed_ancestry_read_is_unknown_too() -> None:
    """Neither half answered — say so, do not fall through to the other."""
    got = _verdict("fix/x", ancestors=None,
                   ancestors_why="neither master nor main resolves here",
                   merged_prs=_merged_index("fix/other"))
    assert got.state == wt.MERGED_UNKNOWN, got
    assert "neither master nor main" in got.detail, got.detail


def test_a_detached_worktree_is_not_asked_the_question() -> None:
    got = _verdict("", merged_prs=_merged_index("fix/x"))
    assert got.state == wt.MERGED_NA, got
    assert "not merged" not in got.token


# ── what the board actually prints ───────────────────────────────────────

def _row(branch, merged=None):
    entry = {"branch": branch, "path": "/tmp/st-wt/" + (branch or "x"),
             "detached": not branch}
    verdict = wt.Assessment(wt.STATE_IDLE, ["nothing writing here"])
    return (entry, verdict, None, merged)


def test_the_row_carries_the_verdict_word_and_the_method() -> None:
    board = wt.render([_row("fix/1216", _verdict(
        "fix/1216", merged_prs=_merged_index("fix/1216")))])
    assert "[merged]" in board, board
    assert "merged: " in board, board


def test_an_unmerged_row_says_so_instead_of_carrying_nothing() -> None:
    board = wt.render([_row("fix/live", _verdict(
        "fix/live", merged_prs=_merged_index("fix/other")))])
    assert "[not merged]" in board, board


def test_the_result_line_counts_the_rows_whose_merge_state_is_unknown() -> None:
    """It has to ride the one line that survives `| tail -1`."""
    rows = [_row("fix/a", _verdict("fix/a", merged_prs=_declined("offline"))),
            _row("fix/b", _verdict("fix/b", merged_prs=_declined("offline")))]
    board = wt.render(rows)
    tail = board.strip().splitlines()[-1]
    assert "2 merge unknown" in tail, tail


def test_a_shorter_row_tuple_still_renders() -> None:
    """`render` is handed 2- and 3-tuples elsewhere; arity stays tolerant.

    A row with no merge verdict carries no merge tag — which is the *absence*
    the rest of this file is about, and is why the op itself now always hands
    one in. This pins the arity, not a rendering anyone should rely on.
    """
    entry = {"branch": "fix/x", "path": "/tmp/x"}
    verdict = wt.Assessment(wt.STATE_IDLE, ["quiet"])
    board = wt.render([(entry, verdict, None)])
    assert "fix/x" in board, board
    assert "merged" not in board, board


# ── the lookup itself ────────────────────────────────────────────────────

class _Runner:
    """A fake `gh`, recording the argv it was handed."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        out, rc = self.payloads.pop(0)
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")


def _json(*heads):
    return json.dumps([{"number": 100 + i, "headRefName": h}
                       for i, h in enumerate(heads)])


def test_the_lookup_asks_about_the_branches_it_holds_not_the_whole_history() -> None:
    """The cap has to be bounded by the question, not by the repository.

    Paging `--state merged` was the first implementation and it does not
    survive: this repo held 632 merged PRs on 2026-08-10, so any page size is
    a number the history passes and never comes back under, and every
    unmerged branch then renders `merge unknown` forever.
    """
    run = _Runner([(_json("fix/a"), 0)])
    idx = gc.query_merged_prs_for_branches(["fix/a", "fix/b"], runner=run)
    assert idx.answered
    assert set(idx.by_branch) == {"fix/a"}
    args = run.calls[0]
    assert args[args.index("--search") + 1] == "head:fix/a head:fix/b"
    assert "--state" in args and args[args.index("--state") + 1] == "merged"


def test_the_lookup_is_one_call_for_many_branches() -> None:
    run = _Runner([(_json(), 0)])
    gc.query_merged_prs_for_branches([f"fix/{n}" for n in range(25)], runner=run)
    assert len(run.calls) == 1, run.calls


def test_a_duplicate_branch_is_asked_about_once() -> None:
    run = _Runner([(_json(), 0)])
    gc.query_merged_prs_for_branches(["fix/a", "fix/a", ""], runner=run)
    assert run.calls[0][run.calls[0].index("--search") + 1] == "head:fix/a"


def test_no_branches_means_no_call_and_an_empty_answer() -> None:
    """An empty question has an empty answer, and it is answered, not declined."""
    run = _Runner([])
    idx = gc.query_merged_prs_for_branches(["", None], runner=run)
    assert idx.answered and idx.by_branch == {}
    assert run.calls == []


def test_a_failed_lookup_is_a_declined_index_not_an_empty_map() -> None:
    run = _Runner([("", 4)])
    idx = gc.query_merged_prs_for_branches(["fix/a"], runner=run)
    assert not idx.answered
    assert idx.by_branch is None


def test_a_full_result_set_declines_rather_than_answering_short() -> None:
    """20 asked for, 20 returned — indistinguishable from a cut page."""
    run = _Runner([(_json(*[f"fix/{n}" for n in range(20)]), 0)])
    idx = gc.query_merged_prs_for_branches(["fix/a"], runner=run)
    assert not idx.answered
    assert "cap" in idx.reason, idx.reason


def test_the_change_is_findable() -> None:
    assert_change_is_findable(1229, ROOT)
