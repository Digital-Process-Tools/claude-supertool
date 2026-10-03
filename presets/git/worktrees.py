#!/usr/bin/env python3















































from __future__ import annotations

import math
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

from _git_common import (  
    _git, _git_verbatim, use_utf8_stdout, query_open_prs_by_branch,
    query_merged_prs_for_branches,
    foreign_worktree, foreign_worktree_note,  
)
from _env import env_int  
import _checks  
import _untrusted  
from _spawnable import which_excluding_cwd  

STATE_OCCUPIED = "occupied"
STATE_IDLE = "idle"
STATE_UNKNOWN = "cannot tell"












EXIT_IDLE = 0
EXIT_OCCUPIED = 1
EXIT_UNKNOWN = 2
EXIT_DIRTY = 3



ACTIVE_WINDOW_DEFAULT = 900










IDLE_QUIET_DEFAULT = 3600



CWD_SCAN_TIMEOUT = 10



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






    __slots__ = ("answer", "detail", "pids")

    def __init__(self, answer: str, detail: str, pids: list | None = None) -> None:
        self.answer = answer
        self.detail = detail
        self.pids = list(pids or [])

    def __repr__(self) -> str:
        return f"CwdScan({self.answer!r}, {self.detail!r}, pids={self.pids!r})"









TRACKER_PR = "pr"
TRACKER_NONE = "none"
TRACKER_NO_REMOTE = "no-remote-ref"
TRACKER_UNKNOWN = "unknown"
TRACKER_NA = "n/a"


class Tracker:


    __slots__ = ("state", "token", "detail")

    def __init__(self, state: str, token: str, detail: str) -> None:
        self.state = state
        self.token = token
        self.detail = detail

    def __repr__(self) -> str:
        return f"Tracker({self.state!r}, {self.token!r}, {self.detail!r})"











MERGED_YES = "merged"
MERGED_NO = "not-merged"
MERGED_UNKNOWN = "unknown"
MERGED_NA = "n/a"










MERGED_NO_COMMITS = "no-own-commits"


class Merged:







    __slots__ = ("state", "token", "detail")

    def __init__(self, state: str, token: str, detail: str) -> None:
        self.state = state
        self.token = token
        self.detail = detail

    def __repr__(self) -> str:
        return f"Merged({self.state!r}, {self.token!r}, {self.detail!r})"


def branch_ever_committed(branch: str, runner=None):


































    if not branch:
        return None






    run = _git_verbatim if runner is None else runner



    res = run(["reflog", "show", "--format=%gs", "refs/heads/" + branch])
    if res.returncode != 0:
        return None
    entries = [line.strip() for line in res.stdout.split(chr(10)) if line.strip()]
    if not entries:
        return None








    return bool(any(not e.startswith("branch: Created") for e in entries))


def merged_for(branch: str, ancestors, merged_prs, ancestors_why: str = "",
               base: str = "master", ever_committed=None) -> Merged:


























    if not branch:
        return Merged(MERGED_NA, "merge n/a",
                      "merged: n/a — no branch checked out here (detached or "
                      "bare), so there is nothing to measure against " + base)

    if ancestors is not None and branch in ancestors:




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










DIRTY_CLEAN = "clean"
DIRTY_DIRTY = "dirty"
DIRTY_UNKNOWN = "unknown"


DIRTY_SCAN_TIMEOUT = 10


class Dirty:














    __slots__ = ("state", "token", "detail", "count")

    def __init__(self, state: str, token: str, detail: str, count: int = 0) -> None:
        self.state = state
        self.token = token
        self.detail = detail
        self.count = count

    def __repr__(self) -> str:
        return f"Dirty({self.state!r}, {self.token!r}, {self.count!r})"


def _count_porcelain_z(out: str) -> int:












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

        if record[:1] in ("R", "C") or record[1:2] in ("R", "C"):
            index += 1
    return count


def dirty_for(path: str, runner=None) -> Dirty:
















    def _default(args):
        return _git(args, timeout=DIRTY_SCAN_TIMEOUT)

    run = _default if runner is None else runner
    res = run(["--no-optional-locks", "-C", path,
               "-c", "status.showUntrackedFiles=normal",
               "status", "--porcelain", "-z"])
    if res.returncode != 0:



        why = _untrusted.flat(
            (res.stderr or "").strip().replace(chr(10), " ")[:200]) or (
            f"git exited {res.returncode} and said nothing")
        return Dirty(DIRTY_UNKNOWN, "dirty unknown",
                     f"uncommitted work: UNKNOWN — the status read did not "
                     f"answer ({why}). This is the tool failing to look, NOT a "
                     f"finding that the tree is clean")



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






    __slots__ = ("state", "evidence")

    def __init__(self, state: str, evidence: list) -> None:
        self.state = state
        self.evidence = list(evidence)

    def __repr__(self) -> str:
        return f"Assessment({self.state!r}, {self.evidence!r})"




def _age(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"




def parse_worktree_list(text: str) -> list:






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




def _lock_signals(gitdir: str | None) -> list:





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

    if not gitdir:
        return []
    found = []
    for name, what in _IN_PROGRESS:
        if os.path.exists(os.path.join(gitdir, name)):
            found.append(f"{what} ({name} present) — the tree is mid-operation")
    return found







_REFLOG_TAIL_BYTES = 8192



_REFLOG_TAIL_MAX_BYTES = 1 << 20  


def _reflog_newest_entry_time(target: str):


































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

    lsof = which_excluding_cwd("lsof")
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




def assess(entry: dict, *, now: float | None = None, window: int | None = None,
           scan: CwdScan | None = None,
           known_good_since: float | None = None) -> Assessment:
















    now = time.time() if now is None else now
    window = env_int("SUPERTOOL_WORKTREE_ACTIVE_WINDOW", ACTIVE_WINDOW_DEFAULT,
                     minimum=1) if window is None else window
    quiet_for = max(window, env_int("SUPERTOOL_WORKTREE_IDLE_QUIET",
                                    IDLE_QUIET_DEFAULT, minimum=1))
    evidence: list = []







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




class RemoteRefs(dict):











    __slots__ = ("all_refs",)

    def __init__(self, mapping=None, all_refs=()) -> None:
        super().__init__(mapping or {})
        self.all_refs = frozenset(all_refs)


def remote_branch_names():



















    res = _git(["for-each-ref", "--format=%(refname)", "refs/remotes/"])
    if res.returncode != 0:







        why = _untrusted.split_lines((res.stderr or "").strip())
        return None, _untrusted.flat(why[0] if why else
                                     f"git for-each-ref exited {res.returncode}")
    names = {}
    all_refs = []








    for line in _untrusted.split_lines(res.stdout):
        ref = line.strip()


        parts = ref.split("/", 3)
        if len(parts) != 4 or not parts[3]:
            continue






        if parts[3] not in names or parts[2] == "origin":
            names[parts[3]] = ref
        all_refs.append(ref)
    return RemoteRefs(names, all_refs), ""


def upstream_refs():














    res = _git(["for-each-ref",
                "--format=%(refname:strip=2)%09%(upstream)%09%(upstream:remotename)",
                "refs/heads/"])
    if res.returncode != 0:




        why = _untrusted.split_lines((res.stderr or "").strip())
        return None, _untrusted.flat(why[0] if why else
                                     f"git for-each-ref exited {res.returncode}")
    ups = {}
    for line in _untrusted.split_lines(res.stdout):





        parts = line.split("\t")
        if len(parts) != 3 or not parts[0]:
            continue
        ups[parts[0]] = (parts[1], parts[2])
    return ups, ""


class Sync:







    __slots__ = ("ahead", "why", "ref", "how")

    def __init__(self, ahead, why: str = "", ref: str = "",
                 how: str = "") -> None:
        self.ahead = ahead
        self.why = why




        self.ref = ref
        self.how = how

    def __repr__(self) -> str:
        return f"Sync({self.ahead!r}, {self.why!r}, {self.ref!r}, {self.how!r})"




HOW_UPSTREAM = "the remote this branch tracks"
HOW_BY_NAME = "picked by name: this branch has no upstream configured"


def unpushed_for(branch: str, remote_ref: str, how: str = "") -> Sync:











    if not branch or not remote_ref:
        return Sync(None, "no remote ref to measure this branch against",
                    how=how)
    res = _git(["rev-list", "--count", f"{remote_ref}..refs/heads/{branch}"])
    if res.returncode != 0:




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






    base = _untrusted.flat(pr.get("baseRefName") or "?")
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








        return Tracker(TRACKER_NO_REMOTE, "no remote ref",
                       "no remote-tracking ref for this branch here — it was either "
                       "never pushed, or its remote branch has been deleted (the usual "
                       "state after a merge). Either way there is no open PR to find. "
                       "(Local knowledge: remote refs are only as fresh as the last fetch.)")









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




def _sync_for(branch: str, remote_names, upstreams,
              upstream_why: str) -> "Sync | None":





































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



        if merge is not None and merge.state != MERGED_NA:
            tags.append(merge.token)



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



    tracker_part = (f", {unknown_trackers} tracker unknown"
                    if unknown_trackers else "")



    merge_part = (f", {unknown_merges} merge unknown" if unknown_merges else "")




    dirty_part = (f", {dirty_trees} DIRTY" if dirty_trees else "")
    dirty_unknown_part = (f", {unknown_dirty} dirty unknown"
                          if unknown_dirty else "")



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



_FLAGS = {"nopr"}


_OFF = {"0", "false", "no", "off"}





_SINCE_PREFIX = "since="


def _parse_since(value: str, now: float):




















    if not value:
        return None, "since= given with no value"
    if value.startswith("@"):
        raw = value[1:]
        try:
            epoch = float(raw)
        except ValueError:
            return None, f"since=@{raw!r} is not a number of seconds since the epoch"
        if not math.isfinite(epoch):
            return None, f"since=@{raw!r} is not a finite point in time"
        if epoch > now:
            return None, (f"since=@{raw!r} is in the future ({epoch - now:.0f}s ahead of "
                          "now) — a known-good point cannot be after now")
        return epoch, None
    try:
        seconds_ago = float(value)
    except ValueError:
        return None, (f"since={value!r} is not understood — use `since=<seconds-ago>` "
                      "or `since=@<unix-epoch-seconds>`")
    if not math.isfinite(seconds_ago):
        return None, f"since={value!r} is not a finite number of seconds"
    if seconds_ago < 0:
        return None, f"since={value!r} is negative — a duration ago cannot be negative"
    return now - seconds_ago, None


def parse_args(argv: list) -> tuple:































    path = ""
    since_raw = None
    since_seen: list = []
    want_pr = (os.environ.get("SUPERTOOL_WORKTREE_PR", "").strip().lower()
               not in _OFF)
    for arg in argv:
        if arg in _FLAGS:
            want_pr = False
        elif arg.startswith(_SINCE_PREFIX):
            since_raw = arg[len(_SINCE_PREFIX):]
            since_seen.append(since_raw)
        elif not path:
            path = arg
    since_conflict = since_seen if len(set(since_seen)) > 1 else None
    return path, want_pr, since_raw, since_conflict


def main() -> int:
    use_utf8_stdout()




    _copy = foreign_worktree()
    if _copy is not None:
        print(foreign_worktree_note(_copy))
        print(f"  {_copy[0]} is a copy — `cp` copies a worktree's `.git` "
              f"pointer, not its repository. It is not in the list below, "
              f"because git does not know it exists.")
    wanted, want_pr, since_raw, since_conflict = parse_args(sys.argv[1:])
    if since_conflict is not None:
        print("ERROR: refused — since= was given more than once with "
              f"DISAGREEING values: {since_conflict!r}. A repeated but "
              "IDENTICAL since= is fine (the same claim twice); two "
              "different ones is two different claims about when the tree "
              "was written, and this declines to silently pick one — name "
              "the one you mean.")
        print(_exit_note(EXIT_UNKNOWN, "nothing was inspected — since= was "
                                       "refused, see the ERROR above"))
        return EXIT_UNKNOWN
    if wanted.startswith("-"):
        print(f"ERROR: refused — PATH must name a worktree, not an option: {wanted!r}")
        print("  usage: worktrees.py [PATH]   (inspection only; nothing is removed)")



        print(_exit_note(EXIT_UNKNOWN, "nothing was inspected — the argument was "
                                       "refused, see the ERROR above"))
        return EXIT_UNKNOWN







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












        if known_good_since is not None and len(entries) != 1:
            print(f"ERROR: refused — since= matched {len(entries)} worktrees "
                  f"for {_untrusted.flat(wanted, disclose_newline=True)!r} (the "
                  "PATH filter is ancestor-or-descendant), and a known-good "
                  "declaration is a claim about ONE tree — applying it to "
                  "every match would attribute a write in an unrelated "
                  "worktree to this declaration too. Name the exact worktree "
                  "path.")
            print(_exit_note(EXIT_UNKNOWN, "nothing was inspected — since= was "
                                           "refused, see the ERROR above"))
            return EXIT_UNKNOWN

    ancestors, ancestors_why, base = _merged_branches()
    index = query_open_prs_by_branch() if want_pr else None







    merged_prs = (query_merged_prs_for_branches(
        [e.get("branch") or "" for e in entries]) if want_pr else None)
    remote_names, remote_why = remote_branch_names() if want_pr else (None, "")



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



             dirty_for(entry.get("path", "")))
            for entry in entries]



    if wanted and len(rows) == 1:
        state = rows[0][1].state
        dirt = rows[0][4]











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
