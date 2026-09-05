#!/usr/bin/env python3
"""git-worktrees — who is working in this worktree? (#860)

`git worktree list` knows every worktree and its branch. Nothing knew whether
a worktree was *in use*, so the question got answered by hand:

    $ ps aux | grep -c "st-wt/804"
    0

and the zero was read as "the agent is dead, verified". It was alive. A
worktree path is not in that process's argv — the process is *chdir'd* there —
so the zero meant "my pattern matched nothing", which is a fact about the
pattern. A second agent went into the occupied tree and for two minutes both
wrote through one index.

**Three states, and the third is the point.** `occupied`, `idle`, and
`cannot tell`. Liveness is undecidable in the general case, so a checker that
resolves its uncertainty towards `idle` recreates the incident exactly: "no
evidence of an agent" is precisely what the `ps` grep already said. `idle` is
therefore not the default — it has to be *earned* by a probe that positively
looked and positively found nobody. Everything else is `cannot tell`, and
`cannot tell` is to be treated as occupied.

**Inference, not announcement.** The alternative design is a claim file the
occupant writes on entry. It was rejected for one decisive reason: nothing
that is already running would write it. The five agents live in sibling trees
right now announced nothing, and a claim-based checker would report every one
of them as unclaimed — the same false all-clear, wearing a new mechanism. A
claim also fails towards `occupied` forever when its writer dies, so it needs
an age *and* a liveness cross-check, which is this file again, underneath it.
Where an announcement *does* already exist it is read rather than reinvented:
`git worktree lock` is git's own, it survives across tools, and it is one of
the signals below.

**Inspection only.** Nothing here removes, prunes, unlocks or writes. The
report never prints a removal command: a destructive suggestion sitting under
an ambiguous verdict is how an occupied tree gets removed.

**The board is the tool's, the cells are not.** A `st-wt/NNN` worktree exists
to hold somebody else's branch, so its filenames, its path and its refnames are
remote text — and one of them is printed: the newest write is named in the
evidence. A filename may contain a newline, and unflattened it forged a whole
extra row carrying an `idle` verdict, which is the one verdict that authorises
deleting a tree (#876). Every cell is therefore flattened in `render()`, once,
at the layer the lines are made — not at each producer, which is the part that
keeps being added to. Nothing is censored; every character survives, on the one
line it was given.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

from _git_common import (  # noqa: E402
    _git, _git_verbatim, use_utf8_stdout, query_open_prs_by_branch,
    query_merged_prs_for_branches,
    foreign_worktree, foreign_worktree_note,  # #1536
)
from _env import env_int  # noqa: E402
import _checks  # noqa: E402  (the one check tally, shared with gh-pr / gh-prs)
import _untrusted  # noqa: E402  (filenames in a worktree are not our text — #876)

STATE_OCCUPIED = "occupied"
STATE_IDLE = "idle"
STATE_UNKNOWN = "cannot tell"

#: Exit 0 only for the answer that is safe to act on. `cannot tell` gets its
#: own code so a caller cannot collapse it into either neighbour.
#:
#: `EXIT_DIRTY` is #1751's, and it NARROWS exit 0 rather than widening it. The
#: reap this integer gates is `git worktree remove`, and occupancy alone never
#: answered the question that call destroys: a detached tree has no branch, so
#: its merge column is structurally `n/a`, and `idle` at exit 0 was then the
#: entire verdict over a tree holding uncommitted work. A caller testing `== 0`
#: declines, which is the safe direction; a caller testing `== 1` for
#: `occupied` is untouched, because occupancy keeps its own integer and still
#: outranks this one.
EXIT_IDLE = 0
EXIT_OCCUPIED = 1
EXIT_UNKNOWN = 2
EXIT_DIRTY = 3

#: A write newer than this reads as someone working. 15 minutes is chosen to
#: sit above a slow model turn and below a coffee break.
ACTIVE_WINDOW_DEFAULT = 900

#: And a tree must be quiet for *this* long before `idle` is allowed at all.
#:
#: The gap between the two windows is not padding, it is a live finding. On the
#: fleet this op was built against, two worktrees written to 7 and 12 minutes
#: earlier had **no process whose cwd was inside them** — an agent's parent
#: process does not have to be chdir'd into the tree it is editing. So "the
#: process table was read and holds nobody" is a much weaker licence for `idle`
#: than it looks, and on its own it would have called a working tree free at
#: the 16th minute. Between the two windows the answer is `cannot tell`.
IDLE_QUIET_DEFAULT = 3600

#: The cwd scan is the only probe that can license `idle`, so it gets a real
#: budget — and a stall is `unknown`, never "found nobody".
CWD_SCAN_TIMEOUT = 10

#: Walking a huge tree to find its newest mtime is not worth an unbounded
#: wait. A truncated walk cannot claim the tree is quiet (see `_newest_write`).
MAX_WALK_ENTRIES = 20000
WALK_BUDGET_SECONDS = 3.0

_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "vendor", "target", ".tox", ".gradle",
}

_LOCK_FILES = ("index.lock", "HEAD.lock", "ORIG_HEAD.lock", "config.lock",
               "packed-refs.lock", "shallow.lock")

_IN_PROGRESS = (
    ("rebase-merge", "an interactive/merge rebase is in progress"),
    ("rebase-apply", "a rebase (am/apply) is in progress"),
    ("MERGE_HEAD", "a merge is in progress"),
    ("CHERRY_PICK_HEAD", "a cherry-pick is in progress"),
    ("REVERT_HEAD", "a revert is in progress"),
    ("BISECT_LOG", "a bisect is in progress"),
    ("sequencer", "a sequencer operation (rebase/cherry-pick) is in progress"),
)


class CwdScan:
    """Did any process have its cwd inside the tree?

    `answer` is `yes` / `no` / `unknown` — the same three states, one layer
    down. `unknown` is the state the `ps` grep never had.
    """

    __slots__ = ("answer", "detail", "pids")

    def __init__(self, answer: str, detail: str, pids: list | None = None) -> None:
        self.answer = answer
        self.detail = detail
        self.pids = list(pids or [])

    def __repr__(self) -> str:
        return f"CwdScan({self.answer!r}, {self.detail!r}, pids={self.pids!r})"


#: Tracker states. Four, for the same reason occupancy has three.
#:
#: `TRACKER_NONE` and `TRACKER_UNKNOWN` are the pair that must never merge:
#: the first says GitHub was asked and holds no open PR for this branch, the
#: second says GitHub was not reached. Printed as one state they read as "this
#: work is unpublished" — an invitation to take the tree — when the truth may
#: be a live PR behind a dropped connection.
TRACKER_PR = "pr"
TRACKER_NONE = "none"
TRACKER_NO_REMOTE = "no-remote-ref"
TRACKER_UNKNOWN = "unknown"
TRACKER_NA = "n/a"


class Tracker:
    """A worktree's tracker state: a short token for the row, plus the reason."""

    __slots__ = ("state", "token", "detail")

    def __init__(self, state: str, token: str, detail: str) -> None:
        self.state = state
        self.token = token
        self.detail = detail

    def __repr__(self) -> str:
        return f"Tracker({self.state!r}, {self.token!r}, {self.detail!r})"


#: Merge states. Three, plus `n/a` for a tree with no branch to ask about.
#:
#: `MERGED_NO` and `MERGED_UNKNOWN` are the pair that must never merge, and
#: until #1229 there was no pair at all: a row that did not earn `[merged]`
#: simply carried nothing, and plain absence reads as unmerged work in the op
#: a maintainer uses to decide which tree is safe to reap. Measured on the live
#: fleet 2026-08-10 — 24 worktree branches, 8 tagged by ancestry, 16 with a
#: merged PR — the silent rows were wrong 16 times, every one in the direction
#: that keeps a stale tree alive.
MERGED_YES = "merged"
MERGED_NO = "not-merged"
MERGED_UNKNOWN = "unknown"
MERGED_NA = "n/a"

#: And the state #1750 found missing: the branch never committed anything, so
#: there is nothing to measure and `merged` is a claim about no work at all.
#:
#: Observed on the live board at `62bc3f0`: SEVEN of eight fleet worktrees
#: rendered `[merged]` while an agent was editing each of them seconds earlier.
#: Every one had zero commits — the developer agents commit once, at the end —
#: and "every commit is already an ancestor of master" is vacuously true of
#: nothing. That made a brand-new branch and a fully-landed one the same cell,
#: in the column a maintainer reads to decide which tree is safe to delete.
MERGED_NO_COMMITS = "no-own-commits"


class Merged:
    """Whether this branch's work is in the base — and by which method.

    The method is part of the answer, not decoration. Ancestry and the merged
    PR page disagree by construction on every squash merge, so a row that says
    `merged` without saying how cannot be checked by the reader who doubts it.
    """

    __slots__ = ("state", "token", "detail")

    def __init__(self, state: str, token: str, detail: str) -> None:
        self.state = state
        self.token = token
        self.detail = detail

    def __repr__(self) -> str:
        return f"Merged({self.state!r}, {self.token!r}, {self.detail!r})"


def branch_ever_committed(branch: str, runner=None):
    """Has this branch ever moved since it was created? `True` / `False` / `None`.

    The signal #1750 needs, and the one the issue's own proposal cannot be.
    #1750 offers `git rev-list <base>..<branch>` being empty as "the direct
    test". It is not a second opinion at all — it is the *same predicate* the
    ancestry read already performs, because a branch is an ancestor of the base
    **iff** `base..branch` is empty. Measured on git 2.46.2 against a repo
    holding `feat/real` (three commits, merged `--no-ff`) and `fix/new`
    (created from master, never committed):

        for-each-ref --merged master  ->  feat/real, fix/new, master
        rev-list --count master..feat/real  ->  0
        rev-list --count master..fix/new    ->  0

    The commit graph cannot separate *landed* from *never started*: both leave
    the branch tip reachable from the base. The reflog can, it is local, and it
    costs nothing on the network, so `nopr` keeps working:

        feat/real@{0} commit: real work
        feat/real@{1} branch: Created from HEAD
        fix/new@{0}   branch: Created from master

    **`None` is not `False`.** A reflog can be absent for reasons that say
    nothing about the branch — `core.logAllRefUpdates` off, a bare repo, a
    fresh clone that did not create the ref locally, or expiry that trimmed the
    oldest entries. Answering `False` there renders `no commits yet` over a
    branch that may hold a year of work, which is this file's own defect class
    pointed at the fix for it.

    Read as a **positive only**, in the same asymmetry `merged_for` already
    uses for ancestry: only a log that was read AND holds nothing but creation
    entries downgrades the row. Anything else leaves the previous behaviour
    exactly where it was.
    """
    if not branch:
        return None
    # `_git_verbatim`, not `_git`: `%gs` is a reflog SUBJECT, which for a commit
    # entry is somebody's commit message. `_git` runs `Popen(text=True)`
    # and text mode rewrites a lone CR and a CRLF to LF before any preset sees
    # the stream, so a crafted message could split into extra records that read
    # as git's own. With the translation off a reflog line cannot contain LF by
    # definition, which makes the split below exact.
    run = _git_verbatim if runner is None else runner
    # The fully-qualified ref, never the short name: it disambiguates a tag
    # sharing the branch's name, and it means the argument cannot begin with a
    # `-` however the ref was spelled.
    res = run(["reflog", "show", "--format=%gs", "refs/heads/" + branch])
    if res.returncode != 0:
        return None
    entries = [line.strip() for line in res.stdout.split(chr(10)) if line.strip()]
    if not entries:
        return None
    # `branch: Created from X` is what `git branch` and `git worktree add -b`
    # write and nothing else does. A `commit:`, `rebase`, `merge`, `reset` or
    # `pull` entry all mean the ref moved, and every one of them is a reason to
    # leave the `merged` verdict alone rather than to claim the branch is new.
    # `bool(...)` is not a cast for the scanner's benefit: it is where the
    # child's text provably stops being text (#1475). Nothing about a reflog
    # subject survives into the answer — only whether some entry was not a
    # creation entry — so the taint ends here rather than at a render.
    return bool(any(not e.startswith("branch: Created") for e in entries))


def merged_for(branch: str, ancestors, merged_prs, ancestors_why: str = "",
               base: str = "master", ever_committed=None) -> Merged:
    """Which of the five answers holds for this worktree's branch (#1229, #1750).

    Two signals, deliberately asymmetric:

    * **Ancestry** (`for-each-ref --merged`) is consulted first and is used as
      a **positive only**. A branch that is an ancestor of the base has every
      one of its commits on the base — certain, local, free, and true even
      with `nopr` and no network. As a *negative* it is worthless here: a
      squash merge writes a commit with no parent link to the branch, so a
      fully-merged branch is not an ancestor, which is the whole defect.
    * **The merged-PR lookup** answers the squash case, and only it can. It is
      a network read, so its failure is a third state and never a `no`. Its
      own cap arrives here as a failure with the cap named, rather than as an
      answered map that is quietly short — see
      `query_merged_prs_for_branches`.

    Folding the two rather than dropping ancestry is the call this makes, and
    the reason is the offline op: with `nopr` the merged page is never fetched,
    and an ancestry-free implementation could then say `merged` about nothing
    at all. The two cannot disagree in the direction that matters, because
    ancestry is only ever read as `yes`.

    `ancestors is None` means the local read itself failed, and that is
    `unknown` even when the PR page answered — a branch absent from the merged
    page is only `not merged` if ancestry also had its say.
    """
    if not branch:
        return Merged(MERGED_NA, "merge n/a",
                      "merged: n/a — no branch checked out here (detached or "
                      "bare), so there is nothing to measure against " + base)

    if ancestors is not None and branch in ancestors:
        # #1750: ancestry is `branch tip is reachable from base`, which a branch
        # that never committed satisfies by holding nothing. The reflog is asked
        # HERE and only here — a non-ancestor is answered by the PR page below
        # and a probe on it would be a git call per row buying no answer.
        probe = branch_ever_committed if ever_committed is None else ever_committed
        moved = probe(branch)
        if moved is False:
            return Merged(MERGED_NO_COMMITS, "no commits yet",
                          f"no commits yet — this branch has never moved since "
                          f"it was created (its reflog holds only the creation "
                          f"entry), so it is an ancestor of {base} by holding "
                          f"NOTHING, not by landing. Anything an agent has done "
                          f"here is uncommitted and exists nowhere else")
        if moved is None:
            return Merged(MERGED_UNKNOWN, "merge unknown",
                          f"merged: UNKNOWN — an ancestor of {base}, but its "
                          f"reflog did not answer, so whether this branch ever "
                          f"held commits of its own is unestablished. A branch "
                          f"created and never committed to is an ancestor too, "
                          f"and the two are indistinguishable in the commit "
                          f"graph (#1750)")
        return Merged(MERGED_YES, "merged",
                      f"merged: yes — every commit is already an ancestor of "
                      f"{base}, and its reflog shows the branch did commit "
                      f"(local, no network)")

    if merged_prs is not None and merged_prs.answered:
        pr = merged_prs.get(branch)
        if pr is not None:
            return Merged(MERGED_YES, "merged",
                          f"merged: yes — PR #{pr.get('number', '?')} is merged. "
                          f"Not an ancestor of {base}: a squash merge leaves no "
                          "ancestry, which is why the local check cannot see it")

    if ancestors is None:
        return Merged(MERGED_UNKNOWN, "merge unknown",
                      "merged: UNKNOWN — the local ancestry check did not answer "
                      f"({ancestors_why or 'not run'}). This is the tool failing "
                      "to look, NOT a finding that the work is unmerged")

    if merged_prs is None:
        return Merged(MERGED_UNKNOWN, "merge unknown",
                      f"merged: UNKNOWN — not an ancestor of {base}, and the "
                      "merged-PR page was not looked up (nopr / "
                      "SUPERTOOL_WORKTREE_PR=0). A squash merge is invisible to "
                      "ancestry, so this is not a finding that the work is unmerged")

    if not merged_prs.answered:
        return Merged(MERGED_UNKNOWN, "merge unknown",
                      f"merged: UNKNOWN — not an ancestor of {base}, and the "
                      f"merged-PR lookup did not answer ({merged_prs.reason}). "
                      "A squash merge is invisible to ancestry, so nothing here "
                      "establishes that the work is unmerged")

    return Merged(MERGED_NO, "not merged",
                  f"merged: no — not an ancestor of {base}, and no merged PR "
                  "has this branch as its head")


#: Dirty states. Three, and the third is why the column is worth its git call.
#:
#: `DIRTY_CLEAN` and `DIRTY_UNKNOWN` are the pair that must never merge. A
#: `git status` that timed out, hit a missing directory or failed outright
#: returns no records, and no records is exactly what a clean tree returns —
#: so the two collapse into the reassuring one unless they are kept apart by
#: construction. That collapse is the whole subject of #1751 one layer up:
#: `idle` was reachable by nothing having answered.
DIRTY_CLEAN = "clean"
DIRTY_DIRTY = "dirty"
DIRTY_UNKNOWN = "unknown"

#: The dirty scan's own budget. A stall is `unknown`, never "found nothing".
DIRTY_SCAN_TIMEOUT = 10


class Dirty:
    """Does this worktree hold work that exists nowhere else? (#1751)

    The field the board was missing entirely. For a tree with a branch the
    merge column covered most of it — `merged: yes` means the commits are on
    the base and only uncommitted changes remain at risk. For a **detached**
    tree the merge column is structurally `n/a`, so `idle` was the entire
    verdict standing between `git worktree remove` and destroyed work, and
    `idle` is a statement about the process table at one instant.

    `count` is the number of change RECORDS, not files: an untracked directory
    collapses to one record (`?? udir/`), which is git's own summary and not a
    loss this layer introduces.
    """

    __slots__ = ("state", "token", "detail", "count")

    def __init__(self, state: str, token: str, detail: str, count: int = 0) -> None:
        self.state = state
        self.token = token
        self.detail = detail
        self.count = count

    def __repr__(self) -> str:
        return f"Dirty({self.state!r}, {self.token!r}, {self.count!r})"


def _count_porcelain_z(out: str) -> int:
    """Change records in `status --porcelain -z` output.

    `-z` rather than the newline form for one reason: a filename may contain a
    newline, and this count is PRINTED. NUL-separated records make it exact
    instead of inflatable by whoever named the file — and `_git` runs
    `text=True`, which rewrites a lone CR and a CRLF to LF before any preset
    sees the stream, so the newline form could not have been trusted anyway.

    A rename or copy carries its origin path as its OWN NUL field (measured,
    git 2.46.2: `R  new.py<NUL>old.py<NUL>`), so counting fields reports two
    changes for one renamed file.
    """
    fields = out.split(chr(0))
    if fields and fields[-1] == "":
        fields.pop()
    count = 0
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        count += 1
        # `XY PATH`, and X or Y being R/C means an origin path follows.
        if record[:1] in ("R", "C") or record[1:2] in ("R", "C"):
            index += 1
    return count


def dirty_for(path: str, runner=None) -> Dirty:
    """The uncommitted-work column, in three states (#1751).

    `--no-optional-locks` is not a nicety. `git status` refreshes the index and
    writes it back, and this op's contract is INSPECTION ONLY — it must not
    write inside a tree a live agent is holding, must not contend for
    `index.lock`, and must not perturb the newest-write mtime that the
    occupancy column one cell to the left is reading.

    **`-c status.showUntrackedFiles=normal` is what makes this a GATE rather
    than a render** (#1290, #1295). That setting is an ordinary user or repo
    preference, and with it inherited a tree whose only uncommitted work is
    UNTRACKED reports no records at all — indistinguishable here from clean, in
    the column an exit code branches on. A display preference is not allowed to
    turn a destructive decision green. `-c` outranks both config files and the
    environment, which is why the pin goes on the argv.
    """
    def _default(args):
        return _git(args, timeout=DIRTY_SCAN_TIMEOUT)

    run = _default if runner is None else runner
    res = run(["--no-optional-locks", "-C", path,
               "-c", "status.showUntrackedFiles=normal",
               "status", "--porcelain", "-z"])
    if res.returncode != 0:
        # git's stderr is a child stream and this sentence is rendered, so it is
        # flattened at the producer rather than relying on `render()` doing it
        # one frame up (#1475): nothing here may reach column 0 or add a line.
        why = _untrusted.flat(
            (res.stderr or "").strip().replace(chr(10), " ")[:200]) or (
            f"git exited {res.returncode} and said nothing")
        return Dirty(DIRTY_UNKNOWN, "dirty unknown",
                     f"uncommitted work: UNKNOWN — the status read did not "
                     f"answer ({why}). This is the tool failing to look, NOT a "
                     f"finding that the tree is clean")
    # `int(...)` for the same reason as the `bool(...)` above: the records are
    # counted and then discarded, so no byte a filename chose reaches a render
    # from here — only how many there were.
    count = int(_count_porcelain_z(res.stdout))
    if count:
        return Dirty(DIRTY_DIRTY, f"dirty: {count}",
                     f"uncommitted work: {count} change record"
                     f"{'' if count == 1 else 's'} — this tree holds work that "
                     f"exists nowhere else, and removing it destroys that work",
                     count)
    return Dirty(DIRTY_CLEAN, "clean",
                 "uncommitted work: none — `git status --porcelain` was read in "
                 "this tree and returned no records", 0)


def exit_code_for(state: str, dirty_state: str) -> int:
    """The one integer, and what each column may contribute to it (#1751).

    Occupancy keeps precedence and keeps its own codes: somebody being in the
    tree is the more urgent fact and the one a caller already branches on.
    Under `idle`, the dirty column decides — because `idle` alone was never a
    statement about whether the tree holds work, and for a detached tree
    nothing else in the render was either.

    A dirty scan that could not answer lands on `cannot tell`, not on `idle`.
    The footer's standing advice for that code — treat the tree as occupied
    until something answers — is exactly right here.
    """
    if state == STATE_OCCUPIED:
        return EXIT_OCCUPIED
    if state != STATE_IDLE:
        return EXIT_UNKNOWN
    if dirty_state == DIRTY_DIRTY:
        return EXIT_DIRTY
    if dirty_state == DIRTY_CLEAN:
        return EXIT_IDLE
    return EXIT_UNKNOWN


class Assessment:
    """A verdict that always carries the evidence it was built from.

    A bare verdict is the thing that misled the reporter; every state here —
    including `cannot tell` — names what was looked at and what answered.
    """

    __slots__ = ("state", "evidence")

    def __init__(self, state: str, evidence: list) -> None:
        self.state = state
        self.evidence = list(evidence)

    def __repr__(self) -> str:
        return f"Assessment({self.state!r}, {self.evidence!r})"


# ── formatting ───────────────────────────────────────────────────────────

def _age(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


# ── worktree inventory ───────────────────────────────────────────────────

def parse_worktree_list(text: str) -> list:
    """Parse `git worktree list --porcelain` into entries.

    `locked` is `None` when absent and `""` when git reported a lock with no
    reason — those are different facts, and folding the second into the first
    is how an announced occupancy disappears.
    """
    entries: list = []
    current: dict = {}
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                entries.append(current)
            current = {
                "path": value, "head": None, "branch": None, "detached": False,
                "bare": False, "locked": None, "prunable": None, "gitdir": None,
            }
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.replace("refs/heads/", "", 1)
        elif key == "detached":
            current["detached"] = True
        elif key == "bare":
            current["bare"] = True
        elif key == "locked":
            current["locked"] = value
        elif key == "prunable":
            current["prunable"] = value
    if current:
        entries.append(current)
    return entries


def resolve_gitdir(path: str) -> str | None:
    """The per-worktree git dir — where `index.lock` and `HEAD` actually live."""
    dot = os.path.join(path, ".git")
    if os.path.isdir(dot):
        return dot
    try:
        with open(dot, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("gitdir:"):
                    target = line.split(":", 1)[1].strip()
                    if not os.path.isabs(target):
                        target = os.path.join(path, target)
                    return os.path.normpath(target)
    except OSError:
        return None
    return None


# ── probes ───────────────────────────────────────────────────────────────

def _lock_signals(gitdir: str | None) -> list:
    """Lock files git holds only while a command is mid-flight.

    Strong but transient: absent between two operations of a busy agent, which
    is why an empty list here is never taken as evidence of absence.
    """
    if not gitdir:
        return []
    found = []
    now = time.time()
    for name in _LOCK_FILES:
        target = os.path.join(gitdir, name)
        try:
            age = now - os.stat(target).st_mtime
        except OSError:
            continue
        found.append(f"{name} present in the git dir ({_age(age)} old) — a git command is running here")
    return found


def _inprogress_signals(gitdir: str | None) -> list:
    """A tree stopped mid-rebase belongs to whoever stopped it."""
    if not gitdir:
        return []
    found = []
    for name, what in _IN_PROGRESS:
        if os.path.exists(os.path.join(gitdir, name)):
            found.append(f"{what} ({name} present) — the tree is mid-operation")
    return found


#: Read from the tail rather than the whole file — a reflog can grow large
#: between `git gc`s, and only the last entry is ever needed. If the newest
#: entry's own line turns out to be longer than this (a very long commit
#: subject), the read window grows rather than parsing a mid-line fragment —
#: see `_reflog_newest_entry_time`.
_REFLOG_TAIL_BYTES = 8192

#: Growing past this means either a pathological single reflog line or a
#: file that is not a reflog at all — decline rather than keep re-reading.
_REFLOG_TAIL_MAX_BYTES = 1 << 20  # 1 MiB


def _reflog_newest_entry_time(target: str):
    """The newest entry's own timestamp, as `(unix_time, None)` — or
    `(None, why)` when the entry could not be read.

    `git gc` / `git reflog expire` (auto-triggered by an ordinary `git fetch
    --prune`) rewrites this file for every linked worktree **without
    appending anything**: the mtime moves, the content does not (#1923). The
    file's own mtime therefore cannot tell "somebody just committed here"
    apart from "a background gc just ran", and reading the last entry's own
    timestamp is what does. A reflog line is `<old> <new> <who> <email>
    <timestamp> <tz><TAB><message>`; the timestamp is the second-to-last
    field before the tab.

    Only the tail is read, growing the window rather than trusting a first
    read: seeking back a fixed `_REFLOG_TAIL_BYTES` can land **inside** the
    newest entry's own message, past the tab that marks where its header
    ends — reading that fragment as a header instead of growing the window
    once mis-parsed a line's own message digits as a plausible-looking but
    wrong timestamp, silently, with no failure at all. A short read is
    detected by the absence of a tab in the last line and grown until one is
    found or `_REFLOG_TAIL_MAX_BYTES` is reached, at which point this
    declines rather than guess.

    `_untrusted.split_lines`, not `str.splitlines()` (#1130's register): the
    newest entry's own commit message is written by whoever committed —
    anyone who can write a commit message in a repo this tool inspects, not
    this tool's own operator — and `str.splitlines()` treats U+2028 / U+2029
    as line terminators, which git's reflog format does not. A message
    carrying one could make `lines[-1]` a fragment of the message rather
    than the true last physical line, the same "read a fragment instead of
    the real header" failure the tail-seek growth above exists to close.

    `why` is a reason a caller can fold into the mtime-fallback evidence line
    below — it is never itself read as "the tree is quiet".
    """
    try:
        size = os.path.getsize(target)
    except OSError as exc:
        return None, f"could not be read ({exc})"
    tail = min(size, _REFLOG_TAIL_BYTES)
    while True:
        try:
            with open(target, "rb") as handle:
                if tail < size:
                    handle.seek(-tail, os.SEEK_END)
                raw = handle.read()
        except OSError as exc:
            return None, f"could not be read ({exc})"
        lines = [ln for ln in _untrusted.split_lines(raw.decode("utf-8", errors="replace")) if ln.strip()]
        if not lines:
            return None, "the reflog has no entries"
        last = lines[-1]
        if chr(9) in last or tail >= size:
            break
        if tail >= _REFLOG_TAIL_MAX_BYTES:
            return None, (f"the newest entry's line exceeds {_REFLOG_TAIL_MAX_BYTES} bytes "
                          "with no field separator found — declining rather than parsing a fragment")
        tail = min(size, tail * 8)
    header = last.split(chr(9), 1)[0]
    tokens = header.split()
    if len(tokens) < 2:
        return None, f"last entry does not look like a reflog line: {header!r}"
    try:
        return float(tokens[-2]), None
    except ValueError:
        return None, f"last entry's timestamp field is not numeric: {tokens[-2]!r}"


def _newest_write(path: str, gitdir: str | None, now: float,
                  known_good_since: float | None = None):
    """Newest mtime in the tree and its git dir, as `(age_seconds, label)`.

    The `"reflog written"` candidate is the one exception: it reports the
    newest **entry**'s own timestamp via `_reflog_newest_entry_time`, not
    the file's mtime, and only falls back to mtime when that entry cannot
    be read. See that function's docstring and #1923.

    Returns `(None, why)` when the answer was not obtained — an unreadable
    tree, or a walk that hit its cap. A truncated walk could have missed a
    newer file, so it must not be allowed to underwrite a claim of quiet.

    `known_good_since` (#2272) is a caller's own declaration: "I wrote this
    tree myself, at or before this point in time — do not read that write as
    evidence of a live agent." Any candidate at or before that timestamp is
    skipped as if it were never found, exactly as #1923's mtime fallback
    keeps failures pointed at `occupied` rather than at `idle`: a write
    strictly AFTER the declared cutoff is untouched by this and still counts
    as evidence, which is what stops the declaration from swallowing a real
    agent's write that landed a moment later. When every candidate is
    excluded this way, the answer is not `None` (which downstream reads as
    "recency not established", i.e. `cannot tell`) — it is the elapsed time
    since the declared cutoff itself, because that is the caller's own claim
    about how long the tree has been quiet.
    """
    newest = None
    where = ""
    candidates = []
    if gitdir:
        candidates = [
            (os.path.join(gitdir, "index"), "index written"),
            (os.path.join(gitdir, "HEAD"), "HEAD moved"),
            (os.path.join(gitdir, "logs", "HEAD"), "reflog written"),
            (os.path.join(gitdir, "ORIG_HEAD"), "ORIG_HEAD written"),
        ]
    for target, label in candidates:
        if label == "reflog written":
            if not os.path.exists(target):
                continue
            entry_time, why = _reflog_newest_entry_time(target)
            if entry_time is not None:
                if known_good_since is not None and entry_time <= known_good_since:
                    continue
                if newest is None or entry_time > newest:
                    newest, where = entry_time, "reflog entry written"
                continue
            # The entry itself could not be read — an empty file, a line in
            # a format this does not recognise. Falling back to the file's
            # own mtime (rather than dropping the signal) is the deliberate
            # choice here: it keeps this failure mode inside the direction
            # #1923 already showed to be safe — a spurious `occupied`, never
            # a spurious `idle`, which is the verdict that authorises
            # deleting a worktree. See the function docstring above.
            try:
                mtime = os.stat(target).st_mtime
            except OSError:
                continue
            if known_good_since is not None and mtime <= known_good_since:
                continue
            if newest is None or mtime > newest:
                newest, where = mtime, f"reflog file touched, entry unreadable ({why})"
            continue
        try:
            mtime = os.stat(target).st_mtime
        except OSError:
            continue
        if known_good_since is not None and mtime <= known_good_since:
            continue
        if newest is None or mtime > newest:
            newest, where = mtime, label

    seen = 0
    deadline = time.monotonic() + WALK_BUDGET_SECONDS
    truncated = False
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for name in files:
                seen += 1
                if seen > MAX_WALK_ENTRIES or time.monotonic() > deadline:
                    truncated = True
                    break
                try:
                    mtime = os.stat(os.path.join(root, name), follow_symlinks=False).st_mtime
                except OSError:
                    continue
                if known_good_since is not None and mtime <= known_good_since:
                    continue
                if newest is None or mtime > newest:
                    newest = mtime
                    where = f"newest write {os.path.relpath(os.path.join(root, name), path)}"
            if truncated:
                break
    except OSError as exc:
        return None, f"could not walk the worktree ({exc})"

    if newest is None:
        if known_good_since is not None:
            newest = known_good_since
            where = "no write since the declared known-good point"
        else:
            return None, "nothing in the worktree or its git dir could be stat'd"
    age = now - newest
    if truncated and age > ACTIVE_WINDOW_DEFAULT:
        return None, (f"stopped walking after {seen} entries — a newer write may exist "
                      f"below the cap, so quiet cannot be claimed")
    if where == "no write since the declared known-good point":
        return age, f"{where} ({_age(age)} ago)"
    return age, f"{where} {_age(age)} ago"


def _have_proc() -> bool:
    return os.path.isdir("/proc") and os.path.isdir("/proc/self")


def _read_cwd_table(memo: dict | None = None):
    """Every readable process's cwd, as `(rows, detail)`.

    `rows` is `None` when the scan did not answer — no route on this platform,
    a stalled `lsof`, a tool that printed nothing. That `None` is the whole
    difference between this op and the `ps` grep it replaces.
    """
    if memo is not None and "table" in memo:
        return memo["table"]
    result = _read_cwd_table_uncached()
    if memo is not None:
        memo["table"] = result
    return result


def _read_cwd_table_uncached():
    if _have_proc():
        rows = []
        scanned = unreadable = 0
        try:
            names = os.listdir("/proc")
        except OSError as exc:
            return None, f"/proc could not be listed ({exc}) — cannot scan process cwds"
        for name in names:
            if not name.isdigit():
                continue
            scanned += 1
            try:
                cwd = os.readlink(f"/proc/{name}/cwd")
            except OSError:
                unreadable += 1
                continue
            rows.append((name, _proc_comm(name), cwd))
        return rows, (f"{scanned} processes scanned via /proc"
                      + (f", {unreadable} unreadable (other users)" if unreadable else ""))

    lsof = shutil.which("lsof")
    if not lsof:
        return None, (f"no way to read process cwds on this platform ({sys.platform}): "
                      "/proc is absent and lsof is not installed — occupancy undecidable")
    cmd = [lsof, "-a", "-d", "cwd", "-w", "-n", "-F", "pn"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=CWD_SCAN_TIMEOUT,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None, (f"lsof did not answer within {CWD_SCAN_TIMEOUT}s — the process table "
                      "was not read, so nothing is claimed about it")
    except OSError as exc:
        return None, f"lsof could not be run ({exc}) — cannot scan process cwds"
    if not proc.stdout.strip():
        return None, (f"lsof printed nothing (exit {proc.returncode}) — the process table "
                      "was not read, so nothing is claimed about it")
    rows = []
    pid = ""
    for line in proc.stdout.splitlines():
        if not line:
            continue
        if line[0] == "p":
            pid = line[1:].strip()
        elif line[0] == "n" and pid:
            rows.append((pid, "", line[1:].strip()))
    return rows, f"{len({r[0] for r in rows})} processes scanned via lsof"


def _proc_comm(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _inside(candidate: str, target: str) -> bool:
    candidate = os.path.realpath(candidate)
    target = os.path.realpath(target)
    return candidate == target or candidate.startswith(target + os.sep)


def _cwd_scan(path: str, memo: dict | None = None) -> CwdScan:
    """Is any process chdir'd into this tree? The question `ps aux | grep` asked wrong."""
    rows, detail = _read_cwd_table(memo)
    if rows is None:
        return CwdScan("unknown", detail)
    hits = []
    for pid, comm, cwd in rows:
        try:
            if _inside(cwd, path):
                hits.append((pid, comm))
        except OSError:
            continue
    if hits:
        pids = [pid for pid, _ in hits]
        who = ", ".join(f"pid {pid}" + (f" ({comm})" if comm else "") for pid, comm in hits[:5])
        more = f" +{len(hits) - 5} more" if len(hits) > 5 else ""
        return CwdScan("yes", f"{who}{more} has cwd inside this worktree", pids=pids)
    return CwdScan("no", f"no process has its cwd inside this worktree — {detail}")


# ── the verdict ──────────────────────────────────────────────────────────

def assess(entry: dict, *, now: float | None = None, window: int | None = None,
           scan: CwdScan | None = None,
           known_good_since: float | None = None) -> Assessment:
    """Three states, and `idle` is the one that has to be earned.

    Any positive signal is `occupied` — a lock, an in-progress operation, a
    `git worktree lock`, a recent write, a process chdir'd inside. Absence of
    all of them is `idle` **only** when every probe that could have spoken did
    speak: the tree was stat'd and is quiet, and the process table was read and
    holds nobody. Otherwise `cannot tell`, naming which probe went silent.

    `known_good_since` (#2272) is the caller's own declaration — "I wrote this
    tree myself, at or before this instant" — threaded straight to
    `_newest_write`, which is the only probe an index write from the caller's
    own `git merge`/`git push` can otherwise poison. It is disclosed in the
    evidence unconditionally, whether or not it changed the verdict, because a
    declaration applied silently is indistinguishable from one that was never
    read.
    """
    now = time.time() if now is None else now
    window = env_int("SUPERTOOL_WORKTREE_ACTIVE_WINDOW", ACTIVE_WINDOW_DEFAULT,
                     minimum=1) if window is None else window
    quiet_for = max(window, env_int("SUPERTOOL_WORKTREE_IDLE_QUIET",
                                    IDLE_QUIET_DEFAULT, minimum=1))
    evidence: list = []

    # Disclosure of the declaration, kept OUT of `evidence` deliberately: that
    # list's truthiness is what routes to `occupied` a few lines down, and a
    # disclosure line is not itself a positive occupancy signal. It is folded
    # into whichever Assessment this function returns, via `_finish` below,
    # so a declaration applied silently is never possible — every exit path
    # names it, whether or not it changed the verdict.
    disclosure: list = []
    if known_good_since is not None:
        disclosure.append(
            "known-good declaration: writes at or before "
            f"{_age(now - known_good_since)} ago are attributed to the caller, "
            "not read as evidence of a live agent"
        )

    def _finish(state: str, reasons: list) -> Assessment:
        return Assessment(state, disclosure + reasons)

    locked = entry.get("locked")
    if locked is not None:
        reason = locked or "no reason given"
        evidence.append(f"git worktree lock held: {reason} — the occupant announced itself")

    evidence.extend(_lock_signals(entry.get("gitdir")))
    evidence.extend(_inprogress_signals(entry.get("gitdir")))

    if known_good_since is not None:
        age, age_label = _newest_write(entry.get("path", ""), entry.get("gitdir"), now,
                                       known_good_since)
    else:
        age, age_label = _newest_write(entry.get("path", ""), entry.get("gitdir"), now)
    if age is not None and age <= window:
        evidence.append(f"{age_label} (inside the {_age(window)} activity window)")

    if evidence:
        if scan is None:
            evidence.append("process-cwd scan not run — occupied on the evidence above")
        else:
            evidence.append(f"process-cwd scan: {scan.detail}")
        return _finish(STATE_OCCUPIED, evidence)

    if scan is None:
        scan = _cwd_scan(entry.get("path", ""))

    if scan.answer == "yes":
        return _finish(STATE_OCCUPIED, [scan.detail] + ([age_label] if age is not None else []))

    quiet = [age_label] if age is not None else []
    quiet.append("no index.lock or HEAD.lock, no rebase/merge/cherry-pick in progress, no git worktree lock")

    if scan.answer == "no" and age is not None and age >= quiet_for:
        return _finish(STATE_IDLE, [scan.detail] + quiet)

    reasons = []
    if scan.answer != "no":
        reasons.append(f"process-cwd scan did not answer: {scan.detail}")
    if age is None:
        reasons.append(f"recency not established: {age_label}")
    elif age < quiet_for:
        reasons.append(
            f"{age_label} — quiet for less than {_age(quiet_for)}, and an empty cwd scan "
            "does not prove absence: agents have been observed editing a tree no "
            "process was chdir'd into"
        )
    reasons.append("no positive signal — but absence of a signal is not proof of absence, "
                   "so this declines rather than reporting the tree free")
    return _finish(STATE_UNKNOWN, reasons + quiet[-1:])


# ── the tracker column ───────────────────────────────────────────────────

class RemoteRefs(dict):
    """The name-keyed mapping every caller reads, plus every ref it was built
    from (`all_refs`).

    A mapping keyed by branch name cannot answer *is `refs/remotes/fork/X`
    here* — one name, one value — and that is the question #1525 turns on:
    without it, "the branch tracks a remote whose ref is not in this clone"
    is indistinguishable from "the branch is in sync", because both end at the
    same origin-preferred ref. Membership by branch name is unchanged, so a
    caller handed one of these cannot tell the difference.
    """

    __slots__ = ("all_refs",)

    def __init__(self, mapping=None, all_refs=()) -> None:
        super().__init__(mapping or {})
        self.all_refs = frozenset(all_refs)


def remote_branch_names():
    """Branch names that exist on some remote, as `(names, why)`.

    `(None, why)` when git did not answer — and that `None` is not allowed to
    turn into "never pushed", which is the local half of the same mistake this
    column is about.

    `names` is a **mapping** of stripped branch name → the full remote ref it
    was seen at, not a bare set (#1496). Membership is what every caller reads
    and is unchanged; the value is what `unpushed_for` needs, because a stripped
    name is not a rev and `@{upstream}` cannot stand in for it — `git worktree
    add -b X … master` writes an upstream of `origin/master` for a branch that
    has never left the machine, so measuring against it would compare X to the
    wrong ref and answer confidently.

    Even when it answers, this is *local* knowledge: remote-tracking refs are
    written by fetch and push, so a branch pushed from another clone since the
    last fetch here looks unpushed. The wording in `tracker_for` says so rather
    than pretending the ref set is the remote.
    """
    res = _git(["for-each-ref", "--format=%(refname)", "refs/remotes/"])
    if res.returncode != 0:
        # The third of this file's three `for-each-ref`/`rev-list` declines,
        # and the one that was left on `str.splitlines()` with nothing marking
        # it at all (#1654). `[0]` took whatever sat before a U+2028 and threw
        # the rest away, and an ESC in this stream reached the board raw — a
        # `[2K[1A` erases the row above the one it is rendered into, which no
        # splitter of any kind removes (#851). Same two calls as its siblings
        # at `upstream_refs` and `unpushed_for`.
        why = _untrusted.split_lines((res.stderr or "").strip())
        return None, _untrusted.flat(why[0] if why else
                                     f"git for-each-ref exited {res.returncode}")
    names = {}
    all_refs = []
    # `split_lines`, matching `upstream_refs` below, and it closes the risk the
    # #1130 register registered against this very line (#1654). A refname may
    # carry U+2028 — `check-ref-format` exits 0 on
    # `refs/remotes/origin/decoy<U+2028>refs/remotes/origin/mybranch`, verified
    # — so `str.splitlines()` read ONE published ref back as TWO records and a
    # hostile remote could spell your branch name in the tail, making an
    # unpushed branch read as pushed. One record now: its fourth component is
    # the whole forged tail and matches nothing.
    for line in _untrusted.split_lines(res.stdout):
        ref = line.strip()
        # `refs/remotes/<remote>/<name>` — the same three components
        # `%(refname:strip=3)` used to drop, kept here so the ref survives.
        parts = ref.split("/", 3)
        if len(parts) != 4 or not parts[3]:
            continue
        # Two remotes can carry the same branch name, and this key cannot hold
        # both. `origin` wins the *name* lookup rather than whichever sorts
        # first — but that preference is no longer what the count is taken
        # against (#1525): it decides only the fallback for a branch that
        # tracks nothing, and `all_refs` carries every ref so `_sync_for` can
        # ask about the one the branch actually tracks.
        if parts[3] not in names or parts[2] == "origin":
            names[parts[3]] = ref
        all_refs.append(ref)
    return RemoteRefs(names, all_refs), ""


def upstream_refs():
    """What each local branch tracks, as `(mapping, why)` (#1525).

    `branch name → (upstream ref, remote name)`, both `""` for a branch that
    tracks nothing — a third state, not a zero. `(None, why)` when git did not
    answer, and that `None` is not allowed to decay into "tracks origin",
    which is the whole of the mistake this exists to end.

    `%(upstream)` is *configuration* — `branch.<b>.remote` plus `.merge` — not
    ref existence: a branch whose upstream was deleted on the remote and pruned
    here still reports the ref it tracks. That is deliberate. The row is about
    that remote whether or not a ref for it is on disk, and the alternative is
    silently measuring against whichever other remote happens to carry a branch
    of the same name.
    """
    res = _git(["for-each-ref",
                "--format=%(refname:strip=2)%09%(upstream)%09%(upstream:remotename)",
                "refs/heads/"])
    if res.returncode != 0:
        # `flat`, not the bare line: this is git's own stderr about refs a
        # remote named, and it is rendered as one line of the board. The
        # ratchet in `tests/test_forged_child_stream_line_1475.py` is what
        # caught this site — the targeted runs never touched it.
        why = _untrusted.split_lines((res.stderr or "").strip())
        return None, _untrusted.flat(why[0] if why else
                                     f"git for-each-ref exited {res.returncode}")
    ups = {}
    for line in _untrusted.split_lines(res.stdout):
        # A git ref name cannot contain a tab, so three fields is the whole
        # record shape. Anything else is a name carrying its own line
        # separator (`check-ref-format` accepts U+2028, #1119) and is dropped
        # rather than half-read — that branch then falls to the name-keyed
        # fallback, which says out loud that it was picked by name.
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0]:
            continue
        ups[parts[0]] = (parts[1], parts[2])
    return ups, ""


class Sync:
    """Is a branch in sync with its own remote ref? `ahead` commits, or `None`.

    `None` is the state the old rendering never had: `ahead == 0` and "the
    count could not be taken" are different facts, and only one of them
    licenses the word *published* (#1496).
    """

    __slots__ = ("ahead", "why", "ref", "how")

    def __init__(self, ahead, why: str = "", ref: str = "",
                 how: str = "") -> None:
        self.ahead = ahead
        self.why = why
        #: The ref the count was taken against, and why that ref. A count
        #: against an unnamed remote cannot be checked by the reader (#1525),
        #: so both travel with the number rather than being reconstructed by
        #: whoever renders it.
        self.ref = ref
        self.how = how

    def __repr__(self) -> str:
        return f"Sync({self.ahead!r}, {self.why!r}, {self.ref!r}, {self.how!r})"


#: Why `Sync.ref` is the ref it is. Two answers, and the difference is the
#: point: one is the branch's own upstream, the other only shares its name.
HOW_UPSTREAM = "the remote this branch tracks"
HOW_BY_NAME = "picked by name: this branch has no upstream configured"


def unpushed_for(branch: str, remote_ref: str, how: str = "") -> Sync:
    """Commits on `branch` that are not on `remote_ref`, as a `Sync`.

    Ahead only. *Behind* is a fact about the remote having moved and says
    nothing about whether this tree's work survives being discarded, which is
    the question the publication line is read for.

    Every way of not getting an answer — no ref to measure against, a ref that
    does not resolve, git failing, a count that is not a number — returns
    `ahead=None` with the reason. `0` is reserved for a count that was actually
    taken, because `0` is what the caller turns into "published".
    """
    if not branch or not remote_ref:
        return Sync(None, "no remote ref to measure this branch against",
                    how=how)
    res = _git(["rev-list", "--count", f"{remote_ref}..refs/heads/{branch}"])
    if res.returncode != 0:
        # `_untrusted.split_lines`, not `str.splitlines()`: this is git's own
        # stderr about refs a remote named, and it is rendered as one line of
        # the board. A U+2028 in it must not re-cut the message the reader is
        # shown — the same reason the rest of this family narrowed (#1081).
        why = _untrusted.split_lines((res.stderr or "").strip())
        return Sync(None, (why[0] if why else
                           f"git rev-list exited {res.returncode}"),
                    remote_ref, how)
    text = res.stdout.strip()
    if not text.isdigit():
        return Sync(None, "git rev-list --count answered with "
                          f"{text[:40]!r}, which is not a count", remote_ref, how)
    return Sync(int(text), "", remote_ref, how)


def _pr_detail(pr: dict) -> str:
    number = pr.get("number", "?")
    base = pr.get("baseRefName") or "?"
    tally = _checks.summarize_github(pr.get("statusCheckRollup"))
    bits = [f"PR #{number} → {base}", tally]
    mergeable = pr.get("mergeable")
    if isinstance(mergeable, str) and mergeable:
        bits.append(mergeable)
    if pr.get("isDraft"):
        bits.append("DRAFT")
    return " · ".join(bits)


def tracker_for(branch: str, index, remote_branches, remote_why: str = "",
                sync: "Sync | None" = None) -> Tracker:
    """Which of the four answers holds for this worktree's branch.

    Order matters. The lookup's own failure is consulted **first**, because
    every question below it is a question about a map that does not exist —
    and answering one from local state is precisely how a dropped connection
    gets printed as "nothing is published here".
    """
    if not branch:
        return Tracker(TRACKER_NA, "PR n/a",
                       "no branch checked out here (detached or bare) — "
                       "there is nothing to look a PR up by")

    if index is None or not index.answered:
        why = (index.reason if index is not None else "the lookup was not run")
        return Tracker(TRACKER_UNKNOWN, "PR unknown",
                       f"PR unknown — the lookup did not answer ({why}). This is the "
                       "tool failing to look, NOT a finding that no PR exists")

    pr = index.get(branch)
    if pr is not None:
        return Tracker(TRACKER_PR, f"PR #{pr.get('number', '?')}", _pr_detail(pr))

    if index.truncated:
        return Tracker(TRACKER_UNKNOWN, "PR unknown",
                       f"PR unknown — the open-PR page hit its {index.limit}-item cap, "
                       "so this branch's absence from it establishes nothing")

    if remote_branches is None:
        return Tracker(TRACKER_NONE, "no open PR",
                       "no open PR tracks this branch. Whether it has been pushed at "
                       f"all is UNKNOWN ({remote_why or 'the remote refs were not read'})")

    if branch not in remote_branches:
        # Deliberately NOT "never pushed". Run live against the fleet, this leg
        # first said exactly that about four branches carrying `[merged]` —
        # every one of them pushed, merged and then deleted on the remote. A
        # deleted remote branch and a never-published one leave the identical
        # local trace, and `branch.<name>.merge` does not separate them either:
        # `git worktree add -b X … master` writes an upstream of origin/master
        # for a branch that has never left the machine. So the observation is
        # reported and both readings are named.
        return Tracker(TRACKER_NO_REMOTE, "no remote ref",
                       "no remote-tracking ref for this branch here — it was either "
                       "never pushed, or its remote branch has been deleted (the usual "
                       "state after a merge). Either way there is no open PR to find. "
                       "(Local knowledge: remote refs are only as fresh as the last fetch.)")

    # A remote-tracking ref exists. That is NOT the same claim as "the work is
    # published", and printing it as one was #1496: the live clone on `master`
    # was one commit ahead of `origin/master` and the row read `the work is
    # published but unproposed`. Read by somebody deciding whether a tree can
    # be discarded, `published` reads as `safe to remove`.
    # `not measured` is its own token and not `no open PR`, because the row
    # line is all some readers see and the two facts led to opposite actions
    # (#1525).
    if sync is None:
        return Tracker(TRACKER_NONE, "sync not measured, no open PR",
                       "a remote-tracking ref exists for this branch's name and no "
                       "open PR tracks it. Whether the branch is in SYNC with any "
                       "remote was not measured here, so no publication claim is "
                       "made about this branch")
    if sync.ahead is None:
        return Tracker(TRACKER_NONE, "sync not measured, no open PR",
                       "no open PR tracks this branch, and whether every local "
                       "commit is on the remote it tracks is UNKNOWN — NOT "
                       f"measured ({sync.why}) — so this declines rather than "
                       "claiming the work is safely on a remote")
    # A `Sync` built without a ref still renders a sentence: `its remote ref`
    # is what this line said before #1525 and is the honest fallback, where
    # `NOT on , and` is not a sentence at all.
    where = sync.ref or "its remote ref"
    if sync.how:
        where = f"{where} ({sync.how})"
    if sync.ahead > 0:
        return Tracker(TRACKER_NONE, f"{sync.ahead} unpushed, no open PR",
                       f"{sync.ahead} commit(s) here are NOT on {where}, and no "
                       "open PR tracks the branch — the work is NOT published: "
                       "those commits exist only in this clone")
    return Tracker(TRACKER_NONE, "no open PR",
                   f"every commit here is also on {where}, and no open PR tracks "
                   "the branch — the work is published but unproposed")


# ── rendering ────────────────────────────────────────────────────────────

def _sync_for(branch: str, remote_names, upstreams,
              upstream_why: str) -> "Sync | None":
    """The publication measurement for one row, or `None` if there is none to make.

    `None` for a branch with no remote ref (the row says that already) and for a
    caller that handed a bare set rather than the `remote_branch_names` mapping
    — in both cases the count was not taken, and `tracker_for` says so instead
    of inferring it.

    **Which ref the count is taken against**, in order (#1525):

    1. **The ref the branch tracks**, when that is `refs/remotes/<remote>/
       <branch>` and it is in this clone. The rule before this preferred
       `origin` unconditionally, so on a fork layout — upstream `fork/X`, an
       `origin/X` at a different commit — the row counted against a remote it
       was never about, in a sentence that named no remote at all. Measured
       live before the fix: a branch one commit ahead of its `fork` upstream
       read *in sync with its remote ref … published but unproposed*.
    2. **Nothing, deliberately**, when the branch tracks a ref that is not here
       (deleted on the remote, or never fetched) or one that is a *different*
       branch — `git worktree add -b X … master` leaves X tracking
       `origin/master`, and measuring X against that is #1496's mistake with a
       fresh face. A same-named ref on another remote is not a substitute: it
       answers "0 commits missing" about a remote this row is not about, which
       is exactly the reading being removed. `Sync(None, why)` renders as *not
       measured*, which the row shows differently from *in sync*.
    3. **The same-named ref**, `origin` preferred, when the branch tracks
       nothing at all. The commits really are on it, and that is what somebody
       deciding whether to discard a tree is asking — but the row says the ref
       was picked by name, because nothing establishes it is the branch's.

    `upstreams` and `upstream_why` are required, not defaulted — the same
    discipline as `push.py`'s `fallback_remote` (#656), for the same reason: a
    new call site cannot reintroduce the origin guess by omitting an argument.

    One `git rev-list --count` per *measured* branch, local and cheap. It is not
    batched into the single `for-each-ref` above because a count per ref is what
    is being asked for, and it costs no network.
    """
    if not branch or not isinstance(remote_names, dict):
        return None
    by_name = remote_names.get(branch)
    if upstreams is None:
        return Sync(None, "which remote this branch tracks could not be read "
                          f"({upstream_why or 'the upstreams were not read'}) "
                          "— so no count was taken: a count against a remote "
                          "the row may not be about is worse than no count")
    up_ref, up_remote = upstreams.get(branch, ("", ""))
    if up_ref:
        if not up_remote or up_ref != f"refs/remotes/{up_remote}/{branch}":
            return Sync(None, f"this branch tracks {up_ref}, which is a "
                              "different branch and not a remote copy of this "
                              "one, so no count was taken against it")
        if up_ref not in getattr(remote_names, "all_refs", frozenset([up_ref])):
            other = (f"; {by_name} is here but belongs to another remote and "
                     "was NOT measured") if by_name and by_name != up_ref else ""
            return Sync(None, f"this branch tracks {up_ref} and there is no "
                              "such remote-tracking ref in this clone — deleted "
                              f"on {up_remote}, or never fetched here{other}")
        return unpushed_for(branch, up_ref, HOW_UPSTREAM)
    return unpushed_for(branch, by_name, HOW_BY_NAME) if by_name else None


def _exit_note(code: int, why: str) -> str:
    """The line that makes the exit status attributable (#1496).

    A non-zero naming nothing in the body and a zero hiding something are the
    same defect: a caller gating on the status and a caller reading the render
    disagreed about the same call, and the render was the one with no way to
    settle it. This does not change any code — it says which line produced it.
    """
    return (f"[exit {code}] {why}. This integer is the SAFE-TO-REAP answer "
            f"compressed into one and nothing more "
            f"({EXIT_IDLE} = idle and clean, {EXIT_OCCUPIED} = occupied, "
            f"{EXIT_UNKNOWN} = cannot tell, or the op could not answer at all, "
            f"{EXIT_DIRTY} = idle but holding uncommitted work). Only "
            f"{EXIT_IDLE} is clear to proceed on, and #1751 made FEWER trees "
            f"qualify for it, never more: `idle` now has to be clean too. A "
            f"detached tree has no merge column, so `idle` alone used to "
            f"authorise deleting work that existed nowhere else")


def render(rows: list, exit_note: str = "") -> str:
    """The board, and the guarantee it makes about its own shape.

    **A row is one line plus one line per piece of evidence, whatever it is
    handed.** The verdict word, the tally and the labels are the tool's; the
    branch, the path and every filename inside the evidence are not, and are
    flattened on the way in (`_untrusted.flat`). After that nothing a stranger
    named can reach column 0, add a line, imitate the column gaps with a tab,
    or move the cursor back over a line already printed — the three routes a
    crafted filename had to a forged `idle` row (#876).

    Flattening, not rejecting or quoting: the reader is an agent under context
    pressure that has to act on the path, so an unreadable path is its own
    failure. `repr()` — the sibling answer in `resolve.py` — is right for a
    heading quoted inside a sentence and wrong for a column of paths, where it
    would quote and backslash-escape every row to disclose the one. `flat()`
    leaves an ordinary path exactly as it was typed and shows a control
    character as itself.
    """
    flat = _untrusted.flat
    out = [f"# git-worktrees ({len(rows)})",
           _untrusted.flat_note("Branch names, paths and the filenames in the evidence",
                                source="the filesystem"),
           ""]
    for row in rows:
        entry, verdict = row[0], row[1]
        tracker = row[2] if len(row) > 2 else None
        merge = row[3] if len(row) > 3 else None
        dirty = row[4] if len(row) > 4 else None
        branch = entry.get("branch") or ("(detached)" if entry.get("detached") else "?")
        tags = []
        # Every state prints, including `not merged`. The absent tag was the
        # #1229 defect: a row carrying nothing reads as unmerged work, and it
        # was wrong that way on 16 of 24 rows on the live fleet.
        if merge is not None and merge.state != MERGED_NA:
            tags.append(merge.token)
        # And `clean` prints for the same reason (#1751). A dirty column that
        # only spoke when it found something would make "no tag" mean both
        # `clean` and `the column did not run` — #1229's defect, re-added.
        if dirty is not None:
            tags.append(dirty.token)
        if entry.get("prunable"):
            tags.append("prunable")
        suffix = f"  [{', '.join(tags)}]" if tags else ""
        token = f"  {flat(tracker.token)}" if tracker is not None else ""
        out.append(f"{verdict.state:<12} {flat(str(branch)):<26} "
                   f"{flat(str(entry.get('path', '?')), disclose_newline=True)}"
                   f"{suffix}{token}")
        for item in verdict.evidence:
            out.append(f"             · {flat(str(item))}")
        if tracker is not None:
            out.append(f"             · {flat(tracker.detail)}")
        if merge is not None:
            out.append(f"             · {flat(merge.detail)}")
        if dirty is not None:
            out.append(f"             · {flat(dirty.detail)}")
        out.append("")

    # The whole-board `merged-into-base: unknown — <why>` line is gone with
    # #1229: it was the third state applied once, to every row at once, and
    # the per-row states supersede it. Two mechanisms for one fact is where a
    # board drifts from what it is measuring.

    tally = {STATE_OCCUPIED: 0, STATE_IDLE: 0, STATE_UNKNOWN: 0}
    unknown_trackers = 0
    unknown_merges = 0
    dirty_trees = 0
    unknown_dirty = 0
    for row in rows:
        verdict = row[1]
        tally[verdict.state] = tally.get(verdict.state, 0) + 1
        tracker = row[2] if len(row) > 2 else None
        if tracker is not None and tracker.state == TRACKER_UNKNOWN:
            unknown_trackers += 1
        merge = row[3] if len(row) > 3 else None
        if merge is not None and merge.state == MERGED_UNKNOWN:
            unknown_merges += 1
        dirty = row[4] if len(row) > 4 else None
        if dirty is not None and dirty.state == DIRTY_DIRTY:
            dirty_trees += 1
        if dirty is not None and dirty.state == DIRTY_UNKNOWN:
            unknown_dirty += 1
    # The tracker count rides the one line that survives `| tail -1`. A row
    # whose PR state was never read has to be visible from there, or the
    # summary is the place the missing answer disappears.
    tracker_part = (f", {unknown_trackers} tracker unknown"
                    if unknown_trackers else "")
    # Same reasoning as the tracker count, and the same line: a row whose merge
    # state was never established must be visible from `| tail -1`, or the
    # summary is where the missing answer disappears (#1229).
    merge_part = (f", {unknown_merges} merge unknown" if unknown_merges else "")
    # The count a reap reads before it deletes anything, on the same surviving
    # line as the two above (#1751). A tree holding uncommitted work is the one
    # row where the destructive call is unrecoverable, so it is not allowed to
    # live only in the body.
    dirty_part = (f", {dirty_trees} DIRTY" if dirty_trees else "")
    dirty_unknown_part = (f", {unknown_dirty} dirty unknown"
                          if unknown_dirty else "")
    # Above `[result]`, never below it: that line is what `gh-pr-merge` and
    # every `| tail -1` reader take the tally off, so the exit disclosure sits
    # next to it rather than after it (#1496).
    if exit_note:
        out.append(exit_note)
    out.append(
        f"[result] {tally[STATE_OCCUPIED]} occupied, {tally[STATE_IDLE]} idle, "
        f"{tally[STATE_UNKNOWN]} cannot tell{tracker_part}{merge_part}"
        f"{dirty_part}{dirty_unknown_part} — "
        "'cannot tell' is NOT "
        "'idle': nothing answered, so treat that tree as occupied until something does"
        + (" · a tracker 'unknown' is the lookup failing, not an absent PR"
           if unknown_trackers else "")
    )
    return "\n".join(out)


def _merged_branches():
    """Branches that are ANCESTORS of the base — `(set|None, why, base)`.

    Renamed in meaning by #1229 rather than in code: this was the whole of the
    `[merged]` decision and is now one half of it, read as a positive only.
    `--merged` is an ancestry test, and this repo squash-merges, so its `no` is
    not a finding. `merged_for` owns what the row says; this owns the local
    half of the evidence, and now returns the base it measured against so the
    row can name it.
    """
    for base in ("master", "main"):
        if _git(["rev-parse", "--verify", "--quiet", base]).returncode != 0:
            continue
        res = _git(["for-each-ref", "--format=%(refname:short)", "--merged", base, "refs/heads"])
        if res.returncode != 0:
            return None, f"git for-each-ref --merged {base} exited {res.returncode}", base
        return set(res.stdout.split()), "", base
    return (None,
            "neither master nor main resolves here — no base to measure against",
            "master")


#: Tokens that are flags rather than a PATH.
_FLAGS = {"nopr"}

#: Values of SUPERTOOL_WORKTREE_PR that turn the tracker column off.
_OFF = {"0", "false", "no", "off"}


#: Prefix for the #2272 declaration. `since=90` means "I wrote this tree
#: myself 90 seconds ago"; `since=@1725500000` means an absolute unix epoch,
#: for a caller that already holds one rather than an elapsed duration.
_SINCE_PREFIX = "since="


def _parse_since(value: str, now: float):
    """A `since=` argument's raw value into `(known_good_since, why)` (#2272).

    Two forms, because the two callers who would reach for this hold
    different facts. A maintainer who just ran `git merge && git push`
    typically knows "that was 90 seconds ago" — an elapsed duration — not a
    memorised epoch, so a plain number means seconds-ago-from-now. `@<epoch>`
    is the escape for a caller that already has an absolute timestamp (a log
    line, a prior call's own `now`) and would otherwise have to compute an
    elapsed duration back out of it.

    Returns `(None, why)` on anything that does not parse or is negative — a
    duration cannot be negative, and a caller who mistyped this should see a
    refusal, not a declaration silently applied against the wrong instant.
    """
    if not value:
        return None, "since= given with no value"
    if value.startswith("@"):
        raw = value[1:]
        try:
            return float(raw), None
        except ValueError:
            return None, f"since=@{raw!r} is not a number of seconds since the epoch"
    try:
        seconds_ago = float(value)
    except ValueError:
        return None, (f"since={value!r} is not understood — use `since=<seconds-ago>` "
                      "or `since=@<unix-epoch-seconds>`")
    if seconds_ago < 0:
        return None, f"since={value!r} is negative — a duration ago cannot be negative"
    return now - seconds_ago, None


def parse_args(argv: list) -> tuple:
    """`(path, want_pr, since_raw)` from the op's arguments.

    The tracker column is **on by default**, with `nopr` and
    `SUPERTOOL_WORKTREE_PR=0` to turn it off. On rather than opt-in because the
    friction #941 reports is a join done in the reader's head, and a suffix
    only helps the reader who already knows the suffix exists — which is not
    the one reaching for this board at speed. The cost is bounded and stated:
    **two** `gh` calls per run, whatever the tree count — the open-PR index and,
    since #1229, the merged-PR lookup, which cannot ride the first because that
    one is `--state open` — both on a short timeout, and a call that fails
    degrades to `PR unknown` / `merge unknown` rather than to a wrong answer or
    a slower op.

    `since_raw` (#2272) is the caller's own declaration of a known-good
    cutoff, left unparsed here — `main` resolves it against `time.time()` at
    the point it is used, and only for the single tree the call is about, so
    the parsing failure mode (a refused argument) belongs there, not here.
    """
    path = ""
    since_raw = None
    want_pr = (os.environ.get("SUPERTOOL_WORKTREE_PR", "").strip().lower()
               not in _OFF)
    for arg in argv:
        if arg in _FLAGS:
            want_pr = False
        elif arg.startswith(_SINCE_PREFIX):
            since_raw = arg[len(_SINCE_PREFIX):]
        elif not path:
            path = arg
    return path, want_pr, since_raw


def main() -> int:
    use_utf8_stdout()
    # First line of the report, before the refusals below it: this op answers
    # "whose tree is this" everywhere except about the directory it was called
    # from, and a copied worktree is the one case where the listing that
    # follows is a listing of somebody else's repository (#1536).
    _copy = foreign_worktree()
    if _copy is not None:
        print(foreign_worktree_note(_copy))
        print(f"  {_copy[0]} is a copy — `cp` copies a worktree's `.git` "
              f"pointer, not its repository. It is not in the list below, "
              f"because git does not know it exists.")
    wanted, want_pr, since_raw = parse_args(sys.argv[1:])
    if wanted.startswith("-"):
        print(f"ERROR: refused — PATH must name a worktree, not an option: {wanted!r}")
        print("  usage: worktrees.py [PATH]   (inspection only; nothing is removed)")
        # Every return from here down names its own code (#1496): an unattributed
        # status is unreadable whether the cause is a refusal, a failure or a
        # verdict, and the reader cannot tell which without being told.
        print(_exit_note(EXIT_UNKNOWN, "nothing was inspected — the argument was "
                                       "refused, see the ERROR above"))
        return EXIT_UNKNOWN

    # #2272: `since=` declares a known-good cutoff for the ONE tree a caller
    # is asking about — "I wrote this myself, at or before this instant". It
    # is refused rather than silently ignored on either failure mode: no PATH
    # means there is no single tree to apply one caller's declaration to
    # (this board can hold many independent trees at once), and a value that
    # does not parse must not be applied against the wrong instant.
    known_good_since = None
    if since_raw is not None:
        if not wanted:
            print("ERROR: refused — since= requires a PATH: it declares a "
                  "known-good cutoff for one worktree's own occupancy signal, "
                  "and there is no single tree to apply it to when the whole "
                  "board is requested")
            print(_exit_note(EXIT_UNKNOWN, "nothing was inspected — since= was "
                                           "refused, see the ERROR above"))
            return EXIT_UNKNOWN
        known_good_since, since_why = _parse_since(since_raw, time.time())
        if known_good_since is None:
            print(f"ERROR: refused — {since_why}")
            print(_exit_note(EXIT_UNKNOWN, "nothing was inspected — since= was "
                                           "refused, see the ERROR above"))
            return EXIT_UNKNOWN

    listing = _git(["worktree", "list", "--porcelain"])
    if listing.returncode != 0:
        print(f"ERROR: git worktree list failed ({listing.returncode}): {listing.stderr.strip()}")
        print(_exit_note(EXIT_UNKNOWN, "the op could not answer at all — git did "
                                       "not list the worktrees, see the ERROR above"))
        return EXIT_UNKNOWN

    entries = parse_worktree_list(listing.stdout)
    for entry in entries:
        entry["gitdir"] = resolve_gitdir(entry["path"])

    if wanted:
        entries = [e for e in entries if _inside(wanted, e["path"]) or _inside(e["path"], wanted)]
        if not entries:
            shown = _untrusted.flat(wanted, disclose_newline=True)
            print(f"# git-worktrees\n\ncannot tell   {shown}")
            print(f"             · {shown} is not a worktree of this repository — "
                  "nothing was inspected, so nothing is claimed")
            print(_exit_note(EXIT_UNKNOWN, "nothing was inspected because that "
                                           "PATH is not a worktree of this "
                                           "repository, so the answer is "
                                           "`cannot tell` — the op itself did "
                                           "not fail"))
            return EXIT_UNKNOWN

    ancestors, ancestors_why, base = _merged_branches()
    index = query_open_prs_by_branch() if want_pr else None
    # The second `gh` call, and it has to be a second one: the index above is
    # `--state open`, and a merged PR is absent from it by construction (#1229).
    # It is scoped to the branches this board holds rather than paging the
    # repo's merged history — that history only grows, so any page size is a
    # cap the repo passes and never comes back under. It rides the same
    # `want_pr` switch, so `nopr` stays fully offline — and offline the merge
    # column answers `merged` from ancestry or `unknown`, never `not merged`.
    merged_prs = (query_merged_prs_for_branches(
        [e.get("branch") or "" for e in entries]) if want_pr else None)
    remote_names, remote_why = remote_branch_names() if want_pr else (None, "")
    # Which remote each branch tracks, so the count below is about *that* one
    # rather than whichever remote happens to carry the same branch name
    # (#1525). One call for the whole board, like the ref listing above.
    upstreams, upstream_why = upstream_refs() if want_pr else (None, "")
    memo: dict = {}
    rows = [(entry,
             assess(entry, scan=_cwd_scan(entry["path"], memo),
                    known_good_since=known_good_since),
             tracker_for(entry.get("branch") or "", index, remote_names,
                         remote_why,
                         sync=_sync_for(entry.get("branch") or "", remote_names,
                                        upstreams, upstream_why))
             if want_pr else None,
             merged_for(entry.get("branch") or "", ancestors, merged_prs,
                        ancestors_why=ancestors_why, base=base),
             # Every row, branch or not — that is the point of #1751. The one
             # tree whose merge column can never answer is the detached one,
             # and it is the tree this column exists for.
             dirty_for(entry.get("path", "")))
            for entry in entries]
    # The code is decided before the render so the render can disclose it. Every
    # arm below is unchanged in what it returns (#1282's included); what is new
    # is that the body names the integer and what produced it (#1496).
    if wanted and len(rows) == 1:
        state = rows[0][1].state
        dirt = rows[0][4]
        # The TRACKER column stays out of this integer, and that reasoning is
        # unchanged: a lookup that did not answer says nothing about whether
        # the tree is safe to enter, and folding it in would make
        # `git-worktrees:PATH` refuse a free worktree because GitHub was down.
        #
        # The DIRTY column is in it, and is a different kind of claim (#1751).
        # It is a LOCAL read about the tree itself, in the same class as
        # occupancy rather than as the network columns — and it is the only
        # thing that answers the question the gated call destroys. A detached
        # tree has no merge column at all, so `idle` at exit 0 was the whole
        # verdict over work that exists nowhere else.
        code = exit_code_for(state, dirt.state)
        if code == EXIT_DIRTY:
            why = ("the one worktree asked about is `idle` but holds "
                   f"UNCOMMITTED WORK ({dirt.count} change record"
                   f"{'' if dirt.count == 1 else 's'}) — nobody is in it, and "
                   "removing it would destroy work that exists nowhere else. "
                   "The op itself did not fail")
        elif code == EXIT_UNKNOWN and state == STATE_IDLE:
            why = ("the one worktree asked about is `idle`, but whether it "
                   "holds uncommitted work could not be read, so this call "
                   "cannot certify it — the op itself did not fail")
        else:
            why = ("the occupancy verdict for the one worktree asked about is "
                   f"`{state}` — the op itself did not fail")
    elif wanted:
        # More than one row: the filter above is ancestor-or-descendant, so a
        # nested layout pulls in the trees above and below the named one and
        # the board is no longer about it. Returning the idle code here printed
        # `0 idle` and exited 0 at the same time (#1282) — and gh-pr-merge's
        # cleanup arm read that code as permission to delete the directory.
        # `cannot tell` is the only honest answer for a board of many, and it
        # is what the render already says.
        code = EXIT_UNKNOWN
        why = (f"the PATH given matched {len(rows)} worktrees (the filter is "
               "ancestor-or-descendant), so no row here is an answer about it "
               "— the op itself did not fail")
    else:
        code = EXIT_IDLE
        why = ("no PATH was given, so this is the whole board and the status is "
               "not a verdict about any tree in it — read the rows; the op "
               "itself did not fail")
    print(render(rows, exit_note=_exit_note(code, why)))
    return code


if __name__ == "__main__":
    sys.exit(main())
