#!/usr/bin/env python3
"""GitHub pull request list via gh CLI — triage board.

The gl-mrs twin. `gh pr list` shows a flat table; this op adds the things it
omits but you need next: check rollup (a failure shows the failing check's
*name* — the failure class in one word), approval state, age, diff size,
watch-state cross-reference (which PRs already have a live `watch` poller),
and an actionable footer. The board is the repo's; "mine" is a filter you
write (`author=@me`), not the shape it arrives in.

Unlike GitLab (where pipeline status needed a per-MR fetch), GitHub returns
`statusCheckRollup` + `reviewDecision` straight from `gh pr list --json`, so
the core board costs a single call. The only optional second wave is
unresolved review-thread counts (GraphQL, per PR) — skip it with `nopipe`.

Usage:
    gh-prs                          every open PR on this repo, check-enriched
    gh-prs:author=@me               only mine
    gh-prs:reviewer=@me             where review is requested from me
    gh-prs:author=@me,state=merged  filter composition
    gh-prs:label=bug                reusable beyond watching
    gh-prs:nopipe                   skip review-thread enrichment (faster)
    gh-prs:iids                     number list, `#`-comment notes first
    gh-prs:failed                   only PRs whose checks are failing
    gh-prs:anyauthor                accepted, and now what the bare op does

**The default is the whole repo, and the board says which population it is.**
It used to be `author=@me`. `No PRs match.` under that unstated filter is the
strongest available statement of absence and it was false about the world: on
`claude-remember` it printed over two open PRs, both from outside contributors
(#1071). #1072 made the filter honest; #1207 removed it, because the rows it
dropped — a dependency bump, an outside contributor's PR — are the ones needing
a decision from someone other than their author, and three of them sat unseen
for between five hours and a day behind a disclosure that was read past every
time.

Three states survive the flip, moved onto the filter the caller now writes:
rows found (the footer names the scope), no rows *because the role filter
excluded some* (it says how many, and that bare `gh-prs` shows them), and
genuinely nothing open. The count for the middle state costs one extra
`gh pr list`, fired only over an empty board; a probe that could not run
reports UNKNOWN rather than `excluded none`.

`radar`'s GitHub tier was **not** covered by this for a release, and that was
the bug (#1230): it calls `_build_list_cmd` positionally, and #1207 flipped the
op at its own call sites rather than in the helper. The helper now adds no role
filter at all, so both boards answer over one population.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pr import (  # noqa: E402  (reuse the gh-pr helper and the cap it fetched under)
    _fetch_review_threads_detailed,
    THREADS_PAGE_MAX,
)
import _board  # noqa: E402  (the board layout shared with gl-mrs / radar)
import _filter_tokens  # noqa: E402  (the one tokenizer + refusal, shared with gh-issues / gl-mrs)
import _untrusted  # noqa: E402  (the repo's remote-text convention)
from _env import env_int  # noqa: E402  (the one numeric-knob reader)
import _checks  # noqa: E402  (the one check classifier, shared with gh-pr / gl-mrs)
import _proc  # noqa: E402  (the one liveness probe, shared with watch / gl-mrs)
import _release_gate  # noqa: E402  (the release gate, folded in here — #1405)
import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)

WATCH_SOURCE = "github-pr"
STATE_DIR = "/tmp"
DEFAULT_PER_PAGE = 50
ENRICH_CAP = 40  # never fire more than this many per-PR thread fetches
ENRICH_WORKERS = 8  # parallel thread fetches

# Tokens that are flags, not key=value filters. `anyauthor` suppressed the
# implicit `author=@me` when there was one; since #1207 it names what the bare
# op already does, and is kept because it shipped — before it there was no way
# to ask this op for the
# board of everyone's open PRs at all: `author=` is refused by the shared
# tokenizer for carrying no value, and `label=`/`state=` are not role keys, so
# the default survived every spelling of the question (#1071).
_FLAGS = {"nopipe", "iids", "failed", "anyauthor"}

# Filter keys this op forwards. Anything else is refused rather than dropped:
# `_build_list_cmd` builds its argv from a ladder with no `else`, so
# `milestone=nonexistent` — a key `gh pr list` has no flag for at all — used to
# return every open PR, rendered exactly as though that were one milestone's
# contents (#939). `gh-issues` grew this refusal in #864; the two sibling ops
# then disagreed about whether a typo was an error or a silent full board,
# which is worse than the original bug.
_FILTER_KEYS = {"author", "assignee", "label", "reviewer", "state", "per",
                "merged-since"}

# The keys that do NOT narrow the population: `state` and `merged-since` define
# which population is being asked about, `per` sizes the page. Everything else
# in `_FILTER_KEYS` selects a subset of it.
#
# The narrowing set is the complement, DERIVED rather than listed, because the
# release gate's local-history cross-check is only valid over an unnarrowed
# board — `git log TAG..BRANCH` has every PR in it and a filtered API page does
# not, so a gap between them would be the filter's artefact. A key added to
# `_FILTER_KEYS` and forgotten here would make that cross-check report `RAN and
# AGREED` about two populations that were never comparable: a verdict reached
# without standing, which is this file's own defect class inside the guard
# written against it. Spelling it as a subtraction means a new key cannot be
# forgotten, only decided.
_NON_NARROWING_KEYS = {"state", "per", "merged-since"}
_NARROWING_KEYS = _FILTER_KEYS - _NON_NARROWING_KEYS

# The one value here that is an instant rather than a name. `merged-since=` is
# the boundary vocabulary this op had none of, and its absence is why
# `gh-since-tag` built its own `gh pr list` argv — a third caller of the listing
# that drifted twice already (#1207, #1230, filed as #1411).
#
# **A tag name IS accepted, since #1405** — see `_VALUE_DOMAINS` below. This
# comment used to say the opposite in bold, and both of its arguments were
# real; recording why each stopped holding, because they are the arguments
# anyone will re-derive when they next touch this.
#
# *"Resolving a tag has three outcomes — RESOLVED, AMBIGUOUS, UNRESOLVED — and
# a listing filter has two: apply the token or refuse it."* Answered by not
# collapsing them. AMBIGUOUS and UNRESOLVED both REFUSE and print no board, so
# a filter value is still only applied or refused; what changed is that
# RESOLVED carries its resolution into the render rather than being silent, and
# the two refusals name every candidate. A filter value may be refused, it may
# never be picked between.
#
# *"This op is `repo_target: true` while a tag is a read of the local clone, so
# a tag would resolve against one repository and list from another"* — measured
# then, and it printed one repo's tag beside another repo's merge count. That
# is what `_tag_target_conflict` refuses outright, below. A date under a target
# is untouched and honoured whole; only the tag spelling is refused, because
# only it is local.
#
# The reason a tag had to be accepted at all is the tokeniser, not taste:
# supertool splits an op argument on `:`, so `merged-since=2026-...T18:57:19Z`
# loses its value before any filter is parsed, and the bare date that survives
# is midnight UTC — 75 PRs where the tag's own instant returns 20. A tag name
# contains no `:` and is the only colon-free spelling of a second-precision
# boundary.

# Check conclusions that mean "this PR is red".
_FAIL_CONCLUSIONS = {
    "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE",
}
_FAIL_STATES = {"FAILURE", "ERROR"}

# Fields pulled in the single list call — everything the board needs except
# review threads (not exposed by `gh pr list --json`, fetched in wave 2).
#
# `headRefOid` is not rendered by this op. It is here because the radar
# `gh-prs` tier keys its snapshot on it (#859): a push that lands a new head
# commit re-runs everything, and without the SHA a "failed → failed" delta
# across two different commits reads as *no change*. It also feeds `gh-pr`'s
# declared-leg reconciliation, which resolves runs by head SHA. Free — one more
# field on a response already being fetched.
_LIST_FIELDS = (
    "number,title,state,author,headRefName,headRefOid,baseRefName,labels,"
    "isDraft,mergeable,reviewDecision,statusCheckRollup,additions,deletions,"
    "changedFiles,updatedAt,createdAt,assignees,url"
)


def _get_config() -> dict[str, int]:
    """Read tunable knobs from SUPERTOOL_ env vars (set from .supertool.json).

    SUPERTOOL_ENRICH_WORKERS — parallel review-thread fetches (default 8)
    SUPERTOOL_ENRICH_CAP     — max PRs to thread-enrich (default 40)
    SUPERTOOL_PER_PAGE       — PRs fetched from the list endpoint (default 50)
    """
    # Was a second private copy of gl-mrs's `_int` — same silent tolerate, same
    # silent clamp, duplicated. One reader now, and it says what it could not
    # honour (#654).
    return {
        "enrich_workers": env_int("SUPERTOOL_ENRICH_WORKERS", ENRICH_WORKERS, minimum=1),
        "enrich_cap": env_int("SUPERTOOL_ENRICH_CAP", ENRICH_CAP, minimum=0),
        "per_page": env_int("SUPERTOOL_PER_PAGE", DEFAULT_PER_PAGE, minimum=1),
    }


# state=X maps to a gh list flag value. open is gh's default.
_STATES = {"open", "closed", "merged", "all"}

# Keys whose value this op maps rather than forwards. An unmapped value is the
# same defect on a key that is known: `state=mergd` fails the `val in _STATES`
# test below, emits no `--state`, and the *open* board renders as the merged
# one (#939).
_VALUE_DOMAINS: dict[str, object] = {
    "state": _STATES,
    "per": _filter_tokens.POSITIVE_INT,
    "merged-since": _filter_tokens.ISO_INSTANT_OR_TAG,
}

# `--search merged:>X` cannot match an open PR, so these are the states where
# the boundary can return a row at all.
_MERGE_BEARING_STATES = {"merged", "closed", "all"}


def _state_conflict(filters: dict[str, str]) -> str | None:
    """`merged-since=` under a state that cannot contain a merge — the refusal.

    `gh pr list` defaults to open, and `--search merged:>INSTANT` over open PRs
    returns nothing however the repository looks. Rendering that as `No PRs
    match.` is this repo's defect class exactly: an absence produced by the
    query, read as an absence in the world — and it is the convincing direction,
    because an empty board under a boundary the caller *did* type looks like an
    answer rather than like a mistake.
    """
    if "merged-since" not in filters or not filters.get("merged-since"):
        return None
    state = filters.get("state") or "open"
    if state in _MERGE_BEARING_STATES:
        return None
    return (
        f"ERROR: merged-since= asks about merges and state={state} cannot "
        f"contain one — `gh pr list` would be sent `--search merged:>...` over "
        f"{state} PRs, which matches no row whatever this repository looks "
        f"like. An empty board there is a fact about the query, not about the "
        f"repo, so it is refused rather than printed. Add state=merged (or "
        f"state=closed / state=all)."
    )


class _GatePlan(NamedTuple):
    """The boundary slice, or nothing. `is_tag` decides how much of it runs."""

    value: str
    is_tag: bool


def _gate_plan(filters: dict[str, str]) -> "_GatePlan | None":
    """`None` for the ordinary board — and that is the case that matters.

    `gh-prs` is this repository's most-called op: radar's GitHub tier, every
    triage tick, every agent's first call. `merged-since=` is the only thing
    that makes it read the local clone at all, and `None` here is what
    guarantees a caller who did not ask for a boundary pays for none of it —
    no `git for-each-ref`, no `git log`, no `changelog.d` scan, and the board's
    own field set unchanged.

    That guard is deliberately one line with one input. This op has already
    shipped a narrowing nobody could see (`author=@me`, #1207, and again in
    #1230 when radar inherited it from the shared argv builder), so the
    condition that switches its behaviour is the last place to be clever.
    """
    value = str(filters.get("merged-since") or "")
    if not value:
        return None
    # A tag is anything the one clock cannot read as a date. The shape was
    # already checked by `_bad_values`; this only routes.
    return _GatePlan(value=value,
                     is_tag=_filter_tokens.parse_iso_instant(value) is None)


def _tag_target_conflict(value: str, target: object) -> "str | None":
    """`merged-since=TAG` under `repo:OWNER/NAME` — refused, not half-honoured.

    The deleted op refused a repo target outright and the hazard did not
    disappear when the code moved here, it just landed on the op with the most
    callers. Measured before that refusal existed: one repository's tag as the
    boundary, another's merge count against it, and the first one's fragment
    count, all under a single header.

    A **date** boundary under a target is fine and is not refused — nothing
    local is read for it, so the target is honoured whole.
    """
    plan = _gate_plan({"merged-since": value})
    if plan is None or not plan.is_tag:
        return None
    if not str(target or "").strip():
        return None
    return _release_gate.repo_target_refusal(target, tag=value)


def _boundary_flag_conflict(plan: "_GatePlan | None", flags: set) -> "str | None":
    """`failed` beside a boundary — refused, because the data is not fetched.

    The boundary slice runs on the gate's narrow field set, which carries no
    `statusCheckRollup` on purpose: that is dozens of check runs per row over a
    page of up to 500, and keeping the release gate out of it is #1411's own
    decision. Answering `failed` from a field that was never requested would
    render every row as passing.
    """
    if plan is None or "failed" not in flags:
        return None
    return (
        "ERROR: `failed` and `merged-since=` cannot be answered together. The "
        "boundary slice is fetched without `statusCheckRollup` — dozens of "
        "check runs per row, over a page of up to 500 — because a merged PR's "
        "rollup is historical and the release gate was deliberately kept off "
        "that field set. With the field absent, `failed` would report every "
        "row as passing, so it is refused rather than answered from data that "
        "was never fetched. Drop one of the two."
    )


def _parse_args(arg_str: str) -> tuple[dict[str, str], set[str], list[str]]:
    """Split a comma-separated arg string into (filters, flags, unrecognised).

    Comma-separated so the single supertool arg segment never collides with
    the ':' op tokenizer. 'author=@me,state=merged,nopipe' becomes
    ({'author': '@me', 'state': 'merged'}, {'nopipe'}, []).

    The third return value is the one that matters, and it is why the tokenizer
    now lives in `_filter_tokens` rather than here: a token this op cannot place
    is handed back so `main` can refuse, instead of falling off the end of the
    loop and leaving the caller reading an unnarrowed board as the answer to a
    narrowing question.
    """
    return _filter_tokens.parse(arg_str, _FILTER_KEYS, _FLAGS)


def _unknown_error(unknown: list[str]) -> str:
    """Name every token that was not applied, and what would have been."""
    return _filter_tokens.unknown_error(unknown, _FILTER_KEYS, _FLAGS)


def _bad_values(filters: dict[str, str]) -> list[tuple[str, str, str]]:
    """Known keys carrying a value this op has no mapping for."""
    return _filter_tokens.bad_values(filters, _VALUE_DOMAINS)


def _build_list_cmd(filters: dict[str, str], per_page: int, *,
                    fields: str = _LIST_FIELDS) -> list[str]:
    """Build the `gh pr list ... --json` argv from parsed filters.

    **This is the one place the listing is spelled.** Two callers, and since
    #1405 both are in this file: the board, and the release-gate slice, which
    passes its own `fields` and a `merged-since` boundary. `gh-since-tag` was
    the second caller until #1411 stopped it building its own argv and #1405
    deleted the op entirely — there is no third spelling and there has not been
    one since. The `fields` parameter is keyword-only on purpose: #1230 is what
    a positional parameter on this function costs, and a field set is the
    caller's render input, never a narrowing of the population.

    **No role filter is ever added here.** The argv narrows only by what the
    caller wrote. state=open is gh's default (no flag emitted). reviewer has no
    list flag on gh, so it routes through --search review-requested:USER.

    There used to be an implicit `author=@me` and an `any_author` opt-out
    (#1071). #1207 flipped the op to the whole repo — but by passing
    `any_author=True` at the op's two call sites and leaving the *parameter*
    default `False`. `watch/tiers/gh_prs.py` calls this with two positional
    arguments, so radar's GitHub tier kept the narrow board after the op
    dropped it, and a maintainer tick opened on a board that excluded every
    dependabot and outside-contributor PR while rendering as healthy (#1230).

    So the parameter is gone rather than flipped. A default that no caller in
    the tree wants is not a default, it is the next inheritance of this bug;
    with the narrowing removed there is nothing left here to inherit.
    """
    cmd = (["gh", "pr", "list", "--json", fields, "--limit", str(per_page)]
           + _repo_target.gh_args())
    # Every `--search` qualifier goes into ONE flag. gh binds `--search` to a
    # single string, so a second occurrence replaces the first rather than
    # narrowing further: `reviewer=@me,merged-since=...` emitted two and the
    # board answered only the later half, with no token unapplied and nothing to
    # disclose. There was one search key until #1411 added the second, so this
    # never fired in a shipped release — it is fixed here because adding the
    # second key is what makes it reachable.
    search: list[str] = []
    for key, val in filters.items():
        if not val:
            continue
        if key == "state":
            if val in _STATES and val != "open":
                cmd += ["--state", val]
        elif key == "author":
            cmd += ["--author", val]
        elif key == "assignee":
            cmd += ["--assignee", val]
        elif key == "label":
            cmd += ["--label", val]
        elif key == "reviewer":
            search.append(f"review-requested:{val}")
        elif key == "merged-since":
            # Normalised to one canonical UTC spelling rather than forwarded as
            # typed, so `2026-08-09`, `...Z` and `...+02:00` become the same
            # qualifier. `_bad_values` has already refused anything unparsable;
            # the `is None` arm is for a caller reaching this function directly.
            when = _filter_tokens.parse_iso_instant(val)
            if when is not None:
                search.append(
                    "merged:>" + when.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    if search:
        cmd += ["--search", " ".join(search)]
    return cmd


def _check_failed(c: dict) -> bool:
    """A single rollup entry is red — handles CheckRun and StatusContext shapes.

    Delegates to the shared classifier so the board and the `gh-pr` tally
    cannot disagree about what CANCELLED means (#454). Unrecognised states
    count as red there, which is the safe default for a failing-first board.
    """
    concl = str(c.get("conclusion") or "").upper()
    state = str(c.get("state") or "").upper()
    if concl in _FAIL_CONCLUSIONS or state in _FAIL_STATES:
        return True
    return _checks.is_red(_checks.github_state(c))


def _check_pending(c: dict) -> bool:
    status = str(c.get("status") or "").upper()
    state = str(c.get("state") or "").upper()
    if status in {"IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "REQUESTED"}:
        return True
    if c.get("conclusion") is None and status != "COMPLETED" and state in {"", "PENDING", "EXPECTED"}:
        return True
    return False


def _rollup_state(checks: list[dict]) -> str:
    """Reduce statusCheckRollup to one word: failed / running / success / ''."""
    if not checks:
        return ""
    if any(_check_failed(c) for c in checks):
        return "failed"
    if any(_check_pending(c) for c in checks):
        return "running"
    return "success"


def _failed_check_names(checks: list[dict]) -> list[str]:
    """Names of the failing checks — the failure class without a gh-job call."""
    names = []
    for c in checks:
        if _check_failed(c):
            n = c.get("name") or c.get("context") or "check"
            names.append(str(n))
    return names


def _annotate(prs: list[dict]) -> None:
    """Derive board fields from the free list data (no extra API calls)."""
    for p in prs:
        checks = p.get("statusCheckRollup") or []
        if not isinstance(checks, list):
            checks = []
        p["_checks"] = _rollup_state(checks)
        p["_failed_checks"] = _failed_check_names(checks)
        add = p.get("additions")
        dele = p.get("deletions")
        try:
            p["_changes"] = int(add) + int(dele) if add is not None and dele is not None else None
        except (ValueError, TypeError):
            p["_changes"] = None
        decision = str(p.get("reviewDecision") or "").upper()
        if decision == "APPROVED":
            p["_approved"] = True
        elif decision in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}:
            p["_approved"] = False
        else:
            p["_approved"] = None


def _enrich(prs: list[dict], cap: int = ENRICH_CAP, workers: int = ENRICH_WORKERS) -> None:
    """Wave 2: unresolved review-thread counts, fetched in parallel via GraphQL.

    The one thing `gh pr list --json` can't give us. Capped and skippable
    (`nopipe`) because it costs one call per PR.

    `None` when the call declined, and never `0` (#1445). The old fetcher
    returned `[]` for a rate limit, a 404 and a timeout alike, so a PR whose
    threads could not be read was counted as having none and lost the `threads`
    flag entirely. On a board — the read that decides which PR to open next —
    that does not just omit a number, it removes the marker that would have
    made somebody look.

    `_unresolved_floor` is the same sentence about the *page* rather than the
    call (#1508). The count is taken over `reviewThreads(first:
    THREADS_PAGE_MAX)`, so at the cap it is a floor — and a floor of zero is no
    information at all. GraphQL returns threads in creation order, so the newest
    ones are the ones past page 1: a long-running PR whose page-1 threads are all
    resolved lost the flag by exactly the route #1445 closed. It travels as its
    own key rather than by collapsing a capped zero into the `None` that means
    "declined", so `_unresolved` keeps meaning one thing and a floor above zero
    is not stored as though it were exact.
    """
    targets = prs[:cap]
    if not targets:
        return

    def _one(p: dict) -> tuple[int | None, bool]:
        threads, _ = _fetch_review_threads_detailed(
            p.get("url", ""), p.get("number"))
        if threads is None:
            return (None, False)
        return (sum(1 for t in threads if not t.get("isResolved")),
                len(threads) >= THREADS_PAGE_MAX)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        counts = list(ex.map(_one, targets))
    for p, (n, floor) in zip(targets, counts):
        p["_unresolved"] = n
        p["_unresolved_floor"] = floor


# One shared probe (presets/_proc.py). `os.kill(pid, 0)` used to live here,
# which on Windows terminates the process it was asked about rather than
# reporting on it — a read-only status query must never be able to kill (#429).
_pid_alive = _proc.pid_alive


def _watched_numbers(state_dir: str = STATE_DIR) -> set[str] | None:
    """PR numbers that currently have a live github-pr watch poller.

    Reads PID files written by the watch dispatcher
    (/tmp/supertool-watch-github-pr__{number}.pid). A stale file whose process
    is dead does not count as watched.

    None under a repo target (#673): that filename is keyed by PR number and
    carries no repo, so a live poller for #12 of whatever repo the watcher was
    started in cannot be told apart from #12 of the repo this board is about.
    Returning the set anyway would mark the wrong rows watched; returning an
    empty set would assert that none are. Neither is knowable here, so the
    board is told so and says `?`.
    """
    if _repo_target.target():
        return None
    prefix = f"supertool-watch-{WATCH_SOURCE}__"
    watched: set[str] = set()
    for path in glob.glob(os.path.join(state_dir, f"{prefix}*.pid")):
        name = os.path.basename(path)
        number = name[len(prefix):-len(".pid")]
        try:
            with open(path, encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if _pid_alive(pid):
            watched.add(number)
    return watched


_CHECK_GLYPH = {
    "failed": "✗ failed",
    "running": "● running",
    "success": "✓ ok",
}


def _check_glyph(state: str) -> str:
    if not state:
        return "? none"
    return _CHECK_GLYPH.get(state, state)


def _check_cell(p: dict) -> str:
    """Checks cell — for failures, the failing check name(s) are the class."""
    state = str(p.get("_checks", ""))
    if state == "failed":
        names = p.get("_failed_checks") or []
        if names:
            extra = f" +{len(names) - 1}" if len(names) > 1 else ""
            return f"✗ {names[0]}{extra}"
        return "✗ failed"
    return _check_glyph(state)


def _appr_cell(p: dict) -> str:
    """Approval mark: ✓ approved, · not yet, blank when unknown/none required."""
    approved = p.get("_approved")
    if approved is True:
        return "✓"
    if approved is False:
        return "·"
    return " "


def _age(iso: str) -> str:
    """ISO timestamp → 'Nd'/'Nh'/'Nm'. '' on parse failure, 'now' on skew."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _sort_key(p: dict) -> tuple[int, str]:
    """Failing PRs first, then stalest (oldest updatedAt) within each group."""
    failed = 0 if p.get("_checks") == "failed" else 1
    return (failed, str(p.get("updatedAt", "")))


def _flags(p: dict) -> str:
    flags = []
    if p.get("isDraft"):
        flags.append("draft")
    if p.get("mergeable") == "CONFLICTING":
        flags.append("conflict")
    # Three states, and the middle one is the point: `None` is "enriched, and
    # the thread call declined", which is not the same claim as `0` and not the
    # same claim as never having been enriched (past the cap, or `nopipe`) —
    # that last one leaves the key absent and flags nothing (#1445).
    if "_unresolved" in p:
        if p["_unresolved"] is None:
            flags.append("threads?")
        elif p["_unresolved"]:
            # True at the page cap as well: the count is a floor there, but the
            # flag is a boolean and `at least 1 unresolved` and `1 unresolved`
            # make the same claim. Hedging a number the reply *does* establish
            # is the same defect pointed the other way (#1508).
            flags.append("threads")
        elif p.get("_unresolved_floor"):
            # A zero counted over a full page of THREADS_PAGE_MAX threads. The
            # unresolved ones sit past the page or nowhere, and the reply cannot
            # tell those apart — so the board does not either (#1508).
            flags.append("threads?")
    return f" [{','.join(flags)}]" if flags else ""


TITLE_INDENT = _board.TITLE_INDENT


def _branches(p: dict) -> str:
    """`head -> base`, the field a human acts on (checkout, worktree add).

    Both keys already ship in the single `gh pr list --json` response this op
    parses (`headRefName`/`baseRefName` are in _LIST_FIELDS), so the branch
    pair costs no extra API call.
    """
    return _board.branch_pair(p.get("headRefName"), p.get("baseRefName"))


def _row(p: dict, watched: set[str] | None, suffix: str = "") -> str:
    """One triage row, rendered through the shared board layout so `gh-prs`,
    `gl-mrs` and `radar` cannot drift apart.

    Everything GitHub-specific — the number, the check-rollup cell, the ref
    names — is resolved here; `_board.render_row` only decides the shape.
    """
    chg = p.get("_changes")
    return _board.render_row(
        sigil="#",
        ident=str(p.get("number", "?")),
        watched=None if watched is None else str(p.get("number", "?")) in watched,
        status=_check_cell(p),
        appr=_appr_cell(p),
        age=_age(str(p.get("updatedAt", ""))),
        changes=f"{chg}Δ" if isinstance(chg, int) else "",
        branches=_branches(p),
        flags=_flags(p),
        title=str(p.get("title", "")),
        suffix=suffix,
    )


def _render_table(prs: list[dict], watched: set[str] | None) -> str:
    """Triage table: checks (+ failed check), approval, age, diff size, flags.

    Sorted failing-first then stalest so what needs you is at the top.
    """
    if not prs:
        return "No PRs match."
    return "\n".join(_row(p, watched) for p in sorted(prs, key=_sort_key))


def _floor_note(prs: list[dict]) -> str | None:
    """Which reading `threads?` is, for the rows that hit the thread page cap.

    `threads?` has two producers (#1508) and the row is width-sensitive, so the
    second one is disclosed in a line above the board rather than as a third
    token: a declined call is transient, a full page is a fact about the PR, and
    the reader deciding whether to re-run wants to know which.

    Silent when no row hit the cap — a note that appears on every board is one
    nobody reads by the time it matters.
    """
    n = sum(1 for p in prs if p.get("_unresolved_floor"))
    if not n:
        return None
    return (f"{n} PR(s) filled the {THREADS_PAGE_MAX}-thread fetch page, so "
            f"their thread counts are floors — a `threads?` there is a zero "
            f"that could not be established, not a call that failed")


def _cap_note(per_page: int | None, fetched: int | None) -> str | None:
    """The page boundary, when the fetch came back exactly `--limit` long.

    `gh-prs` had no such disclosure at all, so a board of the first 50 of an
    unknown number rendered as `50 PR(s)` — the population cap of #1067, in the
    op `gh-issues` already discloses from. Measured against the *fetch*, not
    against what survived `failed`: three rows dropped client-side take the
    count under the limit and the notice would vanish from exactly the queries
    asking for completeness.
    """
    if per_page is None or fetched is None or fetched < per_page:
        return None
    return f"capped at --limit {per_page} — more may exist, raise with per=N"


def _probe_population(filters: dict[str, str], per_page: int
                      ) -> tuple[int | None, str | None]:
    """How many rows the same query returns with no author filter.

    Three states, and the `None` is the one that matters: the probe is a second
    `gh pr list`, and a spawn failure is a platform difference rather than a
    fact about the repo — Windows raises `FileNotFoundError [WinError 2]` where
    POSIX may not fail at all (#997). Reporting `excluded none` off a call that
    never ran would reproduce this fix's own defect class inside the fix.

    Fired only when the board came back empty under a role filter. Every role
    key is dropped, not just `author` (#1207): leaving `--search
    review-requested:...` on the probe argv answers the "how many without the
    filter" question from the still-filtered population, and the board then
    reports `excluded none` off a query that never widened — the tool's own
    absence, read as an absence in the world.
    """
    widened = {k: v for k, v in filters.items()
               if k not in ("author", "assignee", "reviewer")}
    cmd = _build_list_cmd(widened, per_page)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                encoding="utf-8", errors="replace")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return None, f"the check itself failed: {exc}"
    if result.returncode != 0:
        return None, f"the check itself failed: {(result.stderr or '').strip()[:100]}"
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "the check returned unreadable JSON"
    if not isinstance(rows, list):
        return None, "the check returned no list"
    return len(rows), None


def _population(filters: dict[str, str]) -> str:
    """The noun phrase for the rows this board is about — `state=`, not "open".

    The scope note said "every author's **open** PRs" whatever `state=` asked
    for, so `gh-prs:state=merged` labelled a merged board as the open one. That
    is a wrong label on the one line whose entire job is saying which population
    is on screen, and `merged-since=` (#1411) is a filter nobody writes without
    `state=merged`, so it went from rarely-seen to the common case.
    """
    state = filters.get("state") or "open"
    if state == "all":
        return "PRs in any state"
    return f"{state} PRs"


def _scope_note(filters: dict[str, str], per_page: int, fetched: int
                ) -> str | None:
    """Which population this board is, and what the filter cost — three states.

    `No PRs match.` / `0 PR(s)` is the strongest available statement of
    absence, and under an unstated filter it is false about the world: on
    `claude-remember` it was printed over two open PRs, both from outside
    contributors — the rows a maintainer board exists to surface (#1071).

    #1072 made the implicit `author=@me` honest. #1207 removed it: the default
    board is the repo, and it says so, because an unlabelled board spells both
    "this is everything" and "nobody said which population this is". The
    exclusion arithmetic did not go away with the default — it moved onto the
    role filter, which is now something the caller typed, and where an empty
    answer still hides a number they will want.
    """
    population = _population(filters)
    role = sorted(k for k in ("author", "assignee", "reviewer") if k in filters)
    if not role:
        return (f"no author filter (default) — every author's {population} on "
                f"this repo; gh-prs:author=@me for yours")
    spelled = ", ".join(f"{k}={filters[k]}" for k in role)
    if fetched:
        return f"{spelled} — one slice of the repo; gh-prs for all of it"
    count, reason = _probe_population(filters, per_page)
    if count is None:
        return (f"{spelled} applied; whether it excluded anything is "
                f"UNKNOWN — {reason}")
    if count:
        return (f"{spelled} excluded {count} {population} — gh-prs to see them")
    return f"{spelled} excluded none — nothing matches either way"


def _footer(prs: list[dict], watched: set[str] | None,
            notes: list[str] | None = None) -> str:
    """Actionable summary: failing, unapproved, and the watch command.

    `watched=None` (a repo target, see `_watched_numbers`) drops the watch
    clause rather than guessing at it. The clause is not decoration: it emits a
    ready-to-run `watch:github-pr:N`, and watch state is keyed by number alone,
    so under a target that command would start polling *this* repo's #N while
    the board it came from was about another. A suggestion that does the wrong
    thing is worse than no suggestion, and going silent about it would leave
    the reader thinking nothing needs watching — so it says which it is.
    """
    failing = [str(p.get("number")) for p in prs if p.get("_checks") == "failed"]
    unapproved = [p for p in prs if p.get("_approved") is False]
    parts = [f"{len(prs)} PR(s)"]
    # Before the counts, because they qualify what the counts are counting.
    parts.extend(notes or [])
    if failing:
        parts.append(f"{len(failing)} failing")
    if unapproved:
        parts.append(f"{len(unapproved)} unapproved")
    if watched is None:
        parts.append("watch state unknown for a repo target (keyed by number "
                     "only) — watch from a clone of that repo")
    else:
        unwatched_fail = [n for n in failing if n not in watched]
        if unwatched_fail:
            parts.append(
                f"{len(unwatched_fail)} unwatched → watch:{WATCH_SOURCE}:{unwatched_fail[0]}"
            )
    return " | ".join(parts)


def _boundary_slice(*, rows, plan, boundary, filters, flags, per_page) -> int:
    """Render the merged slice, and the release gate when the boundary is a tag.

    Not the triage board. A merged PR's check rollup is historical, its
    approval is spent and its `mergeable` is null, so the board's columns are
    noise here — and the field set that fills them is what #1411 kept the
    release gate off. What this renders instead is merge order and the merge
    instant, which the board has never carried.

    The gate itself only runs against a **tag**. Under a date boundary it says
    so in as many words rather than staying silent, because a footer that omits
    a check reads exactly like one where the check passed.
    """
    fetched = len(rows)
    # A role or label filter narrows the API side and not local history, so a
    # gap between them would be the filter's artefact. `assess` declines the
    # cross-check by name rather than reporting agreement it did not measure.
    narrowed_by = sorted(k for k in _NARROWING_KEYS if filters.get(k))

    if boundary is None:
        # A date boundary is a listing, not a verdict — so it keeps the board's
        # own exit code. There is nothing here for a release gate to fail on.
        instant = _filter_tokens.parse_iso_instant(plan.value)
        kept, _undated = _release_gate.filter_merged(rows, instant)
        lines = ([_release_gate.page_note(page=fetched, limit=per_page)]
                 + _release_gate.not_applicable_note())
        if narrowed_by:
            lines.append(f"population: narrowed by {', '.join(narrowed_by)}")
        code = 0
    else:
        kept, lines, code = _release_gate.assess(
            rows=rows, boundary=boundary, per_page=per_page, fetched=fetched,
            narrowed_by=narrowed_by,
            repo_targeted=bool(str(_repo_target.target() or "").strip()))

    if "iids" in flags:
        # Same contract as the board's `iids`: the disclosures the caller did
        # not choose to lose ride along as `#` comments, because a truncated
        # list and a complete one are the same bytes downstream.
        for line in lines:
            print(f"# {line}")
        for row in kept:
            number = row.get("number")
            if number is not None:
                print(number)
        return code

    # Header position as well as footer, for the one disclosure the caller did
    # not ask for. A footer is lost by exactly the consumer that truncates
    # (#633, #635, #657), and a capped page read as a complete one is this
    # op's own defect class. The board does the same thing for the same reason.
    cap = _release_gate.page_note(page=fetched, limit=per_page)
    if "PAGE FULL" in cap:
        print(f"({cap})")
    if boundary is None:
        print("\n".join(_release_gate.merge_order_rows(kept)))
    print("\n".join(lines))
    return code


def main_with_args(arg_str: str) -> int:
    filters, flags, unknown_tokens = _parse_args(arg_str)
    if unknown_tokens:
        print(_unknown_error(unknown_tokens), file=sys.stderr)
        return 1
    bad = _bad_values(filters)
    if bad:
        print(_filter_tokens.value_error(bad), file=sys.stderr)
        return 1
    conflict = _state_conflict(filters)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1

    # The boundary slice — `None` for every ordinary board, which is the path
    # radar and every tick take and the one nothing below may change.
    plan = _gate_plan(filters)
    for refusal in (_tag_target_conflict(filters.get("merged-since", ""),
                                         _repo_target.target()),
                    _boundary_flag_conflict(plan, flags)):
        if refusal:
            print(refusal, file=sys.stderr)
            return 1

    iids_only = "iids" in flags
    failed_only = "failed" in flags
    enrich = "nopipe" not in flags

    # Two boards, not one wider than the other — `anyauthor` asks for
    # everyone's and `author=X` asks for one person's. Applying either
    # silently is the same defect as the undisclosed default (#1071).
    role = sorted(k for k in ("author", "assignee", "reviewer") if k in filters)
    if "anyauthor" in flags and role:
        print(
            f"ERROR: `anyauthor` and the role filter(s) {', '.join(role)} ask "
            f"for different boards — anyauthor means every author, "
            f"{role[0]}=... means one. Refusing rather than picking one. Drop "
            f"whichever you did not mean.",
            file=sys.stderr,
        )
        return 1

    # #1207: the board is the repo unless the caller narrowed it — which is
    # now `_build_list_cmd`'s only behaviour, so nothing is passed to select
    # it. `anyauthor` stays accepted because it was
    # documented and shipped, and a flag that starts refusing is a break for
    # every script that adopted it.
    cfg = _get_config()
    per_page = cfg["per_page"]
    if "per" in filters:
        per_page = int(filters.pop("per"))

    # A tag boundary is resolved to a second-precision instant BEFORE the argv
    # is built, so `_build_list_cmd` still sees only the vocabulary it has
    # always had. Three states, and two of them print no board at all: a filter
    # value may be refused, but it may never be picked between.
    boundary = None
    if plan is not None and plan.is_tag:
        boundary = _release_gate.resolve_boundary(plan.value)
        if boundary.state != _release_gate.BOUNDARY_RESOLVED:
            print(boundary.refusal, file=sys.stderr)
            return 1
        filters["merged-since"] = boundary.stamp

    try:
        result = subprocess.run(
            _build_list_cmd(filters, per_page,
                            fields=(_LIST_FIELDS if plan is None
                                    else _release_gate.PR_LIST_FIELDS)),
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"ERROR: gh pr list failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        err = result.stderr.strip() or "unknown error"
        low = err.lower()
        if "not logged in" in low or "401" in err:
            print("ERROR: gh not authenticated. Run: gh auth login", file=sys.stderr)
        elif ("github host" in low or "not a git repository" in low
                or "git remotes" in low):
            print(_repo_target.no_repo_error("gh-prs"), file=sys.stderr)
        else:
            print(f"ERROR: gh pr list: {err}", file=sys.stderr)
        return 1

    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("ERROR: could not parse gh JSON output", file=sys.stderr)
        return 1
    if not isinstance(prs, list):
        prs = []

    if plan is not None:
        return _boundary_slice(rows=prs, plan=plan, boundary=boundary,
                               filters=filters, flags=flags, per_page=per_page)

    _annotate(prs)
    # What the fetch returned, before any client-side filter narrows it: the
    # cap is a fact about the fetch, and so is the author question.
    fetched = len(prs)
    if failed_only:
        prs = [p for p in prs if p.get("_checks") == "failed"]

    # `notes` is everything the footer states. `absent` is the subset `iids`
    # carries: the rows the caller did **not** choose to lose.
    #
    # A page boundary is nobody's request, and an empty board under the
    # implicit `author=@me` is a claim about the world the caller never made
    # — both belong in a stream a script parses. Two things deliberately do
    # not. The scope note on a *populated* board ("your PRs, not the repo's")
    # is a label, not an absence: the numbers above it are complete for the
    # filter that was applied. And `failed`'s complement is the thing the
    # caller explicitly asked to exclude — reporting it back would annotate
    # every `failed,iids` call with the count of PRs that are fine.
    notes: list[str] = []
    absent: list[str] = []

    def _note(text: str | None, states_an_absence: bool) -> None:
        if not text:
            return
        notes.append(text)
        if states_an_absence:
            absent.append(text)

    _note(_cap_note(per_page, fetched), True)
    # An empty board under a role filter is an absence claim; a populated one,
    # or the unfiltered default, is a scope label.
    _note(_scope_note(filters, per_page, fetched), bool(role) and not fetched)
    if failed_only and fetched > len(prs):
        _note(f"failed excluded {fetched - len(prs)} of {fetched} fetched", False)

    # Bare number list — for piping into the watch supervisor.
    #
    # The disclosures ride along as `#` comments rather than being dropped.
    # `iids` returns before any footer is built, so it is the one shape told
    # nothing — and it is the shape whose output becomes another tool's input,
    # where a truncated list and a complete one are the same bytes.
    #
    # Deliberately NOT stderr, which was the first shape of this fix and was
    # wrong: `_run_custom_op` returns a successful op's stdout and drops its
    # stderr, so a note there is a note nobody receives (#654, and measured
    # again on this branch — the line vanished through the wrapper while the
    # preset printed it correctly when run directly). A `#` prefix is a
    # comment marker every pipe already knows, and it cannot be mistaken for
    # a number; the exit code stays 0.
    if iids_only:
        for note in absent:
            print(f"# {note}")
        for p in prs:
            number = p.get("number")
            if number is not None:
                print(number)
        return 0

    if enrich:
        _enrich(prs, cfg["enrich_cap"], cfg["enrich_workers"])
        if len(prs) > cfg["enrich_cap"]:
            print(f"(review-thread enrichment capped at {cfg['enrich_cap']} PRs)")
        floor_note = _floor_note(prs)
        if floor_note:
            print(f"({floor_note})")

    watched = _watched_numbers()
    if prs:
        # Header as well as footer, for the absences the caller did not ask
        # for. A footer is lost by exactly the consumer that truncates (#633,
        # #635, #657), and this file already prints its review-thread cap in
        # header position — it was carrying both patterns at once.
        #
        # `absent` is the same subset `iids` carries, for the same reason: the
        # scope label on a populated board and `failed`'s complement are not
        # absence claims. Nothing prints when nothing was cut.
        for note in absent:
            print(f"({note})")
        # One disclosure line above the board — see `gl-mrs.main` (#819).
        print(_untrusted.flat_note("PR titles"))
    print(_render_table(prs, watched))
    footer = _footer(prs, watched, notes)
    if footer:
        print(f"\n{footer}")
    return 0


# The colon-free spelling of the one value here that wants a ':'. supertool
# splits an op argument on ':', so `merged-since=2026-08-09T16:07:45Z` arrives
# as three argv entries and the value is gone before any filter is parsed. The
# generic refusal closes with "query it with the backend CLI directly and file
# the gap", which is right when nothing can express the value and wrong here.
_COLON_HINT = (
    "The one value on this op that wants a ':' is the boundary, and it has two "
    "colon-free spellings. `merged-since=TAG` (v0.34.0) is the one that keeps "
    "SECOND precision — the tag's own commit instant. `merged-since=YYYY-MM-DD` "
    "is that day at 00:00:00 UTC, which for a release boundary is wrong by up "
    "to a day in the direction of over-counting: measured against v0.35.0, the "
    "bare date returned 75 PRs where the tag's instant returned 20 (#1405).")


def _extra_segments_error(argv: list[str]) -> str | None:
    """`extra_segments_error` for this op, carrying the colon-free spelling."""
    return _filter_tokens.extra_segments_error(argv, "gh-prs", _COLON_HINT)


def main() -> int:
    extra = _extra_segments_error(sys.argv)
    if extra:
        print(extra, file=sys.stderr)
        return 1
    return main_with_args(sys.argv[1] if len(sys.argv) > 1 else "")


if __name__ == "__main__":
    sys.exit(main())
