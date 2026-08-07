#!/usr/bin/env python3
"""`dashboard` — the join behind "what do I do next", in one read-only call (#953).

Four calls used to answer this, and none of them answered it. A fetch-then-pull
says whether the clone is current; `gh run list --branch master` says whether the
default branch is green; `gh-prs:state=open` says what is on the board;
`git-worktrees` says which trees are occupied. The decision lives in the *join*,
which was performed by hand every time — six times in one session, which is what
filed the issue.

Two of the columns here are assertions a human acts on immediately, so both are
built to decline rather than to guess.

**The verdict.** `MERGE` is read and acted on without a second look, so it is the
worst thing this tool could get wrong. It is derived from the #454 arithmetic via
the same `_checks` tally every other op uses, and from the same second leg count
`gh-pr` reconciles against (#724/#804) — reused by importing `gh-pr`'s own
`_reconcile_checks`, not by re-deriving it. **A tally that does not sum to the
legs the run declares is `UNKNOWN`** — never `WAITING`, and certainly never
`MERGE`. `CANCELLED`, `SKIPPED`, `TIMED_OUT`, `NEUTRAL` and `ACTION_REQUIRED` are
none of them passes, and neither is a state added after this file was written:
`MERGE` requires `_checks.all_green`, which is true only when *every* leg landed
in the passed bucket, so an unrecognised state falls out as `UNKNOWN` by
construction rather than by enumeration.

**Lane occupancy.** A lane reported free while an agent is working in it is how
two agents end up editing one file, which is the failure the lane system exists
to prevent. Occupancy is inferred, and every hop of the inference can be absent,
so the three states are carried all the way up:

* an open PR, or a worktree `git-worktrees` calls `occupied`, makes a lane
  `occupied`;
* a lane whose only signal is a worktree `git-worktrees` could not decide is
  `unknown`, because `cannot tell` is not `idle` one layer down either;
* `free` additionally requires that **every** live occupancy in the repository
  was attributable to some lane. A worktree on `feat/pr-ops` carries no issue
  number, so nothing maps it to a lane — and a lane printed `free` beside an
  occupancy nobody could place is a claim the data does not support. When that
  happens every otherwise-free lane degrades to `unknown` and names the stray.

**Partial failure.** This is several network reads behind one op. A section that
could not be fetched prints its heading, says `!! unread` with the reason, and is
counted in the `[result]` line — because a dashboard missing its board section reads
exactly like a dashboard with an empty board, and that misreading is this repository's
most-filed defect.

**Read-only, permanently.** Nothing here spawns, heals, fetches or mutates. The
clone-currency check is `git ls-remote`, which reads the remote without writing
the local one; every command `next:` can print is a supertool read op. A
subsystem whose inspection was fused to its actions once stayed unobservable for
hours, and `tests/test_dashboard_953.py` pins that this file names no mutating verb.

**GitHub only.** There is no GitLab equivalent and none is half-built here: the
lane vocabulary, `gh-prs` and `git-worktrees`' PR awareness are all GitHub-shaped
today. `gl-mrs` answers the board half for GitLab; the join does not exist there.
"""
from __future__ import annotations

import concurrent.futures as _futures
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRESETS = os.path.dirname(_HERE)
sys.path.insert(0, _PRESETS)

import _checks  # noqa: E402  (the one check tally — #454, shared with every board)
import _untrusted  # noqa: E402  (branch names and paths are not ours — #694/#876)


def _sibling(preset: str, name: str, alias: str):
    """Load `presets/<preset>/<name>.py` under its own module name.

    Composition rather than reimplementation: `gh-pr`'s leg reconciliation and
    `git-worktrees`' three-state assessment are the two pieces of judgement this
    op must not fork. They are imported by path because `pr`, `branch` and
    `worktrees` are names two presets could both want, and an alias keeps a
    traceback readable.
    """
    path = os.path.join(_PRESETS, preset, f"{name}.py")
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    saved = sys.path[:]
    sys.path.insert(0, os.path.join(_PRESETS, preset))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    return mod


_gh_pr = _sibling("github", "pr", "dashboard_gh_pr")
_gh_branch = _sibling("github", "branch", "dashboard_gh_branch")
_worktrees = _sibling("git", "worktrees", "dashboard_git_worktrees")


# ── vocabulary ───────────────────────────────────────────────────────────

MERGE = "MERGE"
RED = "RED"
WAITING = "WAITING"
REBASE = "REBASE"
DRAFT = "DRAFT"
UNKNOWN = "UNKNOWN"

#: Board order, worst-understood first. `UNKNOWN` leads because it is the row a
#: reader must resolve before trusting the rest of the column.
VERDICT_ORDER = (UNKNOWN, RED, REBASE, WAITING, DRAFT, MERGE)

LANE_OCCUPIED = "occupied"
LANE_FREE = "free"
LANE_UNKNOWN = "unknown"

LANE_PREFIX = "lane:"

#: Worktree states that count as a live occupancy. `cannot tell` is here on
#: purpose — an undecidable tree is an occupancy for attribution purposes, and
#: only its *lane* verdict softens to `unknown`.
LIVE_WORKTREE_STATES = (_worktrees.STATE_OCCUPIED, _worktrees.STATE_UNKNOWN)

#: Wall clock the whole render may spend on network reads. Past it a section
#: says it ran out rather than blocking a maintainer who asked a status
#: question. Generous by design: the honest slow answer beats a fast `UNKNOWN`,
#: and every second here is one the four hand-run calls were spending anyway.
BUDGET_DEFAULT = 90

#: Concurrency for the per-PR leg reconciliation. Each PR costs one run list
#: plus up to `_declared_legs.MAX_RECONCILED_RUNS` job lists, so the board is
#: the fan-out and everything else is a handful of calls.
WORKERS_DEFAULT = 8

_ISSUE_IN_BRANCH = re.compile(r"(?<![0-9])([0-9]{2,6})(?![0-9])")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# ── data holders ─────────────────────────────────────────────────────────
#
# Plain classes, not `@dataclass`: `tests/_preset_loader` executes a preset
# module without registering it in `sys.modules`, and `dataclasses` on 3.14
# dereferences `sys.modules[cls.__module__]` while building the class.

class PullRequest:
    __slots__ = ("number", "branch", "title", "states", "tally_marker",
                 "mergeable", "merge_state", "draft", "lanes", "red_ref")

    def __init__(self, number, branch, title, states, tally_marker="",
                 mergeable="", merge_state="", draft=False, lanes=None,
                 red_ref=None):
        self.number = number
        self.branch = branch
        self.title = title
        #: `None` means the rollup never came back. `[]` means it came back
        #: empty. Collapsing those two is the defect this op is built against.
        self.states = states
        self.tally_marker = tally_marker
        self.mergeable = mergeable
        self.merge_state = merge_state
        self.draft = draft
        self.lanes = list(lanes or [])
        #: `(kind, id)` of a red leg, so `next:` can name the log to read.
        self.red_ref = red_ref


class Worktree:
    __slots__ = ("path", "branch", "state", "lanes")

    def __init__(self, path, branch, state, lanes=None):
        self.path = path
        self.branch = branch
        self.state = state
        self.lanes = list(lanes or [])


class Section:
    """One block of the render, which prints whether or not it has data.

    `error` is the whole point: a section holding neither lines nor an error
    would be indistinguishable from one that looked and found nothing.
    """

    __slots__ = ("name", "lines", "error", "warning")

    def __init__(self, name, lines=None, error="", warning=""):
        self.name = name
        self.lines = list(lines or [])
        self.error = error
        #: Rendered, but from inputs that did not all arrive. Counted apart from
        #: `unread` in the footer, because "0 sections unread" beside a section
        #: built on a failed read is the same sentence as an omitted section.
        self.warning = warning

    @property
    def unread(self) -> bool:
        return bool(self.error)


class Report:
    __slots__ = ("repo", "sections", "prs", "lanes")

    def __init__(self, repo, sections=None, prs=None, lanes=None):
        self.repo = repo
        self.sections = dict(sections or {})
        self.prs = list(prs or [])
        #: `None` when the lane universe could not be read.
        self.lanes = lanes


# ── the verdict ──────────────────────────────────────────────────────────

def pr_verdict(pr: PullRequest) -> tuple:
    """`(word, why)` — and every ambiguity resolves to `UNKNOWN`.

    Ordered the way a reader acts: what was not read beats what failed, which
    beats what cannot merge, which beats what is still moving. `MERGE` is the
    last branch and the only one that asserts anything, so it is reachable only
    after every doubt above it has been excluded.
    """
    if pr.states is None:
        return (UNKNOWN, "the check rollup did not come back — nothing about "
                         "this PR's CI is established, so it is not a merge signal")

    if pr.tally_marker:
        return (UNKNOWN, f"the tally does not sum — {pr.tally_marker}. The legs "
                         "that were read say nothing about the ones that were not")

    if not pr.states:
        return (UNKNOWN, _checks.NO_CHECKS + " — nothing has passed, so this is "
                                             "not green, it is unestablished")

    buckets = [_checks.bucket(s) for s in pr.states]

    if "failed" in buckets:
        n = buckets.count("failed")
        return (RED, f"{n} of {len(buckets)} legs failed")

    others = [_checks.normalize(s) for s, b in zip(pr.states, buckets)
              if b == "other"]
    if others:
        named = ", ".join(f"{c} {_checks.label(s)}" for s, c in
                          sorted({s: others.count(s) for s in others}.items()))
        return (UNKNOWN, f"{named} — neither a pass nor a failure, so whether "
                         "this commit is covered is UNKNOWN")

    if "pending" in buckets:
        n = buckets.count("pending")
        return (WAITING, f"{n} of {len(buckets)} legs still moving")

    # Every leg passed. Nothing below can produce a MERGE that the tally has
    # not already earned; it can only take one away.
    if not _checks.all_green(pr.states):  # pragma: no cover - belt and braces
        return (UNKNOWN, "the tally is green by count but not by identity")

    if pr.draft:
        return (DRAFT, "green, but the PR is a draft — not offered for merge")

    mergeable = _checks.normalize(pr.mergeable)
    state = _checks.normalize(pr.merge_state)

    if mergeable == "CONFLICTING" or state == "DIRTY":
        return (REBASE, "green, but the branch conflicts with the base")
    if state == "BEHIND":
        return (REBASE, "green, but the branch is behind the base")
    if mergeable == "MERGEABLE" and state in ("CLEAN", "HAS_HOOKS"):
        return (MERGE, f"{len(pr.states)} of {len(pr.states)} legs passed, "
                       "mergeable, no conflicts")
    if state == "BLOCKED":
        return (WAITING, "green and conflict-free, but GitHub reports the merge "
                         "BLOCKED — a required review or ruleset is outstanding")
    return (UNKNOWN, f"green, but mergeability is {mergeable}/{state} — not a "
                     "state this op will read as mergeable")


# ── lane occupancy ───────────────────────────────────────────────────────

def stray_worktrees(worktrees, default_branch: str = "") -> list:
    """Live worktrees that could not be placed in any lane.

    The set that denies `free` to every lane, named once by the render rather
    than repeated under each one — the same finding printed seven times is a
    wall a reader skips, and a skipped disclosure is an absent one.
    """
    return [w for w in worktrees
            if w.state in LIVE_WORKTREE_STATES and not w.lanes
            and not (default_branch and w.branch == default_branch)]


def lane_states(lanes, prs, worktrees, default_branch: str = ""):
    """`{lane: (state, evidence)}`, or `None` when the lane universe is unread.

    `None` in and `None` out: a lane board built from the labels that happened
    to be reachable would report every unseen lane as free, which is the
    inference this whole function exists to refuse.

    **The clone on the default branch is excluded from the stray set**, and it
    is the one exclusion here. Lane work happens on a `fix/NNN` branch in its
    own worktree; the main clone sits on `master` permanently and is where the
    symlinked binary lives, so counting it as an unplaced occupancy would deny
    `free` to every lane on every call forever. An alarm that can never clear is
    one nobody reads, which is the same failure as no alarm. Every *other*
    unattributable live tree still denies `free` — including a second worktree
    that merely happens to be on the default branch is not possible here,
    because that is what the path comparison would need and git does not allow
    two worktrees on one branch.
    """
    if lanes is None:
        return None

    stray = stray_worktrees(worktrees, default_branch)

    out = {}
    for lane in lanes:
        hard: list = []
        soft: list = []
        for pr in prs:
            if lane in pr.lanes:
                hard.append(f"#{pr.number} open ({_untrusted.flat(str(pr.branch))})")
        for wt in worktrees:
            if lane not in wt.lanes:
                continue
            where = _untrusted.flat(str(wt.path))
            if wt.state == _worktrees.STATE_OCCUPIED:
                hard.append(f"{where} occupied")
            elif wt.state == _worktrees.STATE_UNKNOWN:
                soft.append(f"{where} cannot tell — undecidable, so this lane "
                            "declines rather than reporting itself free")

        if hard:
            out[lane] = (LANE_OCCUPIED, hard + soft)
        elif soft:
            out[lane] = (LANE_UNKNOWN, soft)
        elif stray:
            out[lane] = (LANE_UNKNOWN, [
                f"nothing points here, but {len(stray)} live worktree(s) could "
                "not be placed in any lane (named above) — one of them could be "
                "this one, so 'free' is not claimed"
            ])
        else:
            out[lane] = (LANE_FREE, ["no open PR and no live worktree points here"])
    return out


# ── `next:` — one opinion, and it reads as one ───────────────────────────

def next_action(report: Report) -> str:
    """The single highest-value action, as a command, or a plain "nothing".

    It is an opinion over the same rows the reader can see above it, so it never
    introduces a fact the board does not already carry — and it declines outright
    when the board is unread, because the most confident wrong sentence this op
    could print is advice derived from data it never got.
    """
    board = report.sections.get("board")
    if board is None or board.unread:
        why = board.error if board is not None else "the board section never ran"
        return (f"next: UNKNOWN — the board could not be read ({why}), so nothing "
                "is claimed about what to do next")

    graded = [(pr, pr_verdict(pr)[0]) for pr in report.prs]

    ready = [pr for pr, word in graded if word == MERGE]
    if len(ready) == 1:
        return (f"next: 1 PR is ready — review and merge it: "
                f"gh-pr:{ready[0].number}:diff")
    if len(ready) > 1:
        numbers = ", ".join(f"#{pr.number}" for pr in ready)
        return (f"next: {len(ready)} PRs are equally ready ({numbers}) — no single "
                "highest-value action; take them in number order, starting with "
                f"gh-pr:{ready[0].number}:diff")

    reds = [pr for pr, word in graded if word == RED]
    if reds:
        pr = reds[0]
        if pr.red_ref and pr.red_ref[0] == "job":
            probe = f"gh-job:{pr.red_ref[1]}:fail"
        elif pr.red_ref and pr.red_ref[0] == "check":
            probe = f"gh-check:{pr.red_ref[1]}"
        else:
            probe = f"gh-pr:{pr.number}"
        return (f"next: nothing is ready to merge; #{pr.number} is red and a red "
                f"blocks its lane — read it: {probe}")

    unsure = [pr for pr, word in graded if word == UNKNOWN]
    if unsure:
        return (f"next: nothing is ready to merge, and #{unsure[0].number} is "
                "UNKNOWN — resolve the doubt before anything else: "
                f"gh-pr:{unsure[0].number}:status")

    free = sorted(lane for lane, (state, _e) in (report.lanes or {}).items()
                  if state == LANE_FREE)
    if free:
        shown = ", ".join(name[len(LANE_PREFIX):] for name in free)
        return (f"next: nothing ready on the board — {len(free)} lane(s) free "
                f"({shown}); pick work for one: gh-issues:label={free[0]}")
    if report.lanes is None:
        return ("next: nothing ready on the board, and the lane board is unread, "
                "so whether a lane is free is UNKNOWN")
    return "next: nothing ready — no PR is mergeable and no lane is free"


# ── render ───────────────────────────────────────────────────────────────

def render(report: Report) -> str:
    """Every section prints. `[result]` is always the last line."""
    out = [f"# dashboard — {_untrusted.flat(str(report.repo))}", ""]

    unread = 0
    degraded = 0
    for name, section in report.sections.items():
        out.append(name)
        if section.unread:
            unread += 1
            out.append(f"  !! unread — {_untrusted.flat(str(section.error))}")
            out.append("  this section is missing, not empty; nothing about it "
                       "is claimed below")
        elif section.warning:
            degraded += 1
            out.append(f"  !! degraded — {_untrusted.flat(str(section.warning))}")
            out.extend(f"  {line}" for line in section.lines)
        elif not section.lines:
            out.append("  (none)")
        else:
            out.extend(f"  {line}" for line in section.lines)
        out.append("")

    out.append(next_action(report))
    out.append("")

    tally = {word: 0 for word in VERDICT_ORDER}
    for pr in report.prs:
        tally[pr_verdict(pr)[0]] = tally.get(pr_verdict(pr)[0], 0) + 1
    board = ", ".join(f"{tally[w]} {w}" for w in VERDICT_ORDER if tally.get(w))

    if report.lanes is None:
        lanes = "lanes UNREAD"
    else:
        counts = {LANE_FREE: 0, LANE_OCCUPIED: 0, LANE_UNKNOWN: 0}
        for state, _evidence in report.lanes.values():
            counts[state] = counts.get(state, 0) + 1
        lanes = (f"{counts[LANE_FREE]} lanes free, {counts[LANE_OCCUPIED]} "
                 f"occupied, {counts[LANE_UNKNOWN]} unknown")

    noun = "section" if unread == 1 else "sections"
    extra = f", {degraded} degraded" if degraded else ""
    out.append(f"[result] dashboard: {board or 'no open PRs'} · {lanes} · "
               f"{unread} {noun} unread{extra}")
    return "\n".join(out)


# ── plumbing ─────────────────────────────────────────────────────────────

def _run(argv: list, timeout: int = 30):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def _json_cmd(argv: list, timeout: int = 30):
    """`(data, error)`. Never `([], "")` for a call that did not answer."""
    try:
        res = _run(argv, timeout=timeout)
    except FileNotFoundError:
        return None, f"{argv[0]} not found on PATH"
    except subprocess.TimeoutExpired:
        return None, f"{' '.join(argv[:3])} timed out after {timeout}s"
    except OSError as exc:
        return None, f"{' '.join(argv[:3])} failed: {exc}"
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        return None, (f"{' '.join(argv[:3])} exited {res.returncode}: "
                      f"{detail[0] if detail else 'no output'}")
    try:
        return json.loads(res.stdout or "null"), ""
    except json.JSONDecodeError:
        return None, f"{' '.join(argv[:3])} returned unparseable JSON"


def _git(args: list, timeout: int = 15):
    """`(stdout, error)` for a read-only git command."""
    try:
        res = _run(["git"] + args, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return "", f"git {' '.join(args[:2])}: {exc}"
    if res.returncode != 0:
        return "", (f"git {' '.join(args[:2])} exited {res.returncode}: "
                    f"{(res.stderr or '').strip().splitlines()[:1] or ['no output']}")
    return res.stdout.strip(), ""


# ── section: local clone ─────────────────────────────────────────────────

def collect_local(default_branch: str) -> Section:
    """Is this clone current, established without writing to it.

    `ls-remote` reads the remote's ref; it does not update ours. That is the
    difference between answering the question and performing half of the fix,
    and the reason this op can be run from a worktree an agent is using.
    """
    head, err = _git(["rev-parse", "--short", "HEAD"])
    if err:
        return Section("local", error=err)
    branch, _e = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    lines = [f"branch {_untrusted.flat(branch or '?')} @ {head}"]

    if not default_branch:
        lines.append("clone currency: UNKNOWN — the repository's default branch "
                     "was not established, so there is nothing to compare against")
        return Section("local", lines)

    ref = f"origin/{default_branch}"
    local_sha, local_err = _git(["rev-parse", ref])
    remote_out, remote_err = _git(["ls-remote", "origin",
                                   f"refs/heads/{default_branch}"])
    remote_sha = remote_out.split()[0] if remote_out.split() else ""

    if local_err or not remote_sha:
        lines.append(f"clone currency: UNKNOWN — {local_err or remote_err or 'the remote ref did not come back'}")
        return Section("local", lines)

    if local_sha == remote_sha:
        lines.append(f"{ref} {local_sha[:7]} — matches the remote, clone is current")
    else:
        lines.append(f"{ref} {local_sha[:7]} is STALE — the remote is at "
                     f"{remote_sha[:7]}. Refresh before acting on anything below")

    counts, count_err = _git(["rev-list", "--left-right", "--count",
                              f"{ref}...HEAD"])
    parts = counts.split()
    if len(parts) == 2:
        lines.append(f"HEAD is {parts[1]} ahead / {parts[0]} behind {ref}")
    else:
        lines.append(f"ahead/behind: UNKNOWN — {count_err or 'unreadable'}")
    return Section("local", lines)


# ── section: default branch ──────────────────────────────────────────────

def collect_default(repo: str, default_branch: str) -> Section:
    """`gh-branch`'s conjunctive verdict, reused rather than re-derived.

    Every piece of judgement here — selection by workflow identity, the four
    states kept apart, the leg reconciliation — is `gh-branch`'s. This function
    only sequences its calls and keeps one line.
    """
    if not default_branch:
        return Section("default", error="the repository's default branch was "
                                        "not established")
    sha, age, err = _gh_branch._head_commit(default_branch)
    if err:
        return Section("default", error=err)

    runs, err = _gh_branch._run_list(default_branch)
    if err:
        return Section("default", error=err)

    selected = _gh_branch.latest_per_workflow(runs, sha)
    if not selected:
        state, sentence = _gh_branch.no_run_verdict(sha, age)
        return Section("default", [f"{default_branch} {sha[:7]}", sentence])

    fetched = {wf: _gh_branch._jobs_for(_gh_branch._run_id(run))
               for wf, run in selected.items()}
    legs = {wf: (None if jobs is None
                 else [_checks.github_state(j) for j in jobs])
            for wf, jobs in fetched.items()}
    marker, detail = _gh_branch._reconcile(repo, selected, fetched)
    _prev_sha, prev_names = _gh_branch.previous_head(runs, sha)
    missing = sorted(prev_names - set(selected)) if prev_names else []
    state, sentence = _gh_branch.verdict(selected, legs, missing, sha, age,
                                         unreconciled=marker)

    read = [s for group in legs.values() if group is not None for s in group]
    lines = [f"{default_branch} {sha[:7]} — {sentence}",
             f"legs: {_checks.summarize(read)}"]
    if marker:
        lines.append(marker)
        lines.extend(line.strip() for line in detail)
    return Section("default", lines)


# ── section: board ───────────────────────────────────────────────────────

_PR_FIELDS = ("number,headRefName,title,url,headRefOid,mergeable,"
              "mergeStateStatus,isDraft,statusCheckRollup,body,labels")


def _red_ref(rollup) -> object:
    """The namespaced id of a red leg, so `next:` can name the log to read."""
    for _name, state, kind, ident in _checks.github_named_states(rollup):
        if _checks.bucket(state) == "failed" and kind and ident:
            return (kind, ident)
    return None


def _build_pr(payload: dict, issue_lanes: dict) -> PullRequest:
    rollup = payload.get("statusCheckRollup")
    states = _checks.github_states(rollup) if isinstance(rollup, list) else None
    marker, _lines = _gh_pr._reconcile_checks(payload)

    lanes = {label.get("name") for label in (payload.get("labels") or [])
             if isinstance(label, dict)
             and str(label.get("name") or "").startswith(LANE_PREFIX)}
    for ref in _checks.closing_issue_refs(payload.get("body")):
        if ref.startswith("#"):
            lanes.update(issue_lanes.get(ref[1:], ()))
    lanes.update(issue_lanes.get(str(payload.get("number")), ()))

    return PullRequest(
        number=payload.get("number"),
        branch=str(payload.get("headRefName") or "?"),
        title=str(payload.get("title") or ""),
        states=states,
        tally_marker=marker,
        mergeable=str(payload.get("mergeable") or ""),
        merge_state=str(payload.get("mergeStateStatus") or ""),
        draft=bool(payload.get("isDraft")),
        lanes=sorted(lanes),
        red_ref=_red_ref(rollup),
    )


def collect_board(issue_lanes: dict, workers: int):
    """`(Section, prs)` — the open PRs, each with its verdict derived.

    The per-PR reconciliation is the fan-out: one run list plus up to four job
    lists each. It runs concurrently because the answer is a join and a serial
    join is six sequential round trips wearing one command's clothes.
    """
    data, err = _json_cmd(["gh", "pr", "list", "--state", "open", "--limit",
                           "50", "--json", _PR_FIELDS], timeout=60)
    if err:
        return Section("board", error=err), []
    if not isinstance(data, list):
        return Section("board", error="gh pr list did not return a list"), []
    if not data:
        return Section("board", ["no open PRs"]), []

    with _futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        prs = list(pool.map(lambda d: _build_pr(d, issue_lanes), data))

    prs.sort(key=lambda p: (VERDICT_ORDER.index(pr_verdict(p)[0]),
                            -int(p.number or 0)))
    lines = []
    for pr in prs:
        word, why = pr_verdict(pr)
        lanes = " ".join(name[len(LANE_PREFIX):] for name in pr.lanes) or "lane ?"
        lines.append(f"{word:<8} #{pr.number:<5} "
                     f"{_untrusted.flat(pr.branch):<20} {lanes:<14} {why}")
    return Section("board", lines), prs


# ── section: worktrees ───────────────────────────────────────────────────

def _branch_lanes(branch: str, issue_lanes: dict, pr_by_branch: dict) -> list:
    """Lanes a branch belongs to — from its PR, then from its issue numbers.

    The issue-number hop is a *naming convention* (`fix/941`), not data, so it
    is only ever additive: a branch it cannot parse contributes no lane rather
    than a guessed one, and `lane_states` treats that absence as a reason to
    decline `free` rather than as an absence of occupancy.
    """
    lanes = set(pr_by_branch.get(branch, ()))
    for number in _ISSUE_IN_BRANCH.findall(str(branch or "")):
        lanes.update(issue_lanes.get(number, ()))
    return sorted(lanes)


def collect_worktrees(issue_lanes: dict, pr_by_branch: dict):
    """`(Section, worktrees)` — `git-worktrees`' own assessment, one line each.

    The evidence lines are `git-worktrees`' to print; here the verdict word
    carries the three states and the row names the op that expands it. Thirteen
    trees times four evidence lines is a page nobody reads, and an unread
    section is the same as no section.
    """
    listing, err = _git(["worktree", "list", "--porcelain"])
    if err:
        return Section("worktrees", error=err), []

    entries = _worktrees.parse_worktree_list(listing)
    for entry in entries:
        entry["gitdir"] = _worktrees.resolve_gitdir(entry["path"])

    memo: dict = {}
    trees = []
    for entry in entries:
        verdict = _worktrees.assess(
            entry, scan=_worktrees._cwd_scan(entry["path"], memo))
        branch = entry.get("branch") or ("(detached)" if entry.get("detached")
                                         else "?")
        trees.append(Worktree(path=entry.get("path", "?"), branch=branch,
                              state=verdict.state,
                              lanes=_branch_lanes(branch, issue_lanes,
                                                  pr_by_branch)))

    lines = []
    for tree in sorted(trees, key=lambda t: (t.state != _worktrees.STATE_OCCUPIED,
                                             t.path)):
        lanes = " ".join(name[len(LANE_PREFIX):] for name in tree.lanes) or "lane ?"
        lines.append(f"{tree.state:<12} {_untrusted.flat(tree.path):<40} "
                     f"{_untrusted.flat(tree.branch):<22} {lanes}")
    lines.append("'cannot tell' is NOT 'idle' — expand one with "
                 "git-worktrees:<path>")
    return Section("worktrees", lines), trees


# ── section: lanes ───────────────────────────────────────────────────────

def collect_lane_universe():
    """`(lanes, error)` — every `lane:*` label the repository declares.

    Read from the label list rather than from the labels in use, so a lane
    nobody has filed against still appears. Derived from usage it would simply
    not exist, and a lane that does not exist cannot be reported free — which
    sounds safe and is the opposite: it silently shrinks the delegation menu.
    """
    data, err = _json_cmd(["gh", "label", "list", "--limit", "200", "--json",
                           "name"], timeout=30)
    if err:
        return None, err
    if not isinstance(data, list):
        return None, "gh label list did not return a list"
    return sorted(str(item.get("name")) for item in data
                  if isinstance(item, dict)
                  and str(item.get("name") or "").startswith(LANE_PREFIX)), ""


def collect_issue_lanes():
    """`({issue number: [lanes]}, error)` — the label lives on the issue.

    Measured on this repository on 2026-08-07: 7 `lane:*` labels exist, 54 of
    65 open issues carry one, and **no open PR carries one at all**. A lane
    board built from PR labels would therefore have rendered all seven lanes
    free while six PRs and thirteen worktrees were live — the exact wrong
    answer, printed confidently. The PR reaches its lane through its closing
    reference, and a worktree through the issue number in its branch name.
    """
    data, err = _json_cmd(["gh", "issue", "list", "--state", "all", "--limit",
                           "400", "--json", "number,labels"], timeout=60)
    if err:
        return None, err
    if not isinstance(data, list):
        return None, "gh issue list did not return a list"
    out: dict = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        lanes = [str(label.get("name")) for label in (item.get("labels") or [])
                 if isinstance(label, dict)
                 and str(label.get("name") or "").startswith(LANE_PREFIX)]
        if lanes:
            out[str(item.get("number"))] = lanes
    return out, ""


def render_lanes(states, strays=()) -> Section:
    if states is None:
        return Section("lanes", error="the lane label universe could not be read")
    lines = []
    if strays:
        named = ", ".join(_untrusted.flat(f"{w.path} ({w.branch})")
                          for w in strays[:4])
        more = f", +{len(strays) - 4} more" if len(strays) > 4 else ""
        lines.append(f"unplaced: {len(strays)} live worktree(s) belong to no "
                     f"lane — {named}{more}. No lane is reported free while one "
                     "of them could be in it")
    order = {LANE_OCCUPIED: 0, LANE_UNKNOWN: 1, LANE_FREE: 2}
    for lane in sorted(states, key=lambda name: (order[states[name][0]], name)):
        state, evidence = states[lane]
        lines.append(f"{state:<10} {lane[len(LANE_PREFIX):]:<14} "
                     f"{' · '.join(evidence)}")
    lines.append("'unknown' is NOT 'free' — an undecidable or unplaced "
                 "occupancy could be in it")
    return Section("lanes", lines)


# ── main ─────────────────────────────────────────────────────────────────

def _repo_identity():
    name, default, err = _gh_branch._repo_identity()
    return name, default, err


def build_report(budget: int, workers: int) -> Report:
    started = time.monotonic()
    repo, default_branch, repo_err = _repo_identity()

    def _left():
        return max(1, int(budget - (time.monotonic() - started)))

    sections: dict = {}
    with _futures.ThreadPoolExecutor(max_workers=4) as pool:
        f_issues = pool.submit(collect_issue_lanes)
        f_labels = pool.submit(collect_lane_universe)
        f_local = pool.submit(collect_local, default_branch)
        f_default = pool.submit(collect_default, repo, default_branch)

        try:
            issue_lanes, issue_err = f_issues.result(timeout=_left())
        except _futures.TimeoutError:
            issue_lanes, issue_err = None, f"budget of {budget}s ran out"

        board, prs = collect_board(issue_lanes or {}, workers)
        pr_by_branch = {pr.branch: pr.lanes for pr in prs}
        worktrees_section, trees = collect_worktrees(issue_lanes or {},
                                                     pr_by_branch)

        for key, future in (("local", f_local), ("default", f_default)):
            try:
                sections[key] = future.result(timeout=_left())
            except _futures.TimeoutError:
                sections[key] = Section(key, error=f"budget of {budget}s ran out")
            except Exception as exc:  # noqa: BLE001 - a section, not the render
                sections[key] = Section(key, error=f"{type(exc).__name__}: {exc}")

        try:
            lanes, lane_err = f_labels.result(timeout=_left())
        except _futures.TimeoutError:
            lanes, lane_err = None, f"budget of {budget}s ran out"

    sections["board"] = board
    sections["worktrees"] = worktrees_section

    states = lane_states(lanes, prs, trees, default_branch=default_branch)
    if lanes is None:
        lane_section = Section("lanes", error=lane_err or "unread")
    else:
        lane_section = render_lanes(
            states, stray_worktrees(trees, default_branch))
        if issue_lanes is None:
            lane_section.warning = (
                f"the issue labels could not be read ({issue_err}), so no PR or "
                "worktree could be placed in a lane — every lane below is "
                "'unknown' for that reason and not because it was inspected")
    sections["lanes"] = lane_section

    ordered = {name: sections[name] for name in
               ("local", "default", "board", "worktrees", "lanes")
               if name in sections}
    report = Report(repo=repo or "?", sections=ordered, prs=prs, lanes=states)
    if repo_err:
        report.sections["local"] = Section("local", error=repo_err)
    return report


def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    if args:
        print(f"ERROR: refused — dashboard takes no arguments, got {args[0]!r}")
        print("  usage: dashboard   (read-only; no repo target, GitHub only)")
        return 2

    budget = _env_int("SUPERTOOL_DASHBOARD_BUDGET", BUDGET_DEFAULT)
    workers = _env_int("SUPERTOOL_DASHBOARD_WORKERS", WORKERS_DEFAULT)
    report = build_report(budget, workers)
    print(render(report))
    return 0 if not any(s.unread for s in report.sections.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
