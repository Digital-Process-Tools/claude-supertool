#!/usr/bin/env python3
"""gh-prs — the GitHub pull-request board, as a radar tier (#859).

`presets/watch/tiers/` held exactly one tier and it spoke GitLab, so the board
this repository is actually merged from was the one population radar could not
watch. Registered by name like any other tier:

    {"ops": {"radar": {"radar_tiers": {"gh-prs": {}}}}}

Why this is a parallel module and not `gl_mrs` generalised
----------------------------------------------------------

Three of the four things `gl_mrs` does turn out not to transfer, and forcing
one interface over them would have bent GitLab's semantics to fit GitHub's or
the reverse:

  * **there is no discovery feed.** `gitlab-mr-feed` is a watch *source*; there
    is no `github-pr-feed`, so a PR opened after this run is discovered on the
    next radar tick and not before. That is a real difference in coverage, so
    the footer states it (`discovery: radar ticks only`) rather than leaving a
    reader to infer a guarantee this side does not have.
  * **drift has no analogue.** GitLab's drift is `last_event.pipeline_id` vs
    `source_state.pipeline_id`. A GitHub PR has no pipeline id; its identity
    under a re-push is the head SHA, which is a *snapshot* concern here rather
    than an event-vs-state one.
  * **watch state is repo-blind.** `/tmp/supertool-watch-github-pr__{number}.pid`
    carries no repo (#673), which gives this tier a failure mode `gl_mrs` does
    not have — see "The one-filter invariant" below.

What *does* transfer is the snapshot: keeping a previous board keyed by the
population it describes, so a delta cannot lie. That reasoning is not GitLab's,
so it moved to `tiers/_snapshot.py` and both tiers read it. One copy, because a
second copy is how a fixed defect comes back.

The one-filter invariant
------------------------

`gl_mrs` states it as *board, watcher fleet and feed are three views of one
resolved filter*. Here it is two views, not three — and it acquires a clause
GitLab does not need:

    The board and the watcher fleet come from one resolved filter, **and that
    filter must describe one repository**, because watch state is keyed by PR
    number alone.

Under a repo target (`gh-prs` against another repo) a live poller for `#12`
cannot be told apart from `#12` of the repo the watcher was started in. So
coverage is **UNKNOWN**, not zero, and nothing is healed: spawning
`watch:github-pr:N` there would start polling *this* clone's `#N` while the
board it came from is about another repo. Rendering that as `0 watched` would
be a number a reader acts on, and healing on it would be an action taken on a
misidentification.

Never green when it cannot tell
-------------------------------

Every route by which this board could narrow itself says so:

  filter        a token `gh-prs` cannot honour is **refused**, before any call.
                `gh pr list` silently ignores an unrecognised key, so
                `radar:milestone=v19` would otherwise return the whole
                unfiltered board and read as "everything matched" — the #486
                shape, and the reason `gh-prs` itself is named in this file's
                changelog fragment as still carrying it.
  auth / rate   a non-zero `gh pr list` raises `RadarError`. Radar prints it on
                stderr and exits non-zero; nothing is healed and **nothing is
                snapshotted**, because acting on a population we could not read
                is how a cache gets overwritten with a guess.
  empty match   a filter that selected nothing is reported *with its scope*.
                "No open PRs" and "this filter matched nothing" are different
                facts and only one of them is about the world.
  no checks     a PR whose rollup is empty is `unchecked`, never green — the
                run may not exist yet, and "not yet" has rendered as "fine" on
                this board's GitLab twin before (#659).
  short tally   a PR the board calls **green** is the only claim that can be
                wrong in the expensive direction, so green rows — and only
                green rows, capped — are reconciled against the legs their runs
                declare, through `gh-pr`'s own `_reconcile_checks` (#724/#804/
                #837). A shortfall makes the row `[legs UNVERIFIED]` and counts
                as unchecked. The tier consumes that arithmetic; it never
                re-implements it, and it never prints a leg count of its own —
                a tally that looks reconciled and is not is the defect three
                PRs just closed.

The default branch is a member
------------------------------

The case that cost the most was not a PR: `master` sat red after a squash
landed, because a green PR is a statement about its merge base and nothing
watches the default branch afterwards. So it is a row on this board, answered
by composing `gh-branch`'s own four states — `GREEN` / `NOT GREEN` / `NO RUN` /
`UNKNOWN` — rather than a second, weaker verdict written here. `default_branch`
names it; `""` switches it off; absent resolves it from the repo.

Heal versus report
------------------

This tier heals, through radar's `_watch` rather than by calling
`dispatcher.start_poller` itself — so radar owns the death cap and the ledger,
and every slot refused is named by radar on the same run. And because heal
spawns processes, everything read-only is reachable without it: `radar_state()`
answers what this tier knows — scope, repo, snapshot, live pollers — and starts
nothing. `watches` is read-only about the fleet; this is the same guarantee
about the tier.
"""
from __future__ import annotations

import concurrent.futures
import glob
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
_WATCH = _HERE.parent

sys.path.insert(0, str(_WATCH))
import dispatcher  # noqa: E402,F401  (radar_state reads its source registry)
import transport  # noqa: E402

sys.path.insert(0, str(_WATCH.parent))
import _checks  # noqa: E402
import _filter_tokens  # noqa: E402  (the one tokenizer the boards share)
import _repo_target  # noqa: E402
import _untrusted  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prs = _load("radar_github_prs", _WATCH.parent / "github" / "prs.py")
pr = _load("radar_github_pr", _WATCH.parent / "github" / "pr.py")
branch = _load("radar_github_branch", _WATCH.parent / "github" / "branch.py")
snapshot = _load("radar_snapshot", _HERE / "_snapshot.py")

SOURCE = prs.WATCH_SOURCE
SNAPSHOT_PREFIX = "supertool-radar-gh-prs"

# Filter keys `gh-prs._build_list_cmd` can actually put on the command line.
# Anything else is refused rather than dropped — see the module docstring.
KNOWN_FILTERS = {"author", "assignee", "reviewer", "label", "state"}

# Tokens that are flags rather than key=value. `iids` and `failed` are board
# *shapes* the op offers and this tier does not: a radar board that silently
# printed only the failing rows would be the narrowing this file is against.
KNOWN_FLAGS = {"nopipe"}

# How many green PRs are reconciled against their runs' declared legs per tick.
# Each costs one call for the commit's runs plus one per run, and only a green
# needs proving, so the budget is small and its edge is disclosed.
RECONCILE_CAP = 6

# A `running` PR whose reported facts have not moved for this long comes back
# onto the delta board with the reason on the row (#1025). Four hours, because
# the false positive it must clear is a genuinely queued matrix: eight PRs sat
# at "18 passed, 2 pending" for the better part of an hour while the macOS
# runners were starved, and every one of them eventually landed. A threshold
# that names those trains its reader to skim, which costs more than the wedge
# it catches. Raise or lower it per board; 0 turns it off.
STALE_RUNNING_MINUTES = 240

RADAR_OPTIONS = {"quiet_when_healthy", "default_branch", "reconcile_cap",
                 "stale_running_minutes"}

# A healthy PR board still speaks: a board that prints nothing on a quiet day
# is byte-identical to a radar that failed to run.
RADAR_QUIET_DEFAULT = False


class RadarError(RuntimeError):
    """The board could not be built. Never degrade to 'all green'."""


# ---------------------------------------------------------------------------
# the one filter
# ---------------------------------------------------------------------------

def resolve_filter(arg: str = "") -> tuple[dict[str, str], set[str]]:
    """`(filters, flags)` in `gh-prs` vocabulary, or `RadarError`.

    Refusing is the whole point. `gh-prs` used to drop a key it did not
    recognise and run the command without it, so `milestone=v19` returned every
    open PR and the reader believed they filtered. On a triage board that reads
    as "all of these matched", which is an absence produced by the tool rendered
    as a fact about the world.

    The op refuses that itself now (#939) — but this tier still parses against
    its *own* vocabulary, which is deliberately narrower: `iids` and `failed`
    are board shapes `gh-prs` offers and a radar board must not silently take.
    So the check stays here; only the token-splitting is shared, because a
    second hand-rolled scan of the arg string is how the two answers drift.
    """
    arg = (arg or "").strip()
    filters, flags, unknown = _filter_tokens.parse(arg, KNOWN_FILTERS, KNOWN_FLAGS)
    if unknown:
        named = ", ".join(
            f"{t.partition('=')[0]}=" if "=" in t else t for t in unknown
        )
        raise RadarError(
            f"radar: gh-prs tier cannot honour {named!r}. Known filters: "
            f"{', '.join(sorted(KNOWN_FILTERS))}; known flags: "
            f"{', '.join(sorted(KNOWN_FLAGS))}. Refusing rather than running "
            f"the query without it — an ignored filter returns the whole board "
            f"and reads as though everything matched."
        )
    return filters, flags


def filter_string(filters: dict[str, str]) -> str:
    """The filter back in `gh-prs` arg form, in one fixed spelling."""
    return ",".join(f"{k}={v}" for k, v in sorted(filters.items()))


def scope_label(filters: dict[str, str], repo: str) -> str:
    """Named on every board, the default included (#486).

    An unlabelled board spells both "this is the default population" and
    "nobody said which population this is", and the filter does not survive an
    invocation.
    """
    spelled = filter_string(filters)
    if not spelled:
        return f"scope author=@me (default) on {repo}"
    return f"scope {spelled} on {repo}"


def repo_name() -> str:
    """`owner/name` this board is about — the target, or the cwd's clone."""
    target = _repo_target.target()
    if target:
        return str(target)
    try:
        r = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner"],
                           capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return "?"
    if r.returncode != 0:
        return "?"
    try:
        return str(json.loads(r.stdout).get("nameWithOwner") or "?")
    except (json.JSONDecodeError, AttributeError):
        return "?"


# ---------------------------------------------------------------------------
# 1. live truth
# ---------------------------------------------------------------------------

def live_open_prs(filters: dict[str, str]) -> list[dict]:
    """Every open PR the filter describes, annotated. `RadarError`, never [].

    One `gh pr list` call: unlike GitLab, the rollup and the review decision
    ride on the list response, so the board costs one request. Review-thread
    enrichment is deliberately skipped — it is one GraphQL call per PR and the
    `[threads]` flag is not a completeness claim about anything.
    """
    cfg = prs._get_config()
    cmd = prs._build_list_cmd(filters, cfg["per_page"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                encoding="utf-8", errors="replace")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise RadarError(f"gh pr list failed: {exc}") from exc
    if result.returncode != 0:
        err = (result.stderr or "").strip() or "unknown error"
        low = err.lower()
        if "not logged in" in low or "401" in err:
            raise RadarError("gh not authenticated. Run: gh auth login")
        if "rate limit" in low or "403" in err:
            raise RadarError(f"gh refused the query (rate limit or permission): {err}")
        raise RadarError(f"gh pr list: {err}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RadarError("could not parse gh JSON output") from exc
    if not isinstance(data, list):
        raise RadarError("gh returned no PR list")
    prs._annotate(data)
    return data


# ---------------------------------------------------------------------------
# 2. the green claim is the one worth proving
# ---------------------------------------------------------------------------

def _reconcile_one(p: dict) -> tuple[str, list[str]]:
    """`gh-pr`'s reconciliation, not a second copy of it (#724/#804/#837)."""
    return pr._reconcile_checks(p)


def verify_green(open_prs: list[dict], cap: int = RECONCILE_CAP) -> list[str]:
    """Mark green PRs whose rollup cannot be squared with the declared legs.

    Only the greens, because red is already a finding and running is already
    unknown — a doubt attached to either changes no action. Capped, and the cap
    is disclosed when it cuts: a budget that silently stopped checking is the
    same silence one level down.
    """
    greens = [p for p in open_prs if p.get("_checks") == "success"]
    if not greens or cap <= 0:
        return ([f"radar: NOTE — green PRs are not being reconciled against "
                 f"their declared legs (reconcile_cap={cap}); a short rollup "
                 f"is indistinguishable from a complete one here."]
                if greens else [])
    lines: list[str] = []
    for p in greens[:cap]:
        marker, detail = _reconcile_one(p)
        if marker:
            p["_unverified"] = marker
            lines.append(
                f"radar: WARNING — #{p.get('number')} shows every check green, "
                f"but the tally could not be squared with what its runs declare "
                f"({marker}), so whether these are all the legs is UNKNOWN.")
            lines.extend(detail)
    if len(greens) > cap:
        lines.append(
            f"radar: WARNING — {len(greens) - cap} of {len(greens)} green PRs "
            f"were not reconciled against their declared legs (reconcile_cap is "
            f"{cap}): a short rollup among them is indistinguishable from a "
            f"complete one. Raise it in the tier's options.")
        for p in greens[cap:]:
            p["_unverified"] = "not reconciled"
    return lines


def unchecked(open_prs: list[dict]) -> list[str]:
    """PR numbers whose check state this board did not establish.

    Two ways in, one word: no rollup at all, and a green whose legs could not
    be reconciled. Both mean *unknown*, and unknown sorted among the green with
    nothing saying so is the defect (#659) on the other platform.
    """
    return [str(p.get("number")) for p in open_prs
            if not str(p.get("_checks") or "") or p.get("_unverified")]


# ---------------------------------------------------------------------------
# 3. coverage and heal
# ---------------------------------------------------------------------------

def watch_coverage() -> set[str] | None:
    """Numbers with a live `github-pr` poller, or `None` when unknowable.

    `None` under a repo target and it is not a shortcut: the pid filename is
    keyed by PR number with no repo (#673), so a live poller for `#12` of some
    other clone is indistinguishable from this board's `#12`. An empty set
    would assert nobody is watching; the set itself would mark the wrong rows.
    """
    return prs._watched_numbers(transport.STATE_DIR)


def heal(numbers: list[str], watched: set[str] | None,
         watch) -> tuple[list[str], list[str]]:
    """`(healed, uncovered)` — one live poller per open PR.

    Spawning goes through radar's `_watch`, never `dispatcher.start_poller`:
    radar owns the #476 slot claim and the #513 death cap, records every slot,
    and emits the cap warning itself. A tier that spawned directly would be a
    second bound to keep in step with the first.

    `watched is None` heals nothing at all — see `watch_coverage`. A slot
    already alive is neither healed nor uncovered: nothing was spawned here, so
    claiming the action would be false, but the PR *is* covered.
    """
    if watched is None:
        return [], []
    healed: list[str] = []
    uncovered: list[str] = []
    for number in [n for n in numbers if n not in watched]:
        status = watch(SOURCE, number, [])
        if status == "spawned":
            healed.append(number)
        elif status == "alive":
            continue
        else:
            uncovered.append(number)
    return healed, uncovered


# ---------------------------------------------------------------------------
# 4. the default branch — the member that is not a PR
# ---------------------------------------------------------------------------

def default_branch_report(ref: str | None, repo: str) -> tuple[list[str], bool]:
    """`(lines, could_tell)` for the branch nothing else watches.

    Composed from `gh-branch`'s own selection, verdict, reconciliation **and
    scope**, so the answer here and the answer from `gh-branch:master` are the
    same arithmetic rather than two renderings that agree today. `could_tell` is
    False for `UNKNOWN` and for `NO RUN` — neither establishes a green, and
    this tier's `healthy` is a claim about what it could see — and, since
    #1077, for a green whose scope is unresolved: a declared set that could not
    be read, or a push-triggered workflow that produced no run on the head
    commit. Those are greens over a universe of unknown size, which is the same
    claim-about-what-it-could-see failing.

    `ref is None` means "not configured" and resolves the repo's own default
    branch; `""` means the operator switched the member off. Two different
    intentions, and collapsing them would either cost a call nobody asked for
    or drop the row that this whole member exists for.
    """
    if ref is None:
        ref = branch._repo_identity()[1]
    if not ref:
        return [], True

    sha, age, err = branch._head_commit(ref)
    if err:
        return [f"radar: {ref} — {branch.UNKNOWN}: {err} The default branch's "
                f"state is not established; a red master looks exactly like "
                f"this line being absent."], False

    runs, err = branch._run_list(ref)
    if err or runs is None:
        return [f"radar: {ref} — {branch.UNKNOWN}: {err or 'run list unreadable'}"], False

    selected = branch.latest_per_workflow(runs, sha)
    _prev_sha, prev_names = branch.previous_head(runs, sha)
    missing = sorted(prev_names - set(selected))

    fetched: dict = {}
    if selected:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(branch.JOB_WORKERS, len(selected))) as pool:
            fetched = dict(zip(selected, pool.map(
                lambda n: branch._jobs_for(branch._run_id(selected[n])), selected)))
    legs = {name: (None if jobs is None
                   else [_checks.github_state(j) for j in jobs])
            for name, jobs in fetched.items()}

    marker, shortfall = branch._reconcile(repo, selected, fetched)
    # #1077: `scope_for` says a caller that has to remember this will not, and
    # this was the caller that did not. Without it the tier published the same
    # unscoped green #846 exists to stop — on the one board that reports master
    # on every tick.
    scope, scope_lines, unresolved = branch.scope_for(repo, sha, selected)
    state, sentence = branch.verdict(selected, legs, missing, sha, age,
                                     branch._GRACE, marker, scope=scope)

    lines = [f"radar: {ref} @ {sha[:7]} — {sentence}"]
    lines.extend(f"  {line}" for line in shortfall)
    for name in missing:
        lines.append(f"  {_untrusted.flat(name)} ran on the previous head and "
                     f"has no run on {sha[:7]} — that is not 'ran and passed'.")
    lines.extend(f"  {line}" for line in scope_lines)
    # The blank-on-green is what made the disclosure unreachable, and removing
    # it outright would be the opposite mistake: on this repo `slow tests`
    # (schedule) and `changelog` (pull_request) produce no run on any master
    # push, forever, so an unconditional clause is two permanent lines on every
    # tick — the render nobody reads by the time it matters. `unresolved` is
    # the narrower question: a green this tier cannot account for, because a
    # push-triggered workflow produced no run or the declared set could not be
    # read at all. Those it says out loud.
    if state == branch.GREEN and not unresolved:
        lines = []
    # `could_tell`, not just the lines. Radar's `quiet_when_healthy` drops a
    # healthy tier's whole output, so lines emitted under a healthy verdict go
    # nowhere — un-blanking them without moving this would have looked fixed
    # and disclosed nothing to an operator running quiet.
    #
    # Only against the green, deliberately, and for the same reason `verdict()`
    # tests `unreconciled` last: every other state here is a *finding*, and a
    # finding is something this tier could tell. A green is a clearance, and a
    # clearance over a set of unknown size is not one.
    could_tell = (state == branch.NOT_GREEN
                  or (state == branch.GREEN and not unresolved))
    return lines, could_tell


# ---------------------------------------------------------------------------
# 5. snapshot and report
# ---------------------------------------------------------------------------

def snap_entry(p: dict) -> dict[str, Any]:
    """The facts this tier reports about one PR. Delta is computed over these.

    `head_sha` is the load-bearing field and the GitHub-shaped one. A push that
    lands a new head commit re-runs everything; the rollup word can be
    identical either side of it ("failed" before, "failed" after) while the
    board is describing a different commit. GitLab keys this on `pipeline_id`;
    number alone would suppress a rerun as "no change", which on a board a
    maintainer merges from is a missed event rather than a phantom one.
    """
    return {
        "checks": str(p.get("_checks") or ""),
        "head_sha": str(p.get("headRefOid") or ""),
        "draft": bool(p.get("isDraft")),
        "mergeable": str(p.get("mergeable") or ""),
        "review": str(p.get("reviewDecision") or ""),
        "unverified": str(p.get("_unverified") or ""),
    }


def snapshot_key(filters: dict[str, str], repo: str) -> str:
    """Filter *and* repo. The same filter over two repos is two populations,
    and sharing one file would report each one's PRs as the other's churn."""
    return snapshot.key({"repo": repo, "filters": dict(sorted(filters.items()))})


def snapshot_path(filters: dict[str, str], repo: str) -> str:
    return snapshot.path(SNAPSHOT_PREFIX, snapshot_key(filters, repo))


def _marks(p: dict, healed: set[str], uncovered: set[str],
           coverage_known: bool, stale_minutes: float = 0.0) -> str:
    out = []
    if p.get("_unverified"):
        out.append(f"[legs UNVERIFIED: {p['_unverified']}]")
    if stale_minutes:
        out.append(f"[{stale_running_label(stale_minutes)}]")
    number = str(p.get("number", "?"))
    if not coverage_known:
        out.append("[watch?]")
    elif number in healed:
        out.append("[healed]")
    elif number in uncovered:
        out.append("[unwatched]")
    return ("  " + " ".join(out)) if out else ""


def _is_standing_problem(p: dict) -> bool:
    """A current fact, so never delta-suppressed. An unfixed red is not history."""
    return (p.get("_checks") == "failed"
            or p.get("mergeable") == "CONFLICTING"
            or bool(p.get("_unverified"))
            or not str(p.get("_checks") or ""))


def _stale_running(p: dict, previous_entry: Any, threshold: float,
                   now: str | None = None) -> float:
    """Minutes a `running` PR has been unchanged, past `threshold`. Else 0.

    `running` is deliberately *not* a standing problem: a pipeline in progress
    is the ordinary state of a PR that was just pushed, and reprinting it every
    tick is exactly what the delta exists to prevent. It is also the only state
    that can persist indefinitely **while being wrong** — a wedged leg, a
    runner that never picks the job up, a workflow waiting on an approval
    nobody will give. None of those ever changes, so the snapshot never
    mismatches and the row is suppressed on every tick after the first (#1025).

    So the elision is kept and given an expiry, rather than removed. Under the
    threshold this returns 0 and the row stays off the board.

    `None` from `unchanged_minutes` is *unknown*, and unknown is not stale: a
    board that flagged every row it could not date would train its reader to
    skim, which is the failure this whole surface is built against. The unknown
    resolves itself on the next write — see `_snapshot.unchanged_minutes`.
    """
    if threshold <= 0 or str(p.get("_checks") or "") != "running":
        return 0.0
    mins = snapshot.unchanged_minutes(previous_entry, now)
    if mins is None or mins < threshold:
        return 0.0
    return mins


def stale_running_label(minutes: float) -> str:
    """`running 5h unchanged` — the reason a suppressed row came back."""
    if minutes >= 120:
        return f"running {int(minutes // 60)}h unchanged"
    return f"running {int(minutes)}m unchanged"


def _footer(open_prs: list[dict], covered: set[str] | None, healed: list[str],
            uncovered: list[str], gone: int, label: str,
            unchecked_n: int, elided_n: int = 0,
            departed_capped: bool = False) -> str:
    """Tallies over the whole open population, plus what the delta held back.

    `elided_n` is the token that makes the footer checkable against the rows
    above it (#1022): every other count here describes all `len(open_prs)` PRs
    while the board prints only those that moved, so without it a reader has
    `6 open | 4 running` over three rows and no way to tell a suppressed row
    from a merged one.
    """
    counts: dict[str, int] = {}
    for p in open_prs:
        key = str(p.get("_checks") or "none")
        counts[key] = counts.get(key, 0) + 1
    parts = [label, f"{len(open_prs)} open"]
    if elided_n:
        parts.append(f"{elided_n} unchanged not shown")
    if counts.get("failed"):
        parts.append(f"{counts['failed']} failing")
    if counts.get("running"):
        parts.append(f"{counts['running']} running")
    green = counts.get("success", 0) - sum(
        1 for p in open_prs if p.get("_checks") == "success" and p.get("_unverified"))
    if green:
        parts.append(f"{green} green")
    if unchecked_n:
        parts.append(f"{unchecked_n} unchecked")
    if covered is None:
        parts.append("watch coverage UNKNOWN")
    else:
        parts.append(f"{len([p for p in open_prs if str(p.get('number')) in covered])} watched")
    if healed:
        parts.append(f"{len(healed)} healed")
    if uncovered:
        parts.append(f"{len(uncovered)} unwatched")
    if gone:
        # Not "no longer open" (#1024): `open_prs` is filter-scoped, so a PR
        # that was reassigned away is gone from here and still open there. And
        # on a full page not even "left" is established — see `departed_note`.
        parts.append(f"{gone} off this page" if departed_capped
                     else f"{gone} left this board")
    # Stated on every board: this tier has no discovery feed, so a PR opened
    # after this run is not seen until the next tick. An unstated guarantee is
    # one a reader assumes.
    parts.append("discovery: radar ticks only")
    return " | ".join(parts)


def _coverage_warning(covered: set[str] | None) -> list[str]:
    if covered is not None:
        return []
    return ["radar: WARNING — watch coverage is UNKNOWN for this board. Watch "
            "state is keyed by PR number with no repository (#673) and this "
            "board is about a repo target, so a live poller for #N cannot be "
            "told apart from #N of the clone it was started in. Nothing was "
            "healed; run radar from a clone of that repo to get coverage back."]


def _unchecked_warning(numbers: list[str], total: int) -> list[str]:
    """[] when the board saw everything — the absence *is* the positive claim."""
    if not numbers:
        return []
    shown = ", ".join(f"#{n}" for n in numbers[:8])
    if len(numbers) > 8:
        shown += f", +{len(numbers) - 8} more"
    return [f"radar: WARNING — {len(numbers)} of {total} PRs on this board have "
            f"no established check state ({shown}): unknown, not green, so a "
            f"failing one among them is indistinguishable from a passing one "
            f"here."]


def _departed(previous: dict | None, open_prs: list[dict]) -> list[str]:
    """Numbers in the previous snapshot and not in the live population.

    A function rather than two lines inside `render`, because `radar_report`
    needs the same answer for `healthy` and a second derivation is how the two
    come to disagree (#1024).
    """
    prev_entries: dict[str, Any] = (previous or {}).get("prs", {}) or {}
    live = {str(p.get("number")) for p in open_prs}
    return [n for n in prev_entries if n not in live]


def render(open_prs: list[dict], covered: set[str] | None, healed: list[str],
           uncovered: list[str], previous: dict | None, label: str,
           notes: list[str] | None = None,
           page_capped: bool = False,
           now: str | None = None,
           stale_running_minutes: float = STALE_RUNNING_MINUTES) -> list[str]:
    """Full board on cold start; changed + standing-problem rows afterwards.

    Every open PR lands in exactly one of `shown` and `elided`, and both are
    reported (#1022). The partition is the fix: the footer counts the whole
    population and the loop prints a subset, so the two were free to disagree,
    and a board that quietly prints three of six rows is byte-identical to a
    board with three PRs on it.

    The elision itself is kept. A running PR that has not moved since the last
    tick is genuinely no news, and re-printing it every tick is how a board
    trains its reader to skim. What was wrong was the silence, not the choice.
    """
    cold = previous is None
    prev_entries: dict[str, Any] = (previous or {}).get("prs", {}) or {}
    healed_set, uncovered_set = set(healed), set(uncovered)
    coverage_known = covered is not None
    unchecked_numbers = unchecked(open_prs)

    shown = []
    elided: list[str] = []
    for p in sorted(open_prs, key=prs._sort_key):
        number = str(p.get("number", "?"))
        prev_entry = prev_entries.get(number)
        # `facts`, not the raw entry: the entry also carries `_since`, which
        # changes shape between versions and must never read as a move.
        moved = snapshot.facts(prev_entry) != snap_entry(p)
        notable = number in healed_set or number in uncovered_set
        stale = _stale_running(p, prev_entry, stale_running_minutes, now)
        if cold or moved or notable or _is_standing_problem(p) or stale:
            shown.append(prs._row(p, covered,
                                  _marks(p, healed_set, uncovered_set,
                                         coverage_known, stale)))
        else:
            elided.append(number)

    departed = _departed(previous, open_prs)
    footer = _footer(open_prs, covered, healed, uncovered, len(departed), label,
                     len(unchecked_numbers), len(elided), page_capped)

    # Named only when the board is *partial*. A board where everything was
    # elided already says so unambiguously on its `radar: no change` line, and
    # the footer token still carries the arithmetic; listing every number there
    # would print the whole population back on a board whose entire claim is
    # that there is nothing to look at.
    elision = (snapshot.elided_note(elided, len(open_prs), "PRs", "#", "gh-prs")
               if shown else [])

    lines = (_coverage_warning(covered)
             + _unchecked_warning(unchecked_numbers, len(open_prs))
             + elision
             + snapshot.departed_note(departed, "PR", "#", "gh-pr:<number>",
                                      page_capped)
             + list(notes or []))
    if cold:
        lines.append("radar: cold start — no prior snapshot, full board")
    if shown:
        lines.append(_untrusted.flat_note("PR titles"))
        lines.extend(shown)
        lines.append("")
        lines.append(footer)
    elif cold:
        # "No open PRs" would be a claim about the world. What is true is that
        # this filter selected nothing, and the two are only the same sentence
        # when the filter is the whole population.
        lines.append(f"No PRs matched — {label}.")
        lines.append("")
        lines.append(footer)
    elif departed:
        # A departure is a change, and the only one this board can never print
        # as a row — the entry is gone, so there is nothing left to render and
        # every surviving row legitimately elided. Taking the `no change` arm
        # here announces that nothing happened on the one tick where something
        # fell off the board, which is the token a reader skims by (#1024).
        lines.append(f"radar: no rows changed | {footer}")
    else:
        lines.append(f"radar: no change | {footer}")
    return lines


def _no_watch(source: str, scope: str, only: list[str] | None = None) -> str:
    """Fallback `_watch`. "failed", never "alive" — a tier with no spawner
    cannot establish coverage, and saying it did would be the house defect."""
    return "failed"


def radar_report(options: dict | None = None) -> tuple[list[str], bool]:
    """`(lines, healthy)` — the PR board, as radar's tier contract wants it.

    `healthy` means "this tier could tell you the truth", not "no PR is red". A
    board of failing PRs is a healthy report of an unhealthy world; a board
    with unknown coverage, unchecked rows, or a default branch it could not
    read is not.

    Raises `RadarError` when the board could not be built at all. Radar prints
    it on stderr and exits non-zero, deliberately louder than `healthy=False`,
    and nothing is healed or snapshotted on that path.
    """
    options = options or {}
    watch = options.get("_watch") or _no_watch
    filters, _flags = resolve_filter(str(options.get("_arg") or ""))

    repo = repo_name()
    open_prs = live_open_prs(filters)

    cap = options.get("reconcile_cap", RECONCILE_CAP)
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = RECONCILE_CAP
    notes = verify_green(open_prs, cap)

    numbers = [str(p.get("number")) for p in open_prs if p.get("number") is not None]
    watched = watch_coverage()
    healed, uncovered = heal(numbers, watched, watch)
    covered = None if watched is None else watched | set(healed)

    raw_ref = options.get("default_branch")
    branch_lines, branch_ok = default_branch_report(
        None if raw_ref is None else str(raw_ref), repo)

    label = scope_label(filters, repo)
    key = snapshot_key(filters, repo)
    previous = snapshot.read(SNAPSHOT_PREFIX, key, "prs")
    # One page, no pagination loop. A full page means the population may be
    # truncated, and a truncated population cannot establish which of its
    # previous members left (#1024).
    per_page = int(prs._get_config().get("per_page") or 0)
    page_capped = bool(per_page) and len(open_prs) >= per_page
    departed = _departed(previous, open_prs)
    stale_after = options.get("stale_running_minutes", STALE_RUNNING_MINUTES)
    try:
        stale_after = float(stale_after)
    except (TypeError, ValueError):
        stale_after = STALE_RUNNING_MINUTES
    stamped_at = snapshot.now_iso()
    lines = branch_lines + render(open_prs, covered, healed, uncovered,
                                  previous, label, notes, page_capped,
                                  now=stamped_at,
                                  stale_running_minutes=stale_after)
    prev_entries: dict[str, Any] = (previous or {}).get("prs", {}) or {}
    snapshot.write(SNAPSHOT_PREFIX, key,
                   {str(p.get("number")): snapshot.stamp(
                       snap_entry(p), prev_entries.get(str(p.get("number"))),
                       stamped_at)
                    for p in open_prs},
                   "prs")

    # `departed` counts against health because `healthy` has one consumer —
    # `quiet_when_healthy`, which drops this tier's whole output. A
    # departure-only tick is every row elided plus one summary line, so a
    # healthy verdict there suppresses the only notice that something left.
    healthy = bool(branch_ok) and not uncovered and covered is not None \
        and not unchecked(open_prs) and not departed
    return lines, healthy


# ---------------------------------------------------------------------------
# read-only inspection — the `watches` guarantee, for a tier
# ---------------------------------------------------------------------------

def radar_state(options: dict | None = None) -> list[str]:
    """What this tier knows, without spawning or calling anything.

    Inspection and action were fused on this subsystem once and the cost was
    hours of not looking, because looking had side effects. Nothing here forks,
    nothing here reaches the network: it reads the resolved config, the
    snapshot file on disk and the pid files, and says what it cannot answer.
    """
    options = options or {}
    out: list[str] = []
    try:
        filters, flags = resolve_filter(str(options.get("_arg") or ""))
    except RadarError as exc:
        return [f"  filter    : REFUSED — {exc}"]

    target = _repo_target.target()
    repo = str(target) if target else "(the cwd's clone — not resolved here, "\
                                     "that would be a call)"
    out.append(f"  filter    : {filter_string(filters) or 'author=@me (default)'}"
               f"{(' +' + ','.join(sorted(flags))) if flags else ''}")
    out.append(f"  repo      : {repo}")

    raw_ref = options.get("default_branch")
    out.append("  default br: " + ("(resolved at report time)" if raw_ref is None
                                   else (str(raw_ref) or "(off)")))

    path = snapshot_path(filters, str(target) if target else "?")
    if target:
        try:
            with open(path, encoding="utf-8") as f:
                rows = len((json.load(f).get("prs") or {}))
            out.append(f"  snapshot  : {path} — {rows} PR(s)")
        except (OSError, json.JSONDecodeError):
            out.append(f"  snapshot  : {path} — absent (cold start next run)")
    else:
        # Honest about the one thing this view cannot do without a call: the
        # key includes the repo, and resolving the cwd's repo costs a request.
        out.append(f"  snapshot  : under {transport.STATE_DIR}/"
                   f"{SNAPSHOT_PREFIX}.*.snapshot.json — the exact key needs "
                   f"the repo name, which is a call, so it is not resolved here")

    prefix = f"supertool-watch-{SOURCE}__"
    pids = sorted(os.path.basename(p)[len(prefix):-len(".pid")]
                  for p in glob.glob(os.path.join(transport.STATE_DIR,
                                                  f"{prefix}*.pid")))
    if target:
        out.append(f"  pollers   : {len(pids)} pid file(s) — UNKNOWN whether they "
                   f"cover this repo (#673): {', '.join('#' + n for n in pids) or 'none'}")
    else:
        out.append(f"  pollers   : {', '.join('#' + n for n in pids) or 'none'}")
    return out
