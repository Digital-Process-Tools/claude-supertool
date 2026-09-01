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
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)

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

#: The lane vocabulary is **configuration, never a literal** (#1007). `lane:`
#: was hardcoded here out of the *title* of #964 — an issue whose whole subject
#: is a colon that appears in no label name — and selected none of this
#: repository's seven `lane-*` labels, so every lane resolved to unplaced and
#: this section reported nothing. There is no prefix that is right everywhere:
#: `claude-supertool` spells lanes `lane-watch`, `claude-remember` spells
#: priorities `priority:high` — same organisation, one repository apart, opposite
#: convention. Radar's property therefore applies (#528): **unconfigured refuses**
#: rather than silently matching nothing, because a prefix that selects zero
#: labels renders byte-identically to a healthy board with no work on it. There
#: is deliberately no default — a default makes this repository green and hands
#: the next one the identical defect, minus the evidence that filed it.
#: `ops.dashboard.lane_prefix` reaches a preset as `SUPERTOOL_<KEY>` — the op
#: runner uppercases the config key alone and does not namespace it by op, the
#: same route `ops.radar.radar_tiers` and `ops.git-diff.red_flags_extra` take.
#: Naming a variable the runner never sets makes a fully configured repository
#: refuse, and every unit test below passes either way, so
#: `test_the_config_key_reaches_the_preset_under_the_name_it_reads` pins it.
LANE_PREFIX_ENV = "SUPERTOOL_LANE_PREFIX"

NO_LANE_PREFIX = (
    "no lane vocabulary configured, so the lane board is not built rather than "
    "built from a guess. Add ops.dashboard.lane_prefix to .supertool.json — "
    'e.g. {"ops": {"dashboard": {"lane_prefix": "lane-"}}} where the labels are '
    "lane-watch, lane-release. There is no default on purpose: a prefix nobody "
    "chose would select zero labels and print as a lane board with nothing on it"
)

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
    __slots__ = ("repo", "sections", "prs", "lanes", "lane_prefix")

    def __init__(self, repo, sections=None, prs=None, lanes=None,
                 lane_prefix=""):
        self.repo = repo
        self.sections = dict(sections or {})
        self.prs = list(prs or [])
        #: `None` when the lane universe could not be read, `{}` when it was
        #: read and came back empty. Those are different states and #1007 is
        #: what happens when they render the same.
        self.lanes = lanes
        #: The configured vocabulary, or `None` when nothing configured one.
        self.lane_prefix = lane_prefix


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

def read_lane_prefix(raw=None):
    """`(prefix, complaint)` from `ops.dashboard.lane_prefix`, which may refuse.

    Nothing here raises and nothing here guesses. Absent or blank yields `None`
    plus the sentence naming the key, and the lanes section prints that as
    `!! unread` — the third state. The alternative, which is what #1007 was, is
    a prefix that matches nothing rendering as a tally of zeroes.
    """
    raw = os.environ.get(LANE_PREFIX_ENV, "") if raw is None else raw
    raw = str(raw).strip()
    if not raw:
        return None, NO_LANE_PREFIX
    return raw, ""


def _lane_stem(prefix: str) -> str:
    """`lane:` and `lane-` both stem to `lane` — the separator is the variable."""
    return re.sub(r"[^0-9A-Za-z]+$", "", str(prefix))


def select_lane_universe(labels, prefix: str):
    """`(lanes, near_misses, error)` — what this prefix selects, and what it nearly did.

    `near_misses` is the field that makes an empty universe actionable instead
    of mysterious: the same stem behind a different separator is the exact shape
    of #1007. It is reported as a suggestion the reader applies, never as a
    fallback the op applies for them — silently trying the other separator would
    rebuild the guess this whole change removes.
    """
    if not isinstance(labels, list):
        return None, [], "gh label list did not return a list"
    names = [str(item.get("name")) for item in labels if isinstance(item, dict)]
    lanes = sorted(name for name in names if name.startswith(prefix))
    stem = _lane_stem(prefix)
    near = sorted(name for name in names
                  if stem and not name.startswith(prefix)
                  and name.startswith(stem) and len(name) > len(stem)
                  and not name[len(stem)].isalnum())
    return lanes, near, ""


def lane_universe_note(prefix: str, lanes, near) -> str:
    """An empty lane universe, said as its own state rather than as `0, 0, 0` (#1007).

    "No label matched my prefix" and "this repository declares no lanes" are
    different worlds — the first is a defect, the second is fine — and
    `0 free, 0 occupied, 0 unknown` is the same sentence for both. This is the
    sentence that would have made #1007 self-reporting on its first run instead
    of needing someone to grep the source.
    """
    if lanes:
        return ""
    if near:
        shown = ", ".join(_untrusted.flat(name) for name in near[:4])
        more = f", +{len(near) - 4} more" if len(near) > 4 else ""
        other = near[0][:len(_lane_stem(prefix)) + 1]
        return (f"no label matches {prefix!r}, so the lane universe is empty and "
                f"no lane can be reported free. {len(near)} label(s) share its "
                f"stem behind a different separator ({shown}{more}) — if the "
                f"vocabulary here is {other!r}, set ops.dashboard.lane_prefix to "
                "it; nothing is assumed on your behalf")
    return (f"no label matches {prefix!r}, and nothing resembling it exists "
            "either. Either this repository declares no lanes — which is fine — "
            "or the prefix belongs to a different repository. Nothing here "
            "distinguishes those, so no lane is reported free")


def render_lane_refusal(complaint: str) -> Section:
    """The unconfigured case, as an unread section rather than an empty one."""
    return Section("lanes", error=complaint)


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

    prefix = report.lane_prefix
    free = sorted(lane for lane, (state, _e) in (report.lanes or {}).items()
                  if state == LANE_FREE)
    if free:
        shown = ", ".join(name[len(prefix or ""):] for name in free)
        return (f"next: nothing ready on the board — {len(free)} lane(s) free "
                f"({shown}); pick work for one: gh-issues:label={free[0]}")
    if prefix is None:
        return ("next: nothing ready on the board, and the lane vocabulary is "
                "unconfigured (ops.dashboard.lane_prefix), so whether a lane is "
                "free is UNKNOWN")
    if report.lanes is None:
        return ("next: nothing ready on the board, and the lane board is unread, "
                "so whether a lane is free is UNKNOWN")
    if not report.lanes:
        return ("next: nothing ready on the board, and the lane universe is "
                f"empty — no label matches {prefix!r}, so no lane can be "
                "reported free")
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

    if report.lane_prefix is None:
        lanes = "lanes UNCONFIGURED (ops.dashboard.lane_prefix)"
    elif report.lanes is None:
        lanes = "lanes UNREAD"
    elif not report.lanes:
        lanes = f"lane universe EMPTY — no label matches {report.lane_prefix!r}"
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
    """`(stdout, error)` for a read-only git command.

    `--no-optional-locks` precedes the subcommand -- a git global flag (#1945,
    the same mechanism as #1944). This function's own contract already says
    "read-only", so the flag matches what it claims: git skips the index
    writeback rather than taking `.git/index.lock`, and a call killed by its
    own timeout below leaves nothing behind.
    """
    try:
        res = _run(["git", "--no-optional-locks"] + args, timeout=timeout)
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

    Every piece of judgement here — selection of every run on the head, the four
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

    selected = _gh_branch.runs_on_sha(runs, sha)
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
    # #1959: fetched once and handed to both `missing_workflows` (so a
    # workflow the trigger set proves could not have run here does not hold
    # this board's own verdict inside the creation window) and `scope_for`
    # below, the same call this board's own "release gate 1" read the
    # contradiction through.
    owner, name = _gh_branch._declared_legs.owner_repo(repo)
    declared_pair = _gh_branch._declared_workflows.declared_at(owner, name, sha)
    # `missing_workflows`, not `prev_names - set(selected)`: the selection is
    # keyed per run since #1640, so a workflow with two runs on the head is in
    # neither key verbatim and the subtraction reported it as absent.
    missing = _gh_branch.missing_workflows(prev_names, selected, declared_pair[0])
    # #846: the scope of the green, not only the green. This section is the
    # board a human reads immediately before tagging a release, and it was
    # printing "every workflow on X concluded and every leg passed" over a
    # commit three of whose four declared workflows had produced no run.
    scope, scope_lines, _unresolved = _gh_branch.scope_for(
        repo, sha, selected, declared_pair=declared_pair,
        age_secs=age, grace=_gh_branch._GRACE)
    state, sentence = _gh_branch.verdict(selected, legs, missing, sha, age,
                                         unreconciled=marker, scope=scope)

    read = [s for group in legs.values() if group is not None for s in group]
    lines = [f"{default_branch} {sha[:7]} — {sentence}",
             f"legs: {_checks.summarize(read)}"]
    if marker:
        lines.append(marker)
        lines.extend(line.strip() for line in detail)
    lines.extend(line.strip() for line in scope_lines)
    return Section("default", lines)


# ── section: board ───────────────────────────────────────────────────────

_PR_FIELDS = ("number,headRefName,title,url,headRefOid,mergeable,"
              "mergeStateStatus,isDraft,statusCheckRollup,body,labels")


def _red_ref(rollup) -> object:
    """The namespaced id of a red leg, so `next:` can name the log to read.

    `github_named_live`, not `github_named_states` (#1804): a leg a later run
    of the same name replaced is not decided by anything anymore, and pointing
    `next:` at its log sends the reader to read a run GitHub itself no longer
    counts.
    """
    for _name, state, kind, ident in _checks.github_named_live(rollup):
        if _checks.bucket(state) == "failed" and kind and ident:
            return (kind, ident)
    return None


def _build_pr(payload: dict, issue_lanes: dict, prefix: str) -> PullRequest:
    rollup = payload.get("statusCheckRollup")
    # `github_live_states`, not `github_states` (#1804): `pr.states` feeds
    # `pr_verdict()` directly, and a check run a later run of the same name
    # replaced is not a live failure — same discriminator #1792 gave the
    # merge gate, so this board and the merge gate cannot disagree about one
    # PR.
    states = (_checks.github_live_states(rollup)
              if isinstance(rollup, list) else None)
    marker, _lines = _gh_pr._reconcile_checks(payload)

    lanes = {label.get("name") for label in (payload.get("labels") or [])
             if isinstance(label, dict)
             and str(label.get("name") or "").startswith(prefix)}
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


def collect_board(issue_lanes: dict, workers: int, prefix: str = ""):
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
        prs = list(pool.map(lambda d: _build_pr(d, issue_lanes, prefix), data))

    prs.sort(key=lambda p: (VERDICT_ORDER.index(pr_verdict(p)[0]),
                            -int(p.number or 0)))
    lines = []
    for pr in prs:
        word, why = pr_verdict(pr)
        lanes = " ".join(name[len(prefix):] for name in pr.lanes) or "lane ?"
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


def collect_worktrees(issue_lanes: dict, pr_by_branch: dict, prefix: str = ""):
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
        lanes = " ".join(name[len(prefix):] for name in tree.lanes) or "lane ?"
        lines.append(f"{tree.state:<12} {_untrusted.flat(tree.path):<40} "
                     f"{_untrusted.flat(tree.branch):<22} {lanes}")
    lines.append("'cannot tell' is NOT 'idle' — expand one with "
                 "git-worktrees:<path>")
    return Section("worktrees", lines), trees


# ── section: lanes ───────────────────────────────────────────────────────

def collect_lane_universe(prefix: str):
    """`(lanes, near_misses, error)` — every lane label the repository declares.

    Read from the label list rather than from the labels in use, so a lane
    nobody has filed against still appears. Derived from usage it would simply
    not exist, and a lane that does not exist cannot be reported free — which
    sounds safe and is the opposite: it silently shrinks the delegation menu.

    The selection itself is `select_lane_universe`, which also reports what the
    prefix nearly matched — see `lane_universe_note` for why an empty answer
    here has to arrive with an explanation attached.
    """
    data, err = _json_cmd(["gh", "label", "list", "--limit", "200", "--json",
                           "name"], timeout=30)
    if err:
        return None, [], err
    return select_lane_universe(data, prefix)


def collect_issue_lanes(prefix: str):
    """`({issue number: [lanes]}, error)` — the label lives on the issue.

    Measured on this repository on 2026-08-07: seven lane labels exist (spelled
    `lane-watch`, `lane-release`, … — dash-separated, which is why the prefix is
    configuration and not a literal, #1007), 54 of 65 open issues carry one, and
    **no open PR carries one at all**. A lane board built from PR labels would
    therefore have rendered all seven lanes free while six PRs and thirteen
    worktrees were live — the exact wrong answer, printed confidently. The PR
    reaches its lane through its closing reference, and a worktree through the
    issue number in its branch name.
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
                 and str(label.get("name") or "").startswith(prefix)]
        if lanes:
            out[str(item.get("number"))] = lanes
    return out, ""


def render_lanes(states, strays=(), prefix: str = "", note: str = "") -> Section:
    if states is None:
        return Section("lanes", error="the lane label universe could not be read")
    if not states:
        # Read, and empty. Degraded rather than `(none)`: an empty section reads
        # as "nothing to report", and #1007 is what that sentence costs when the
        # truth is "nothing was selectable".
        return Section("lanes", lines=[],
                       warning=note or lane_universe_note(prefix, [], []))
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
        lines.append(f"{state:<10} {lane[len(prefix):]:<14} "
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

    # Refused before any label read, not after: an unconfigured prefix has no
    # right answer to fetch, and "matched 0 of 54 labels" is not a finding about
    # this repository.
    prefix, prefix_complaint = read_lane_prefix()

    sections: dict = {}
    with _futures.ThreadPoolExecutor(max_workers=4) as pool:
        f_issues = pool.submit(collect_issue_lanes, prefix) if prefix else None
        f_labels = pool.submit(collect_lane_universe, prefix) if prefix else None
        f_local = pool.submit(collect_local, default_branch)
        f_default = pool.submit(collect_default, repo, default_branch)

        if f_issues is None:
            issue_lanes, issue_err = {}, ""
        else:
            try:
                issue_lanes, issue_err = f_issues.result(timeout=_left())
            except _futures.TimeoutError:
                issue_lanes, issue_err = None, f"budget of {budget}s ran out"

        board, prs = collect_board(issue_lanes or {}, workers, prefix or "")
        pr_by_branch = {pr.branch: pr.lanes for pr in prs}
        worktrees_section, trees = collect_worktrees(issue_lanes or {},
                                                     pr_by_branch, prefix or "")

        for key, future in (("local", f_local), ("default", f_default)):
            try:
                sections[key] = future.result(timeout=_left())
            except _futures.TimeoutError:
                sections[key] = Section(key, error=f"budget of {budget}s ran out")
            except Exception as exc:  # noqa: BLE001 - a section, not the render
                sections[key] = Section(key, error=f"{type(exc).__name__}: {exc}")

        if f_labels is None:
            lanes, near, lane_err = None, [], ""
        else:
            try:
                lanes, near, lane_err = f_labels.result(timeout=_left())
            except _futures.TimeoutError:
                lanes, near, lane_err = None, [], f"budget of {budget}s ran out"

    sections["board"] = board
    sections["worktrees"] = worktrees_section

    states = lane_states(lanes, prs, trees, default_branch=default_branch)
    if prefix is None:
        lane_section = render_lane_refusal(prefix_complaint)
    elif lanes is None:
        lane_section = Section("lanes", error=lane_err or "unread")
    else:
        lane_section = render_lanes(
            states, stray_worktrees(trees, default_branch), prefix=prefix,
            note=lane_universe_note(prefix, lanes, near))
        if issue_lanes is None:
            lane_section.warning = (
                f"the issue labels could not be read ({issue_err}), so no PR or "
                "worktree could be placed in a lane — every lane below is "
                "'unknown' for that reason and not because it was inspected")
    sections["lanes"] = lane_section

    ordered = {name: sections[name] for name in
               ("local", "default", "board", "worktrees", "lanes")
               if name in sections}
    report = Report(repo=repo or "?", sections=ordered, prs=prs, lanes=states,
                    lane_prefix=prefix)
    if repo_err:
        report.sections["local"] = Section("local", error=repo_err)
    return report


def main() -> int:
    use_utf8_stdout()
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
