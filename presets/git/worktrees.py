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
    _git, use_utf8_stdout, query_open_prs_by_branch,
)
from _env import env_int  # noqa: E402
import _checks  # noqa: E402  (the one check tally, shared with gh-pr / gh-prs)
import _untrusted  # noqa: E402  (filenames in a worktree are not our text — #876)

STATE_OCCUPIED = "occupied"
STATE_IDLE = "idle"
STATE_UNKNOWN = "cannot tell"

#: Exit 0 only for the answer that is safe to act on. `cannot tell` gets its
#: own code so a caller cannot collapse it into either neighbour.
EXIT_IDLE = 0
EXIT_OCCUPIED = 1
EXIT_UNKNOWN = 2

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


def _newest_write(path: str, gitdir: str | None, now: float):
    """Newest mtime in the tree and its git dir, as `(age_seconds, label)`.

    Returns `(None, why)` when the answer was not obtained — an unreadable
    tree, or a walk that hit its cap. A truncated walk could have missed a
    newer file, so it must not be allowed to underwrite a claim of quiet.
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
        try:
            mtime = os.stat(target).st_mtime
        except OSError:
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
                if newest is None or mtime > newest:
                    newest = mtime
                    where = f"newest write {os.path.relpath(os.path.join(root, name), path)}"
            if truncated:
                break
    except OSError as exc:
        return None, f"could not walk the worktree ({exc})"

    if newest is None:
        return None, "nothing in the worktree or its git dir could be stat'd"
    age = now - newest
    if truncated and age > ACTIVE_WINDOW_DEFAULT:
        return None, (f"stopped walking after {seen} entries — a newer write may exist "
                      f"below the cap, so quiet cannot be claimed")
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
           scan: CwdScan | None = None) -> Assessment:
    """Three states, and `idle` is the one that has to be earned.

    Any positive signal is `occupied` — a lock, an in-progress operation, a
    `git worktree lock`, a recent write, a process chdir'd inside. Absence of
    all of them is `idle` **only** when every probe that could have spoken did
    speak: the tree was stat'd and is quiet, and the process table was read and
    holds nobody. Otherwise `cannot tell`, naming which probe went silent.
    """
    now = time.time() if now is None else now
    window = env_int("SUPERTOOL_WORKTREE_ACTIVE_WINDOW", ACTIVE_WINDOW_DEFAULT,
                     minimum=1) if window is None else window
    quiet_for = max(window, env_int("SUPERTOOL_WORKTREE_IDLE_QUIET",
                                    IDLE_QUIET_DEFAULT, minimum=1))
    evidence: list = []

    locked = entry.get("locked")
    if locked is not None:
        reason = locked or "no reason given"
        evidence.append(f"git worktree lock held: {reason} — the occupant announced itself")

    evidence.extend(_lock_signals(entry.get("gitdir")))
    evidence.extend(_inprogress_signals(entry.get("gitdir")))

    age, age_label = _newest_write(entry.get("path", ""), entry.get("gitdir"), now)
    if age is not None and age <= window:
        evidence.append(f"{age_label} (inside the {_age(window)} activity window)")

    if evidence:
        if scan is None:
            evidence.append("process-cwd scan not run — occupied on the evidence above")
        else:
            evidence.append(f"process-cwd scan: {scan.detail}")
        return Assessment(STATE_OCCUPIED, evidence)

    if scan is None:
        scan = _cwd_scan(entry.get("path", ""))

    if scan.answer == "yes":
        return Assessment(STATE_OCCUPIED, [scan.detail] + ([age_label] if age is not None else []))

    quiet = [age_label] if age is not None else []
    quiet.append("no index.lock or HEAD.lock, no rebase/merge/cherry-pick in progress, no git worktree lock")

    if scan.answer == "no" and age is not None and age >= quiet_for:
        return Assessment(STATE_IDLE, [scan.detail] + quiet)

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
    return Assessment(STATE_UNKNOWN, reasons + quiet[-1:])


# ── the tracker column ───────────────────────────────────────────────────

def remote_branch_names():
    """Branch names that exist on some remote, as `(names, why)`.

    `(None, why)` when git did not answer — and that `None` is not allowed to
    turn into "never pushed", which is the local half of the same mistake this
    column is about.

    Even when it answers, this is *local* knowledge: remote-tracking refs are
    written by fetch and push, so a branch pushed from another clone since the
    last fetch here looks unpushed. The wording in `tracker_for` says so rather
    than pretending the ref set is the remote.
    """
    res = _git(["for-each-ref", "--format=%(refname:strip=3)", "refs/remotes/"])
    if res.returncode != 0:
        why = (res.stderr or "").strip().splitlines()
        return None, (why[0] if why else f"git for-each-ref exited {res.returncode}")
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}, ""


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


def tracker_for(branch: str, index, remote_branches, remote_why: str = "") -> Tracker:
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

    return Tracker(TRACKER_NONE, "no open PR",
                   "the branch is pushed and no open PR tracks it — the work is "
                   "published but unproposed")


# ── rendering ────────────────────────────────────────────────────────────

def render(rows: list, merged=None, merged_why: str = "") -> str:
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
        branch = entry.get("branch") or ("(detached)" if entry.get("detached") else "?")
        tags = []
        if merged is not None and entry.get("branch") in merged:
            tags.append("merged")
        if entry.get("prunable"):
            tags.append("prunable")
        suffix = f"  [{', '.join(tags)}]" if tags else ""
        token = f"  {flat(tracker.token)}" if tracker is not None else ""
        out.append(f"{verdict.state:<12} {flat(str(branch)):<26} "
                   f"{flat(str(entry.get('path', '?')))}{suffix}{token}")
        for item in verdict.evidence:
            out.append(f"             · {flat(str(item))}")
        if tracker is not None:
            out.append(f"             · {flat(tracker.detail)}")
        out.append("")

    if merged is None:
        out.append(f"merged-into-base: unknown — {merged_why or 'not checked'}")
        out.append("")

    tally = {STATE_OCCUPIED: 0, STATE_IDLE: 0, STATE_UNKNOWN: 0}
    unknown_trackers = 0
    for row in rows:
        verdict = row[1]
        tally[verdict.state] = tally.get(verdict.state, 0) + 1
        tracker = row[2] if len(row) > 2 else None
        if tracker is not None and tracker.state == TRACKER_UNKNOWN:
            unknown_trackers += 1
    # The tracker count rides the one line that survives `| tail -1`. A row
    # whose PR state was never read has to be visible from there, or the
    # summary is the place the missing answer disappears.
    tracker_part = (f", {unknown_trackers} tracker unknown"
                    if unknown_trackers else "")
    out.append(
        f"[result] {tally[STATE_OCCUPIED]} occupied, {tally[STATE_IDLE]} idle, "
        f"{tally[STATE_UNKNOWN]} cannot tell{tracker_part} — 'cannot tell' is NOT "
        "'idle': nothing answered, so treat that tree as occupied until something does"
        + (" · a tracker 'unknown' is the lookup failing, not an absent PR"
           if unknown_trackers else "")
    )
    return "\n".join(out)


def _merged_branches():
    for base in ("master", "main"):
        if _git(["rev-parse", "--verify", "--quiet", base]).returncode != 0:
            continue
        res = _git(["for-each-ref", "--format=%(refname:short)", "--merged", base, "refs/heads"])
        if res.returncode != 0:
            return None, f"git for-each-ref --merged {base} exited {res.returncode}"
        return set(res.stdout.split()), ""
    return None, "neither master nor main resolves here — no base to measure against"


#: Tokens that are flags rather than a PATH.
_FLAGS = {"nopr"}

#: Values of SUPERTOOL_WORKTREE_PR that turn the tracker column off.
_OFF = {"0", "false", "no", "off"}


def parse_args(argv: list) -> tuple:
    """`(path, want_pr)` from the op's arguments.

    The tracker column is **on by default**, with `nopr` and
    `SUPERTOOL_WORKTREE_PR=0` to turn it off. On rather than opt-in because the
    friction #941 reports is a join done in the reader's head, and a suffix
    only helps the reader who already knows the suffix exists — which is not
    the one reaching for this board at speed. The cost is bounded and stated:
    exactly one `gh` call per run, whatever the tree count, on a short timeout,
    and a call that fails degrades to `PR unknown` rather than to a wrong
    answer or a slower op.
    """
    path = ""
    want_pr = (os.environ.get("SUPERTOOL_WORKTREE_PR", "").strip().lower()
               not in _OFF)
    for arg in argv:
        if arg in _FLAGS:
            want_pr = False
        elif not path:
            path = arg
    return path, want_pr


def main() -> int:
    use_utf8_stdout()
    wanted, want_pr = parse_args(sys.argv[1:])
    if wanted.startswith("-"):
        print(f"ERROR: refused — PATH must name a worktree, not an option: {wanted!r}")
        print("  usage: worktrees.py [PATH]   (inspection only; nothing is removed)")
        return EXIT_UNKNOWN

    listing = _git(["worktree", "list", "--porcelain"])
    if listing.returncode != 0:
        print(f"ERROR: git worktree list failed ({listing.returncode}): {listing.stderr.strip()}")
        return EXIT_UNKNOWN

    entries = parse_worktree_list(listing.stdout)
    for entry in entries:
        entry["gitdir"] = resolve_gitdir(entry["path"])

    if wanted:
        entries = [e for e in entries if _inside(wanted, e["path"]) or _inside(e["path"], wanted)]
        if not entries:
            shown = _untrusted.flat(wanted)
            print(f"# git-worktrees\n\ncannot tell   {shown}")
            print(f"             · {shown} is not a worktree of this repository — "
                  "nothing was inspected, so nothing is claimed")
            return EXIT_UNKNOWN

    merged, merged_why = _merged_branches()
    index = query_open_prs_by_branch() if want_pr else None
    remote_names, remote_why = remote_branch_names() if want_pr else (None, "")
    memo: dict = {}
    rows = [(entry,
             assess(entry, scan=_cwd_scan(entry["path"], memo)),
             tracker_for(entry.get("branch") or "", index, remote_names, remote_why)
             if want_pr else None)
            for entry in entries]
    print(render(rows, merged=merged, merged_why=merged_why))

    if wanted and len(rows) == 1:
        state = rows[0][1].state
        # The exit code stays a statement about *occupancy* only. A tracker
        # that did not answer says nothing about whether the tree is safe to
        # enter, and folding it in here would make `git-worktrees:PATH` refuse
        # a free worktree because GitHub was down.
        return {STATE_IDLE: EXIT_IDLE, STATE_OCCUPIED: EXIT_OCCUPIED}.get(state, EXIT_UNKNOWN)
    return EXIT_IDLE


if __name__ == "__main__":
    sys.exit(main())
