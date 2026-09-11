"""gh-branch watcher source (#1953).

No tier watched the repository's default branch -- the object that matters
most after a squash merge and the one a merge queue is actually waiting on --
so a consumer that records "wait until main goes green" had written a
condition nothing on the channel could ever satisfy. `presets/github/branch.py`
(`gh-branch`) already answers that question on demand, and
`presets/watch/tiers/gh_prs.py`'s `default_branch_report` already renders it as
a *pulled* member row on every radar tick. Neither *pushes* an event. This
source does: it reuses the same composition -- `branch._head_commit`,
`branch._run_list`, `branch.runs_on_sha`, `branch.verdict` -- and emits once
per **state transition** rather than once per poll or not at all.

`ctx["id"]` names the branch to watch, e.g. `watch:gh-branch:main`. This
source does not resolve "the repository's default branch" on its own the way
`default_branch_report` does: that resolution happens once, at the moment a
human or `radar` asks, and a poller has no equivalent "ask again" moment to
re-derive it from between polls. Naming the branch explicitly is one
`gh repo view --json defaultBranchRef` away, and it does not silently keep
watching the wrong ref after an operator renames the default branch.

Reuses `branch` (presets/github/branch.py) rather than a fourth copy of its
`gh run list` plumbing -- the same reuse `default_branch_report` already
makes -- so a poller-observed verdict and a hand-run `gh-branch:<ref>` can
never disagree about *how* they got there. Whether they can still disagree
about *when* is #1951's own open question, entirely about GitHub's run-listing
endpoint and not about this composition.

Source plugin contract:
- INTERVAL: int seconds between polls (30s, matching gh-run/github-pr)
- poll(state, ctx) -> (events, new_state)
- is_terminal(state) -> bool  (always False: a branch has no merged/closed
  state to stop watching for)
"""
from __future__ import annotations

import concurrent.futures
import importlib.util
from pathlib import Path

INTERVAL = 30

_GITHUB_DIR = Path(__file__).parents[3] / "github"
_WATCH_DIR = Path(__file__).parents[2]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _GITHUB_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


branch = _load("watch_gh_branch_op", "branch.py")


def _load_watch(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _WATCH_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# `ratelimit` (#2509): tells a rate-limit-shaped `error` string apart from
# any other unreachable reading, and reads the reset time to attach as
# `retry_after` -- shared with every other gh-backed source rather than a
# second copy of the same marker check and `gh api rate_limit` call.
ratelimit = _load_watch("watch_gh_branch_ratelimit", "ratelimit.py")

# The four states this source can report, unchanged from `gh-branch`'s own
# vocabulary. #1953's own requirement is that the not-yet-concluded case must
# not be folded into a naive green/red pair -- `branch.NOT_GREEN` already
# covers both "a leg failed" and "nothing has failed but nothing has
# concluded either", and that distinction lives in the *sentence*, not in a
# state this source would have to invent. Emitting the sentence in the
# payload (below) is how a consumer still gets to tell the two apart.
GREEN = branch.GREEN
NOT_GREEN = branch.NOT_GREEN
NO_RUN = branch.NO_RUN
UNKNOWN = branch.UNKNOWN

# The escalated NO_RUN reading (#2362) -- a genuinely never-ran commit past
# `branch.NO_RUN_STALE_SECS`, distinct from the ordinary "still within the
# creation window" NO_RUN. A separate token from `NO_RUN` itself (never a
# suffixed variant of it), for the same reason #2355's PENDING/FAILED split
# is: a consumer watching for this specific, harder finding needs its own
# event key, not a substring search over `NO_RUN`'s sentence.
NO_RUN_STALE = branch.NO_RUN_STALE

# NOT_GREEN split in two, poller-side only (#2355). `branch.verdict()` keeps
# its own four-state vocabulary unchanged -- `dashboard.py` and
# `default_branch_report` both render it as-is and neither needed this -- but
# a *poller* exists to tell a waiting consumer what changed, and "nothing has
# concluded yet" and "a leg failed" are opposite next actions folded into one
# string. Classified in `poll()` off `_snapshot`'s own `has_failed_leg`
# (`bool(branch._red_workflows(selected, legs))`), the same structural check
# `verdict()` makes before it ever renders a sentence -- never a substring
# search over the rendered sentence itself: a first cut of this fix did
# exactly that (`"did not pass" in sentence`) and a review caught the hole --
# `verdict()`'s pending sentence interpolates a workflow's own `name:` field,
# which GitHub lets a repo author spell however they like, so a workflow
# literally named "did not pass" would have forged a false NOT_GREEN_FAILED
# reading on a genuinely pending commit.
NOT_GREEN_PENDING = f"{NOT_GREEN} (PENDING)"
NOT_GREEN_FAILED = f"{NOT_GREEN} (FAILED)"

LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"

_EVENT_FOR_STATE = {
    GREEN: "went_green",
    NOT_GREEN_PENDING: "went_not_green",
    NOT_GREEN_FAILED: "went_failed",
    NO_RUN: "no_run",
    NO_RUN_STALE: "no_run_stale",
    UNKNOWN: "unknown",
}

# #2436: consecutive raw-empty run-list fetches, on the SAME sha, required
# before the direction guard below even reports an anomaly. #2333 shipped
# this at 1 -- the first empty read after confirmed runs was immediately
# downgraded to `unknown` -- and #2436 found that upstream flakiness on the
# run-list endpoint recurs as independent, isolated single-poll blips: six
# of them in 32 minutes on one unchanged, already-green commit, every one
# recovering on the very next 30s poll (~39s later). A single-shot guard
# re-arms itself the instant a blip recovers, so it announced (and
# un-announced) the identical anomaly six separate times. Raising this to 2
# absorbs an isolated blip -- discarded as if the poll never happened,
# since none of the six ever repeated on a second consecutive poll -- while
# a genuine, non-recovering absence still surfaces, just one poll later
# than before (see the guard's own comment for how the persistence promise
# is kept).
UNKNOWN_CONFIRM_STREAK = 2


def _snapshot(ref: str) -> tuple[str, str, str, str, str, bool]:
    """`(state, sentence, sha, repo, error, has_failed_leg)` for the named ref,
    right now.

    `has_failed_leg` is structural, never text-derived: `bool(_red_workflows(
    selected, legs))`, the same check `verdict()` itself makes before it ever
    builds a sentence. It is meaningful only when `state == branch.NOT_GREEN`
    -- `False` on every other path, including the error paths below, where
    there is no leg data to have an opinion about.

    `repo` is `branch._repo_identity()`'s own `nameWithOwner` -- gh's own
    base-repo resolution, which honours `remote.<name>.gh-resolved` -- the
    same repository `_head_commit`/`_run_list` above already queried. It is
    NOT `transport.repo_slug()`'s answer: that function is forge-agnostic and
    reads the cwd's `git remote` once per poller process, which is right for
    every source that does not otherwise learn the repository it is about,
    and wrong for this one, which already asks `gh` directly. In a fork
    checkout the two can disagree -- `origin` names the fork,
    `gh repo set-default` points `gh` at the parent -- and #1963 was filed on
    an event stamped with the fork's name while every `gh` call it describes
    ran against the parent. The caller carries this value on the event so the
    dispatcher's generic, process-level attribution does not override it.

    `error` is set when this call could not establish anything at all --
    `_head_commit`, `_run_list` or `_repo_identity` itself failing to answer.
    That is this source's `LOOKUP_UNAVAILABLE`, never `branch.UNKNOWN`:
    `branch.UNKNOWN` is a *finding* this composition is equipped to make (an
    unread job list on an otherwise-resolved commit); a `gh` that would not
    answer at all is the branch-tier equivalent of `github-pr`'s `_fetch`
    returning `(None, why)`, and the two must not collapse into the one state
    (#541's argument, one source over -- collapsing an outage into the same
    reading as a finding is the mistake #1953 exists to stop repeating one
    layer in). `_repo_identity` failing belongs in exactly this arm and not
    past it (#1965): a repository this call could not identify must not
    reach `branch.verdict()`, which would happily compute a state -- GREEN
    included -- off an empty `repo`.

    That is not the only route to `UNKNOWN` a caller of this module sees,
    though: `poll()`, below, can itself downgrade a `NO_RUN` this function
    *did* return into `UNKNOWN` when the same sha previously had confirmed
    runs (#2333) -- a second, poll()-level finding this function never
    produces on its own and knows nothing about.
    """
    sha, age, err = branch._head_commit(ref)
    if err:
        return "", "", "", "", err, False
    runs, err = branch._run_list(ref)
    if err or runs is None:
        return ("", "", sha, "", err or "ERROR: gh run list returned nothing readable",
                False)

    selected = branch.runs_on_sha(runs, sha)
    _prev_sha, prev_names = branch.previous_head(runs, sha)
    missing = branch.missing_workflows(prev_names, selected)

    fetched: dict = {}
    if selected:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(branch.JOB_WORKERS, len(selected))) as pool:
            fetched = dict(zip(selected, pool.map(
                lambda n: branch._jobs_for(branch._run_id(selected[n])), selected)))
    legs = {name: (None if jobs is None
                   else [branch._checks.github_state(j) for j in jobs])
            for name, jobs in fetched.items()}

    repo, _default_ref, repo_err = branch._repo_identity()
    if repo_err:
        return "", "", sha, "", repo_err, False
    marker, _shortfall = branch._reconcile(repo, selected, fetched)
    scope, _scope_lines, _unresolved = branch.scope_for(
        repo, sha, selected, age_secs=age, grace=branch._GRACE)
    state, sentence = branch.verdict(selected, legs, missing, sha, age,
                                     branch._GRACE, marker, scope=scope)
    # Structural, not textual (#2355 review finding): `bool(_red_workflows(...))`
    # reads the same `legs`/`selected` data `verdict()` itself reads, never the
    # rendered sentence. A marker-substring scan over the sentence was tried
    # first and misfires on a workflow literally named after the marker text --
    # `_names(moving)`/`_names(missing)` interpolate a workflow's own `name:`
    # field, which GitHub lets a repo author spell however they like, into the
    # *pending* sentences, so a workflow named e.g. "did not pass" would forge
    # a false NOT_GREEN_FAILED reading on a genuinely pending commit. This asks
    # the same question `_red_workflows` already answers for `verdict()`
    # itself, off the structured data, so nothing a workflow's name says can
    # change the answer.
    has_failed_leg = bool(branch._red_workflows(selected, legs))
    return state, sentence, sha, repo, "", has_failed_leg


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    ref = str(ctx["id"])
    branch_state, sentence, sha, repo, error, has_failed_leg = _snapshot(ref)

    if error:
        # Three answers, not two -- same shape as `github-pr`'s `_fetch`
        # failure arm. Said once per outage (edge-triggered on the lookup
        # flag), not once per poll: an alert that repeats every 30s is one
        # people mute, and a muted alert is the original silence by a longer
        # route.
        # #2509: a rate-limit-shaped failure carries `retry_after` so the
        # dispatcher's poll loop can sleep until the token actually resets
        # instead of hitting the same wall on the very next ordinary
        # INTERVAL. Computed on every poll while unreachable, not only the
        # first (state.get("lookup") below only suppresses the *event*, and
        # the dispatcher still needs a fresh reset time on every tick it
        # stays throttled) -- `unreachable_extra` returns {} for anything
        # that is not a rate limit, so a network outage never gets one.
        extra = ratelimit.unreachable_extra(error)
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error,
                     "ref": ref, **extra}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        payload = {
            "ref": ref,
            "error": error,
            "last_known_state": str(state.get("branch_state") or ""),
        }
        if "retry_after" in extra:
            payload["retry_after"] = extra["retry_after"]
        return [{
            "event": "branch_unreachable",
            "payload": payload,
            "notify_title": f"{ref} — cannot tell",
            "notify_message": error,
        }], new_state

    # NOT_GREEN split into two poller states (#2355): "nothing has
    # concluded yet" and "a leg failed" are opposite next actions, and a
    # pending -> failed transition on the SAME commit changed nothing the
    # old bare-state comparison below could see. `has_failed_leg` is
    # `_snapshot`'s own structural answer (`bool(branch._red_workflows(...))`)
    # -- not re-derived from `sentence` here. A first cut of this fix scanned
    # `sentence` for `branch.NOT_GREEN_FAILED_MARKER` and a review caught the
    # hole: the pending sentence interpolates a workflow's own `name:` field
    # (`_names(moving)`/`_names(missing)` in `verdict()`), which GitHub lets a
    # repo author spell however they like, so a workflow literally named
    # "did not pass" would have forged a false NOT_GREEN_FAILED reading on a
    # genuinely pending commit.
    if branch_state == NOT_GREEN:
        branch_state = NOT_GREEN_FAILED if has_failed_leg else NOT_GREEN_PENDING

    prev_state = state.get("branch_state", "")
    prev_sha = str(state.get("sha") or "")
    sha_repeated = bool(sha) and sha == prev_sha
    # A state this composition only reaches with `selected` non-empty
    # (`verdict()` routes to `no_run_verdict` before this module ever sees
    # a state at all when it is empty) -- so GREEN, either NOT_GREEN
    # sub-state and UNKNOWN all mean "some earlier poll saw at least one run
    # on this sha", and only NO_RUN/NO_RUN_STALE/`""` mean it did not (or
    # nothing has polled yet) -- NO_RUN_STALE (#2362) is still the same
    # zero-runs reading, only escalated by age, so it belongs on this side
    # of the split too. Reading this off `prev_state` rather than a separate
    # stored flag means an UNKNOWN produced by the guard below keeps the
    # confirmation live for the next poll for free -- there is nothing extra
    # to carry forward. The bare `NOT_GREEN` stays in this tuple too: a state
    # file written before #2355 shipped still has it, and this line is what
    # keeps that stale value read as "confirmed" rather than as a cold start.
    prev_confirmed_runs = prev_state in (
        GREEN, NOT_GREEN, NOT_GREEN_PENDING, NOT_GREEN_FAILED, UNKNOWN)
    # How many consecutive polls of THIS sha have read raw-empty already --
    # reset the moment the sha changes, so it never leaks across commits.
    prev_no_run_streak = int(state.get("no_run_streak") or 0) if sha_repeated else 0
    # NO_RUN_STALE (#2362) is still a raw-empty listing -- it is the SAME
    # zero-runs read as NO_RUN, only older, so it must feed the same
    # direction-guard and streak bookkeeping below rather than falling
    # outside both.
    raw_is_no_run = branch_state in (NO_RUN, NO_RUN_STALE)

    # Direction guard (#2333, cadence fixed by #2436): runs on a concluded
    # commit do not disappear -- only the read of them can fail. Observed
    # live: `went_green` -> `no_run` -> `went_green`, same SHA, 36 seconds
    # apart, while `gh-branch` run cold seconds after the middle event
    # showed four concluded, all-passing runs on that exact commit. A later
    # poll of the SAME sha claiming zero runs, after this poller already
    # confirmed runs exist on it, is read as UNKNOWN rather than trusted at
    # face value -- the fetch did not answer, not the commit losing its
    # history.
    #
    # Keyed on the sha matching, not on suppressing NO_RUN altogether: a
    # fresh sha that legitimately has zero runs (nothing to regress from)
    # still fires `no_run` for real, which is this repository's own named
    # positive-control requirement (CLAUDE.md) applied to this exact fix.
    # A retry-inside-the-fetch alternative was also on the table (issue
    # #2333, raised again by #2436) and is not taken here: retrying moves
    # the same ambiguity one call earlier without resolving it -- a second
    # empty answer would still need this same judgment call -- while the
    # direction guard is a fact this poller already has for free, having
    # polled before.
    #
    # `confirmed_streak < UNKNOWN_CONFIRM_STREAK` makes the suppression a
    # grace window rather than a single shot (#2436, review finding on the
    # original #2333 fix): a single-shot guard re-arms itself the instant a
    # blip recovers, so an upstream endpoint that flakes in short, isolated,
    # self-recovering bursts -- six of them in 32 minutes, observed live,
    # every one gone by the very next poll -- gets announced and
    # un-announced once per burst. Fewer than `UNKNOWN_CONFIRM_STREAK`
    # consecutive empty reads on the same sha is now discarded as if the
    # poll never happened: `branch_state`/`sentence` are reset to what this
    # poller already reported, so no transition fires and no event reaches
    # the channel.
    #
    # "Runs do not disappear" is still not quite true forever -- GitHub's
    # own run-retention window (as low as 1 day, operator-configured)
    # genuinely purges history off a SHA that once had confirmed runs, and
    # a branch that goes quiet for that long must not read as UNKNOWN on
    # every poll from then on, never again as the true NO_RUN. So the
    # persistence promise from #2333 is kept, just shifted by the grace
    # window: reaching the threshold surfaces `unknown` for the first time;
    # ONE MORE consecutive empty read past that -- `confirmed_streak >
    # UNKNOWN_CONFIRM_STREAK` -- is trusted and surfaces as the real
    # `no_run`, exactly as a second consecutive read did before this fix,
    # only one poll later.
    if raw_is_no_run and sha_repeated and prev_confirmed_runs:
        confirmed_streak = prev_no_run_streak + 1
        if confirmed_streak < UNKNOWN_CONFIRM_STREAK:
            branch_state = prev_state
            sentence = ""
        elif confirmed_streak == UNKNOWN_CONFIRM_STREAK:
            branch_state = UNKNOWN
            sentence = (
                f"{UNKNOWN} — a previous poll confirmed runs on {sha[:7]}; "
                f"the last {UNKNOWN_CONFIRM_STREAK} fetches for the same "
                f"commit came back empty. Runs on a concluded commit do not "
                f"disappear, so this is read as a fetch that did not answer "
                f"rather than the commit losing its run history. Original "
                f"reading: {sentence}")
        # else: confirmed_streak > UNKNOWN_CONFIRM_STREAK -- trust the raw
        # NO_RUN read straight through, surfacing the real `no_run`.

    no_run_streak = (prev_no_run_streak + 1) if (raw_is_no_run and sha_repeated) else 0

    events: list[dict] = []
    # `""` never equals a real state, so this fires on the very first
    # successful poll too -- exactly like `github-pr`'s `checks_state`
    # transition. `first_tick` (added by the dispatcher, not this source)
    # is what tells a consumer that first emission apart from a live change.
    #
    # `sha != prev_sha` is an OR on top of the state comparison, not a
    # replacement for it (#2355). Half of the incident this issue was filed
    # over was a same-category transition -- master moved to a brand-new
    # commit while still reading as "not concluded yet" -- and a consumer
    # holding the previous sentence has no way to learn the subject changed
    # under it: every leg on the new commit is unread, and the old sentence
    # was about a different SHA entirely. This is a deliberate widening of
    # emission volume, decided here rather than inherited silently: a real
    # commit landing on a watched branch is exactly the kind of event a
    # branch watcher exists to report, even when the coarse verdict does not
    # move, and #2355 asks for this choice to be made and stated rather than
    # defaulted into. `sha` is only compared once `error` is empty (above),
    # where `_snapshot` guarantees it is non-empty, so this cannot mistake a
    # lookup failure for a same-state new-sha transition.
    if branch_state != prev_state or sha != prev_sha:
        key = _EVENT_FOR_STATE.get(branch_state, "unknown")
        ev = {
            "event": key,
            "payload": {"ref": ref, "sha": sha, "sentence": sentence},
            "notify_title": f"{ref} — {branch_state.lower()}",
            "notify_message": sentence,
        }
        if repo:
            # #1963: this composition already asked gh which repository
            # `_head_commit`/`_run_list` were run against
            # (`branch._repo_identity()`, above). Carry that answer on the
            # event so the dispatcher's own `transport.repo_slug()` -- a
            # cheaper, git-config-based read that is right for every other
            # source -- does not override it with a different repository's
            # name inside a fork checkout.
            ev["repo"] = repo
        events.append(ev)

    new_state = {
        "branch_state": branch_state,
        "sha": sha,
        "ref": ref,
        "lookup": LOOKUP_OK,
        "no_run_streak": no_run_streak,
    }
    return events, new_state


def is_terminal(state: dict) -> bool:
    """Never. A branch has no merged/closed state to stop watching for."""
    return False
