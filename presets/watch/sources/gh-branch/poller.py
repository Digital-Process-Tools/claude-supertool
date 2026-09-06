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


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _GITHUB_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


branch = _load("watch_gh_branch_op", "branch.py")

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

LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"

_EVENT_FOR_STATE = {
    GREEN: "went_green",
    NOT_GREEN: "went_not_green",
    NO_RUN: "no_run",
    UNKNOWN: "unknown",
}


def _snapshot(ref: str) -> tuple[str, str, str, str, str]:
    """`(state, sentence, sha, repo, error)` for the named ref, right now.

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
    """
    sha, age, err = branch._head_commit(ref)
    if err:
        return "", "", "", "", err
    runs, err = branch._run_list(ref)
    if err or runs is None:
        return "", "", sha, "", err or "ERROR: gh run list returned nothing readable"

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
        return "", "", sha, "", repo_err
    marker, _shortfall = branch._reconcile(repo, selected, fetched)
    scope, _scope_lines, _unresolved = branch.scope_for(
        repo, sha, selected, age_secs=age, grace=branch._GRACE)
    state, sentence = branch.verdict(selected, legs, missing, sha, age,
                                     branch._GRACE, marker, scope=scope)
    return state, sentence, sha, repo, ""


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    ref = str(ctx["id"])
    branch_state, sentence, sha, repo, error = _snapshot(ref)

    if error:
        # Three answers, not two -- same shape as `github-pr`'s `_fetch`
        # failure arm. Said once per outage (edge-triggered on the lookup
        # flag), not once per poll: an alert that repeats every 30s is one
        # people mute, and a muted alert is the original silence by a longer
        # route.
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error,
                     "ref": ref}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        return [{
            "event": "branch_unreachable",
            "payload": {
                "ref": ref,
                "error": error,
                "last_known_state": str(state.get("branch_state") or ""),
            },
            "notify_title": f"{ref} — cannot tell",
            "notify_message": error,
        }], new_state

    prev_state = state.get("branch_state", "")
    prev_sha = str(state.get("sha") or "")
    # A state this composition only reaches with `selected` non-empty
    # (`verdict()` routes to `no_run_verdict` before this module ever sees
    # a state at all when it is empty) -- so GREEN, NOT_GREEN and UNKNOWN
    # all mean "some earlier poll saw at least one run on this sha", and
    # only NO_RUN/`""` mean it did not (or nothing has polled yet). Reading
    # this off `prev_state` rather than a separate stored flag means an
    # UNKNOWN produced by the guard below keeps the confirmation live for
    # the next poll for free -- there is nothing extra to carry forward.
    prev_confirmed_runs = prev_state in (GREEN, NOT_GREEN, UNKNOWN)

    # Direction guard (#2333): runs on a concluded commit do not disappear
    # -- only the read of them can fail. Observed live: `went_green` ->
    # `no_run` -> `went_green`, same SHA, 36 seconds apart, while `gh-branch`
    # run cold seconds after the middle event showed four concluded,
    # all-passing runs on that exact commit. A later poll of the SAME sha
    # claiming zero runs, after this poller already confirmed runs exist on
    # it, is read as UNKNOWN rather than trusted at face value -- the fetch
    # did not answer, not the commit losing its history.
    #
    # Keyed on the sha matching, not on suppressing NO_RUN altogether: a
    # fresh sha that legitimately has zero runs (nothing to regress from)
    # still fires `no_run` for real, which is this repository's own named
    # positive-control requirement (CLAUDE.md) applied to this exact fix.
    # A retry-inside-the-fetch alternative was also on the table (issue
    # #2333) and is not taken here: retrying moves the same ambiguity one
    # call earlier without resolving it -- a second empty answer would still
    # need this same judgment call -- while the direction guard is a fact
    # this poller already has for free, having polled before.
    if branch_state == NO_RUN and sha and sha == prev_sha and prev_confirmed_runs:
        branch_state = UNKNOWN
        sentence = (
            f"{UNKNOWN} — a previous poll confirmed runs on {sha[:7]}; this "
            f"poll's run list came back empty for the same commit. Runs on "
            f"a concluded commit do not disappear, so this is read as a "
            f"fetch that did not answer rather than the commit losing its "
            f"run history. Original reading: {sentence}")

    events: list[dict] = []
    # `""` never equals a real state, so this fires on the very first
    # successful poll too -- exactly like `github-pr`'s `checks_state`
    # transition. `first_tick` (added by the dispatcher, not this source)
    # is what tells a consumer that first emission apart from a live change.
    if branch_state != prev_state:
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
    }
    return events, new_state


def is_terminal(state: dict) -> bool:
    """Never. A branch has no merged/closed state to stop watching for."""
    return False
