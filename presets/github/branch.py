#!/usr/bin/env python3
"""Is this branch green? — answered per workflow, at a named commit (#615).

`gh-pr:N` answers for a pull request. After a squash merge the ref that matters
is the default branch, which has no PR — `gh-pr:master:status` returns *no PR
found for branch 'master'* — so the post-merge check was hand-rolled every time:

    gh run list --branch master --limit 1

**That line cannot answer the question it is asked.** A repo with more than one
`push` workflow has several runs per commit, and `--limit 1` returns whichever
*workflow* started most recently. On 2026-08-05 that was CodeQL, `completed
success`, while the `tests` matrix on the same SHA was still `queued`. The
statement "master is green" was true that morning and its method could not have
told anyone if it hadn't been.

So this op selects runs by **workflow identity**, never by recency, and its
summary is **conjunctive**: green only when every workflow that ran on the head
commit concluded and every leg of every one of them passed. One workflow's
conclusion is never allowed to stand in for the commit's.

Four states, because collapsing any pair of them is this repository's house
defect (`docs/validators.md` §"Declining instead of guessing"):

* ``GREEN`` — every workflow on the head SHA concluded, every leg ``SUCCESS``.
* ``NOT GREEN`` — a finding. Something failed, or something has not finished.
  Both are findings and they are worded differently, because "a leg failed" and
  "a leg has not started" are opposite next actions.
* ``NO RUN`` — nothing exists for this SHA, *with the reason*. Zero renders
  exactly like "not yet" and has been read that way; the grace window
  (`_checks.CHECK_CREATION_GRACE_SECS`, measured in #585) is what separates the
  two, and past it the cause is declined rather than guessed.
* ``UNKNOWN`` — something could not be read. An unread job list is never
  counted as zero passing legs.

The leg arithmetic is `presets/_checks.summarize`, the same module `gh-pr` and
`gh-run` render through — deliberately, because #615's own argument for the op
existing is that reusing one renderer keeps one place where a `CANCELLED` can be
mis-tallied. What is *not* reused is the render: `gh-pr` is handed a flat rollup
on one commit, and this is a set of runs grouped by workflow with a conjunction
over them, which is a different shape and would have arrived as a second data
model inside one function.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _checks  # noqa: E402
import _declared_legs  # noqa: E402  (the second leg count, shared with gh-run / gh-pr)
import _declared_workflows  # noqa: E402  (the second *workflow* count — #846)
import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)
import _untrusted  # noqa: E402  (workflow and job names are remote text — #851)

# The four states. Spelled as constants because the tests, the exit code and
# the header all have to mean the same thing by them, and because `NOT GREEN`
# contains `GREEN` — anything comparing these as substrings cannot tell a
# verdict from its negation.
GREEN = "GREEN"
NOT_GREEN = "NOT GREEN"
NO_RUN = "NO RUN"
UNKNOWN = "UNKNOWN"

# A run's lifecycle phase, in this module's own words. #615 comment 1 is a
# worked case of a bare column (`[time]`) being read as a possible `TIMED_OUT`
# and costing a second call to disambiguate, so every row states its phase in a
# word that cannot be mistaken for a leg state.
PHASE_CONCLUDED = "concluded"
PHASE_RUNNING = "running"
PHASE_UNESTABLISHED = "unestablished"

# GitHub never transitions a run out of `completed` — same reading as
# `gh-run`'s `_TERMINAL_RUN_STATUS`, and right for the same reason: this is a
# run-*lifecycle* field, which has one terminal value GitHub owns, unlike the
# set of leg states, which grows.
_TERMINAL_RUN_STATUS = "completed"

# How far back the run list reaches. Enough to cover the head commit *and* the
# one before it — the previous head is what makes "this workflow ran last time
# and not this time" observable at all — with room for a repo running several
# workflows per push. Not a page of history: nothing older than the previous
# head is read.
RUN_LIST_LIMIT = 60

# Job lists are fetched one call per workflow, in parallel. Small on purpose:
# the realistic workflow count per SHA is two or three, and a wide pool would
# only buy latency on a repo that does not exist yet.
JOB_WORKERS = 4

_GRACE = _checks.CHECK_CREATION_GRACE_SECS


# ---------------------------------------------------------------------------
# selection — by workflow identity, never by recency
# ---------------------------------------------------------------------------

def latest_per_workflow(runs: object, sha: str) -> dict:
    """The newest run of each workflow **on this SHA**, keyed by workflow name.

    Two filters, and both are load-bearing.

    *By SHA*, because "the branch is green" is a claim about a commit, and a
    run on the previous head is a true statement about a commit the reader has
    already moved past.

    *By workflow*, because that is the axis `--limit 1` collapses. Keeping the
    newest run per workflow — highest `databaseId`, which GitHub allocates
    monotonically — means a re-run supersedes the run it replaces, while a
    *different* workflow never supersedes anything.
    """
    if not isinstance(runs, list) or not sha:
        return {}
    out: dict = {}
    for r in runs:
        if not isinstance(r, dict) or r.get("headSha") != sha:
            continue
        name = str(r.get("workflowName") or "?")
        prev = out.get(name)
        if prev is None or _run_id(r) >= _run_id(prev):
            out[name] = r
    return out


def _run_id(run: object) -> int:
    try:
        return int(run.get("databaseId"))  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        return -1


# A ref that could abbreviate an object name. Seven is git's own floor for an
# abbreviation and the length every tool prints; below it `gh api commits/<x>`
# refuses anyway. Upper bound is a full name.
_HEX_REF = re.compile(r"^[0-9a-fA-F]{7,40}\Z")  # \Z, not $ — #1188

MODE_BRANCH = "branch"
MODE_COMMIT = "commit"


def ref_mode(ref: str, resolved_sha: str) -> str:
    """`commit` or `branch` — decided by the resolution, not by the spelling.

    `gh-branch` has always taken a ref, and `gh api commits/<ref>` resolves a
    branch name and an object name alike. So the op *accepted* a SHA and then
    asked `gh run list --branch <sha>`, which matches no branch and answers
    `[]` with exit 0 — and zero runs rendered as `NO RUN — zero workflow runs
    on 412375a` for a commit carrying two runs and eighteen legs (#1083). An
    absence produced by the tool, printed as an absence in the world, inside
    the op written to stop exactly that.

    The discriminator is free. Anything hex-shaped *might* abbreviate an object
    name, but `deadbee` is also a legal branch name — so the question is
    whether what the ref resolved to **starts with the ref**. A branch answers
    no; an abbreviation answers yes; and a branch that happens to be named
    after the commit it points at answers yes and is right either way, because
    both readings describe the same commit. No second API call, and no
    ambiguity refusal invented for a case the resolution already decided:
    GitHub 422s an abbreviation it cannot resolve, and that is an error, not a
    guess.
    """
    ref = str(ref or "").strip()
    sha = str(resolved_sha or "").strip().lower()
    if not ref or not _HEX_REF.match(ref):
        return MODE_BRANCH
    return MODE_COMMIT if sha.startswith(ref.lower()) else MODE_BRANCH


def previous_head(runs: object, sha: str) -> tuple[str, set]:
    """`(sha, workflow names)` of the newest run set that is *not* this SHA.

    The only evidence available for "a workflow that normally runs here did not
    run on this commit". It is evidence, not a rule: a path filter makes the
    absence legitimate, so nothing here ever concludes on its own — see
    `verdict()`, which only lets it block green inside the creation window.
    """
    if not isinstance(runs, list):
        return "", set()
    prev = ""
    for r in runs:
        if not isinstance(r, dict):
            continue
        s = str(r.get("headSha") or "")
        if s and s != sha:
            prev = s
            break
    if not prev:
        return "", set()
    names = {str(r.get("workflowName") or "?") for r in runs
             if isinstance(r, dict) and r.get("headSha") == prev}
    return prev, names


def run_phase(run: object) -> str:
    """`concluded` | `running` | `unestablished` for one run.

    A concluded run and one still moving must be unmistakably distinct (#615
    comment 1), and an unreadable lifecycle field is a third thing again — it
    is not evidence that the run is still going.
    """
    if not isinstance(run, dict):
        return PHASE_UNESTABLISHED
    raw = str(run.get("status") or "").strip().lower()
    if not raw:
        return PHASE_UNESTABLISHED
    return PHASE_CONCLUDED if raw == _TERMINAL_RUN_STATUS else PHASE_RUNNING


def orphaned_legs(run: object, states) -> int:
    """Legs still pending on a run GitHub has already closed (#1408).

    GitHub does not guarantee that a run object outlives its own jobs. Twice on
    2026-08-11 — run 31501780284 with `pytest (windows-latest, 3.11)` and
    31507113066 with `pytest (macos-latest, 3.12)` — the run read `completed /
    success` while one of its legs sat `in_progress`, and neither leg would ever
    have reported, because the run that would have carried its result was
    already closed. Both cleared only with `gh run rerun --job`.

    So the two fields are not two views of one fact and must not be rendered as
    though they were. `verdict()` already refuses such a commit, through the
    leg half of its `moving` test; this is the same predicate named, so the
    table can be marked from the same reading the verdict used rather than from
    a second derivation of it that could drift.

    Zero for a run still moving: a pending leg under an open run is an ordinary
    wait, and marking it would put a warning on every in-flight commit.
    """
    if run_phase(run) != PHASE_CONCLUDED:
        return 0
    return sum(1 for s in (states or []) if _checks.bucket(s) == "pending")


def orphan_lines(selected: dict, fetched: dict) -> list:
    """One sentence per workflow whose run closed without one of its legs.

    A bare marker in the `Outcome` cell would say only that something is odd,
    which is a second lookup rather than an answer. These lines say which two
    sources disagree and which one the verdict acted on.
    """
    lines = []
    for name in sorted(selected):
        jobs = fetched.get(name)
        if jobs is None:
            continue
        states = [_checks.github_state(j) for j in jobs]
        n = orphaned_legs(selected[name], states)
        if not n:
            continue
        # Flattened for the same reason the workflow name beside it is: this
        # sentence is two-space indented, so a newline in the field emits at
        # column 0 and reads as the tool's own line (#851/#981). `conclusion`
        # is an enum in practice; the convention is not conditioned on that,
        # because both of those issues were filed after somebody reasoned that
        # way about the field next door.
        conclusion = _untrusted.flat(
            str(selected[name].get("conclusion") or "no conclusion"))
        legword = _agrees(n, "leg", "legs")
        lines.append(
            f"  {_untrusted.flat(name)} — the run object concluded "
            f"`{conclusion}`, but {n} {legword} of it never concluded. A "
            "run-level conclusion is not a claim about a leg GitHub closed the "
            f"run without, so `{conclusion}` does not cover it and the verdict "
            "above does not clear the commit. Re-run the leg "
            "(`gh run rerun --job <id>`); waiting will not help, the run that "
            "would have carried its result is already closed.")
    return lines


def leg_summary(states) -> str:
    """The shared tally, not a second derivation of it.

    Exists as a named seam so a test can assert this op and `_checks` agree by
    identity rather than by two renderings that happen to match today.
    """
    return _checks.summarize(states)


# ---------------------------------------------------------------------------
# the conjunction
# ---------------------------------------------------------------------------

def _duration(secs: object) -> str:
    if secs is None:
        return "age unestablished"
    n = max(0, int(secs))
    if n < 60:
        return f"{n}s"
    if n < 3600:
        return f"{n // 60}m"
    if n < 86400:
        return f"{n // 3600}h"
    return f"{n // 86400}d"


def _window(grace: int) -> str:
    return f"~{max(1, grace // 60)}min"


def no_run_verdict(sha: str, age_secs: object, grace: int = _GRACE) -> tuple:
    """Zero runs on this SHA, rendered as *why* rather than as a zero.

    Three readings, and none of them is green. The window is the one measured
    in #585 for GitHub's own run-creation latency; past it the cause is
    declined, because a workflow can legitimately not fire for a ref (path
    filters) and inferring that from `.github/workflows/*` would be inferring
    it from files that need not be the ones on this ref.
    """
    short = sha[:7] if sha else "an unestablished commit"
    if age_secs is None:
        return (NO_RUN, f"{NO_RUN} — zero workflow runs on {short}, and when "
                        "the head commit landed could not be established, so "
                        "whether one is still coming is UNKNOWN. Nothing has "
                        "passed. Check the repo's Actions tab.")
    if int(age_secs) <= grace:
        return (NO_RUN, f"{NO_RUN} — zero workflow runs on {short}; the head "
                        f"commit is {_duration(age_secs)} old, inside the "
                        f"{_window(grace)} window in which a first run has "
                        "always appeared, so a run is still expected. Nothing "
                        "has passed and nothing has failed.")
    return (NO_RUN, f"{NO_RUN} — zero workflow runs on {short}, head commit "
                    f"{_duration(age_secs)} old and past the {_window(grace)} "
                    "window in which a first run normally appears. Whether any "
                    "workflow covers this ref is UNKNOWN — a path filter and a "
                    "workflow that never fired look identical from here. "
                    "Check the repo's Actions tab.")


def scope_clause(undispatched: list, unestablished: str, n_wf: int) -> str:
    """The sentence a GREEN needs so it stops over-claiming (#846).

    `gh-branch`'s green has always meant *every workflow that produced a run on
    this commit passed*. Nothing said so, and the missing half is not visible
    from inside the arithmetic: a workflow with no run is absent from both sides
    of it and cancels out exactly. On the v0.27.0 tag that green covered three
    of four declared workflows and read as covering all of them.

    The verdict itself is not downgraded — see the module docstring of
    `_declared_workflows` for why a shortfall concluded from an absence is a
    false alarm on a merge gate. What changes is that the clearance states its
    own scope, on the line people actually read.

    Empty when everything declared produced a run: a qualifier printed on every
    render is one nobody reads on the render where it matters, and this repo has
    paid for that twice.
    """
    if unestablished:
        # `these {n_wf} are` was a bare plural over a count that is routinely 1
        # — one workflow producing a run on a commit is ordinary — so this read
        # "whether these 1 are all of them" (#841, found reviewing #841's own
        # fix). The noun is named as well as counted, because "whether this 1
        # is" fixes the agreement and still leaves the reader guessing what is
        # being counted.
        subject = _agrees(n_wf, "this 1 workflow is",
                          f"these {n_wf} workflows are")
        return (f" The set of workflows declared at this commit is "
                f"UNESTABLISHED ({unestablished}), so whether {subject} "
                f"all of them is UNKNOWN.")
    if not undispatched:
        return ""
    n = len(undispatched)
    # The names ride on the sentence, not on the block under it. Every surface
    # that republishes this verdict quotes lines, and not always the same ones:
    # `pr_merge._default_branch_report` filters for four prefixes, so a clause
    # ending "named below" arrived on the merge gate with nothing below it.
    # Fixing that one caller would have left the next one, and #846's own
    # reproduction was a Verdict line read on its own.
    shown = [_untrusted.flat(str(w.get("name")))
             for w in undispatched[:_checks.NAMED_CAP]]
    names = ", ".join(f"`{s}`" for s in shown)
    if n > _checks.NAMED_CAP:
        names += f", +{n - _checks.NAMED_CAP} more"
    # These two were written correctly and are routed through `_agrees` anyway:
    # this clause is appended onto the same rendered line as the verdict, and a
    # count word that agrees by hand today is the shape #841 was.
    return (f" This covers the {n_wf} "
            f"{_agrees(n_wf, 'workflow', 'workflows')} that produced a run; "
            f"{n} declared in {_declared_workflows.WORKFLOW_DIR} at this commit "
            f"produced none and {_agrees(n, 'is', 'are')} NOT covered: "
            f"{names}.")


def scope_for(repo: str, sha: str,
              selected: dict) -> tuple[str, list[str], str]:
    """`(clause, lines, unresolved)` — #846's scope check, for every caller.

    Exists as a seam rather than as four lines inlined in `main()` because
    `main()` is not the only surface that publishes this verdict.
    `presets/dashboard/dashboard.py` and `presets/watch/tiers/gh_prs.py` both
    call `verdict()` directly, and both printed "GREEN — every workflow on X
    concluded and every leg passed" with no scope at all — the dashboard being
    the board a human reads immediately before tagging, which is where the
    v0.27.0 mis-cut happened. A caller that has to remember to compute this
    will not — and #1077 is that sentence coming true: the tier was left
    unwired by the same PR that wrote it. So `verdict()` no longer has a
    default for `scope`; a caller that forgets gets a `TypeError` on its first
    run rather than a green that quietly over-claims.

    `unresolved` is the third element because a caller deciding *whether to
    speak* must not have to parse the clause to find out. It names why this
    green cannot account for itself, and is empty when it can:

      * the declared set could not be established, so the green covers a
        universe of unknown size;
      * a workflow declaring a **push** trigger produced no run on a pushed
        commit — the open question #846 exists for.

    A `schedule` / `workflow_dispatch` / `pull_request`-only workflow producing
    no run on a push is *expected* and leaves `unresolved` empty. That is not a
    softening: on this repo `slow tests` and `changelog` are permanently in
    that state, so a surface that spoke whenever the clause was non-empty would
    say the same thing on every tick forever, which is precisely the
    habituation `scope_clause`'s own docstring says this repo has paid for
    twice. The loud/quiet split is `_declared_workflows.is_push_triggered`,
    the same predicate `undispatched_lines` renders on, so the two cannot
    drift — and `None` (an `on:` block that could not be read) counts as loud
    on both sides.
    """
    if not selected:
        return "", [], ""
    owner, name = _declared_legs.owner_repo(repo)
    declared, why = _declared_workflows.declared_at(owner, name, sha)
    if declared is None:
        return (
            scope_clause([], why, len(selected)),
            [f"Declared workflow set at {sha[:7]}: UNESTABLISHED — {why}. The "
             f"verdict above covers the {len(selected)} workflow(s) that "
             f"produced a run and cannot say whether that is all of them."],
            f"the declared workflow set at {sha[:7]} is UNESTABLISHED")
    undispatched = [w for w in declared if w.get("name") not in selected]
    loud = [w for w in undispatched
            if _declared_workflows.is_push_triggered(w.get("triggers"))
            is not False]
    unresolved = ""
    if loud:
        unresolved = (f"{len(loud)} declared workflow(s) a push should reach "
                      f"produced no run on {sha[:7]}")
    return (scope_clause(undispatched, "", len(selected)),
            undispatched_lines(undispatched), unresolved)


def undispatched_lines(undispatched: list) -> list[str]:
    """Name what the verdict does not cover, loudest case on its own line.

    Split by trigger, because the two absences are different questions. A
    `schedule`/`workflow_dispatch`/`pull_request` workflow producing no run on a
    pushed commit is expected — naming those one per line, forever, on every
    call, is how a disclosure gets tuned out — so they collapse into a single
    summary. A workflow declaring a **push** trigger and producing no run is a
    real open question, and gets its own line saying the question is open rather
    than answering it.
    """
    if not undispatched:
        return []
    loud: list[dict] = []
    quiet: list[dict] = []
    for wf in undispatched:
        (loud if _declared_workflows.is_push_triggered(wf.get("triggers"))
         is not False else quiet).append(wf)

    lines = [f"Declared in {_declared_workflows.WORKFLOW_DIR} at this commit "
             f"with no run on it — NOT covered by the verdict above:"]
    for wf in loud:
        triggers = wf.get("triggers")
        if triggers is None:
            said = ("its `on:` block could not be read, so whether a push "
                    "reaches it is UNKNOWN")
        else:
            said = (f"triggers: {', '.join(_untrusted.flat(str(t)) for t in triggers)} "
                    f"— a push trigger IS declared and this commit has no run "
                    f"from it. Whether a branch filter, a path filter, an `if:` "
                    f"or a disabled workflow accounts for that is UNKNOWN from "
                    f"here")
        lines.append(f"  {_untrusted.flat(str(wf.get('name')))} "
                     f"({_untrusted.flat(str(wf.get('path')))}) — {said}.")
    if quiet:
        named = ", ".join(
            f"{_untrusted.flat(str(w.get('name')))} "
            f"({', '.join(_untrusted.flat(str(t)) for t in (w.get('triggers') or []))})"
            for w in quiet)
        lines.append(f"  no push trigger, so no run on this commit is expected "
                     f"and none of it is covered: {named}")
    return lines


def verdict(selected: dict, legs: dict, missing, sha: str,
            age_secs: object, grace: int = _GRACE,
            unreconciled: str = "", *, scope: str) -> tuple:
    """`(state, sentence)` for the whole commit. Conjunctive, and ordered.

    `scope` is keyword-only and has **no default** (#1077). It had one, and the
    seam that computes it (`scope_for`) says in its own docstring that "a
    caller that has to remember to compute this will not" — then the same PR
    wired one of the two callers it names and left `presets/watch/tiers/
    gh_prs.py` publishing an unscoped green on every radar tick. A default is a
    reminder; a required argument is a mechanism. Pass `scope_for(...)[0]`, or
    `""` if you have decided the scope does not apply here — but decide it.

    `legs` maps a workflow name to its leg states, or to ``None`` when the job
    list did not come back. ``None`` is not zero: a workflow whose legs were
    never read cannot contribute to a green, so it decides the whole answer.

    The order the findings are tested in is the order a reader acts on them —
    unread beats failed beats unfinished beats not-yet-created — and every
    branch names what it is talking about, because a verdict that says "not
    green" without naming the workflow sends the reader back to the web UI,
    which is the cost this op exists to remove.

    `unreconciled` is `_checks.shortfall`'s marker when the legs read could not
    be squared with the legs the runs declare (#837). It is tested last, and
    only against the green: every branch above it is a finding about the legs
    that *were* read, and a finding beats a doubt. But a green is a claim about
    all of them, and "every leg I managed to read passed" is not that claim —
    on a merge gate the difference is the whole point of the op.
    """
    if not selected:
        return no_run_verdict(sha, age_secs, grace)

    short = sha[:7] if sha else "?"

    unread = sorted(n for n, v in legs.items() if v is None)
    if unread:
        return (UNKNOWN, f"{UNKNOWN} — the job "
                         f"{_agrees(len(unread), 'list', 'lists')} for "
                         f"{_names(unread)} did "
                         f"not come back, so how many legs ran on {short} is "
                         "UNKNOWN and nothing here establishes green. Re-run "
                         "the op; if it persists, count by hand with "
                         "`gh run view <run-id> --json jobs`.")

    red_wfs = sorted(n for n, states in legs.items()
                     if any(_checks.is_red(s) for s in (states or []))
                     or _checks.is_red(_run_conclusion(selected[n])))
    if red_wfs:
        bad = sum(1 for states in legs.values()
                  for s in (states or []) if _checks.is_red(s))
        legword = _agrees(bad, "leg", "legs")
        return (NOT_GREEN, f"{NOT_GREEN} — {bad} {legword} on {short} did not "
                           f"pass, in {_names(red_wfs)}. Named below.")

    moving = sorted(n for n, r in selected.items()
                    if run_phase(r) != PHASE_CONCLUDED
                    or any(_checks.bucket(s) == "pending"
                           for s in (legs.get(n) or [])))
    if moving:
        return (NOT_GREEN, f"{NOT_GREEN} — nothing has failed, but "
                           f"{_names(moving)} "
                           f"{_agrees(len(moving), 'has', 'have')} not "
                           f"concluded on {short}, so "
                           f"{_agrees(len(moving), 'it is', 'they are')} "
                           "neither a pass nor a fail. The commit is not "
                           "cleared.")

    if missing and age_secs is not None and int(age_secs) <= grace:
        return (NOT_GREEN, f"{NOT_GREEN} — {_names(sorted(missing))} ran on the "
                           f"previous head and "
                           f"{_agrees(len(missing), 'has', 'have')} no run on "
                           f"{short}; the head "
                           f"commit is {_duration(age_secs)} old, inside the "
                           f"{_window(grace)} creation window, so a run is "
                           "still expected. Waiting is the correct action.")

    n_legs = sum(len(v or []) for v in legs.values())
    n_wf = len(selected)
    if unreconciled:
        return (UNKNOWN, f"{UNKNOWN} — every one of the {n_legs} legs read on "
                         f"{short} passed, but the tally could not be squared "
                         f"with what the runs declare ({unreconciled}), so "
                         "whether these are all of the legs is UNKNOWN. "
                         "Detailed below. Nothing here has failed.")
    # `scope` rides on the green and only on the green (#846). Every branch
    # above is a finding, and a reader looking at a finding is not clearing
    # anything — the scope of a clearance is what was over-claimed.
    return (GREEN, f"{GREEN} — every workflow on {short} concluded and every "
                   f"leg passed ({n_legs} legs across {n_wf} "
                   f"{_agrees(n_wf, 'workflow', 'workflows')}).{scope}")


def _run_conclusion(run: object) -> str:
    if not isinstance(run, dict):
        return _checks.UNKNOWN
    raw = str(run.get("conclusion") or "").strip()
    if raw:
        return _checks.normalize(raw)
    # Not concluded yet — that is the `moving` branch's business, not a red.
    return "PENDING"


def _names(names) -> str:
    """The workflow names as an English list — the last separator is `and`.

    A comma-separated list with no conjunction is the shape a **truncated**
    list has, and this one is interpolated into the sentence whose whole job is
    to say whether the commit is cleared (#1374). The reader's question at that
    moment is "is that all of them"; `` `CodeQL`, `changelog`, `tests` ``
    answers it wrong for free, and it costs nothing to answer it right.

    **Not capped**, deliberately, and this is the second half of #1374's
    question. `scope_clause` caps at `_checks.NAMED_CAP` and discloses the
    remainder, because the names there are a *supplementary* list under a
    verdict the reader has already got. These names are the verdict's own
    subject — they are what the reader has to act on — and a shortened subject
    inside a not-concluded sentence is exactly the absence-produced-by-the-tool
    defect this repository keeps filing. Eight backticked names on one line is
    ugly; eight workflows of which three are named is wrong.
    """
    # Flattened here rather than at the four call sites, so the fifth is
    # right too. These names reach the *verdict sentence*, which `pr_merge`
    # republishes on the merge gate — the highest-authority line this op
    # writes, and the one a forged newline would be worth landing in (#851).
    items = [f"`{_untrusted.flat(str(n))}`" for n in names]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _agrees(n: int, singular: str, plural: str) -> str:
    """The form of a word agreeing with the count it is talking about.

    Every count-dependent word in the rendered verdict line comes from here
    rather than from its own inline conditional — including `scope_clause`'s,
    which is appended onto that same line and is therefore part of the same
    sentence, not a neighbour of it. That is the whole mechanism of #841. The
    pronoun half of the not-concluded sentence was already count-aware —
    `'it is' if len(moving) == 1 else 'they are'` — and the verb half next to it
    was left at a hardcoded `has`, so a two-workflow commit rendered
    "`CodeQL`, `tests` has not concluded ... so they are neither a pass nor a
    fail": one sentence, one subject, two different numbers. Two independent
    conditionals over the same count is a disagreement waiting to be written.
    """
    return singular if n == 1 else plural


# ---------------------------------------------------------------------------
# gh plumbing
# ---------------------------------------------------------------------------

def _gh(argv: list, timeout: int = 20):
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _format_error(stderr: str, what: str, commit: bool = False) -> str:
    s = (stderr or "").lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return _repo_target.no_repo_error("gh-branch:master")
    # 422 + "no commit found for sha" is what the commits endpoint returns for
    # a ref that does not exist — not a 404. Left unclassified it echoed gh's
    # own sentence, which names a SHA for something the caller typed as a
    # branch name and reads as an API fault rather than a typo.
    if ("could not resolve" in s or "404" in s or "not found" in s
            or "422" in s or "no commit found" in s):
        # Scope is shared; the hint is not. `_repo_target.not_found_hint()`
        # says "Check the number", which is right for the issue/PR ops it was
        # written for and wrong for a ref the caller typed as a name.
        target = _repo_target.target()
        where = f" (gh repo view {target})" if target else ""
        # A hex-shaped ref that does not resolve is a different mistake from a
        # misspelled branch, and 422 `No commit found for SHA` is the same
        # response for an unknown name and one too short to be unambiguous
        # (#1083). Naming the branch hint at it sends the reader to check a
        # spelling that was never the problem.
        #
        # Flagged by the caller rather than sniffed out of `what`: sniffing is
        # what the first version did, and `_run_list`'s "workflow runs for X"
        # never started with `commit `, so commit mode reached the branch hint
        # anyway — the leak this whole change exists to close, reintroduced one
        # function along.
        if commit:
            return (f"ERROR: {what} not found "
                    f"{_repo_target.not_found_scope()}. GitHub answers the "
                    f"same way for an object name that does not exist, one "
                    f"that is not pushed, and one too short to be "
                    f"unambiguous — which of those it is is UNKNOWN from "
                    f"here. Try the full 40-character name{where}.")
        return (f"ERROR: {what} not found {_repo_target.not_found_scope()}. "
                f"Check the spelling, or that the branch is pushed"
                f"{where}.")
    if "401" in s or "unauthorized" in s or "not logged in" in s:
        return "ERROR: gh CLI not authenticated. Run: gh auth login"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes."
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {what}. Check repo access."
    return f"ERROR: gh failed for {what}: {(stderr or '').strip()}"


def _repo_identity():
    """`(nameWithOwner, defaultBranchRef, error)`.

    Read on every call, not only the no-argument one: the head SHA and the
    verdict are claims about a specific repository, and a call carrying a
    `repo:` target reads as being about the cwd's repo unless it says otherwise.
    """
    target = _repo_target.target()
    argv = ["gh", "repo", "view"] + ([target] if target else []) + \
        ["--json", "nameWithOwner,defaultBranchRef"]
    try:
        r = _gh(argv)
    except FileNotFoundError:
        return "", "", "ERROR: gh not found — install from https://cli.github.com"
    except subprocess.TimeoutExpired:
        return "", "", "ERROR: gh timed out resolving the repository"
    if r.returncode != 0:
        return "", "", _format_error(r.stderr, "this repository")
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return "", "", "ERROR: invalid JSON from gh repo view"
    ref = d.get("defaultBranchRef") or {}
    return (str(d.get("nameWithOwner") or "?"),
            str(ref.get("name") or ""), "")


def _head_commit(ref: str):
    """`(sha, age_secs, error)` for the commit this ref names.

    A branch's head, or — since #1083 — the commit itself, because `gh api
    commits/<ref>` resolves both and the *mode* is decided from what comes
    back. So this runs before anything knows which question was asked, and its
    answer is what decides.

    Resolved from the *ref*, never from the run list. Deriving the SHA from the
    newest run would make "no run exists for this commit" — the state #615's
    second comment is most concerned with — structurally unreachable: the
    answer would always be the SHA of a commit that had a run.
    """
    try:
        r = _gh(["gh", "api", _repo_target.api_path(f"commits/{ref}")])
    except FileNotFoundError:
        return "", None, "ERROR: gh not found — install from https://cli.github.com"
    except subprocess.TimeoutExpired:
        return "", None, f"ERROR: gh timed out resolving ref {ref!r}"
    if r.returncode != 0:
        # The mode is not established yet — that needs the resolved SHA this
        # call is failing to produce. The *shape* is all there is, and it is
        # enough to pick the right hint.
        is_commit = bool(_HEX_REF.match(str(ref or "")))
        kind = "commit" if is_commit else "branch"
        return "", None, _format_error(r.stderr, f"{kind} {ref!r}",
                                       commit=is_commit)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return "", None, f"ERROR: invalid JSON from gh api for branch {ref!r}"
    sha = str(d.get("sha") or "")
    if not sha:
        return "", None, f"ERROR: gh returned no sha for branch {ref!r}"
    return sha, _age_secs((d.get("commit") or {}).get("committer") or {}), ""


def _age_secs(committer: dict):
    raw = str(committer.get("date") or "").strip()
    if not raw:
        return None
    try:
        when = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    return int((datetime.now(timezone.utc) - when).total_seconds())


def _run_list(ref: str, sha: str = ""):
    """The runs to select from, asked for by whichever key can answer.

    `sha` switches the selector to `--commit`, and the caller passes the
    **resolved 40-hex name**, never the ref it was typed as. `gh run list
    --commit 412375a` returns `[]` with exit 0; `--commit 412375ae98…` returns
    the two runs on it (measured 2026-08-08). That silent empty is the failure
    the maintainer hit by hand in #1083, and passing an abbreviation here would
    reproduce it inside the op meant to insulate against it.
    """
    selector = ["--commit", sha] if sha else ["--branch", ref]
    try:
        r = _gh(["gh", "run", "list", *selector, "--limit",
                 str(RUN_LIST_LIMIT), "--json",
                 "workflowName,headSha,databaseId,status,conclusion,event,"
                 "createdAt,attempt"] + _repo_target.gh_args())
    except FileNotFoundError:
        return None, "ERROR: gh not found — install from https://cli.github.com"
    except subprocess.TimeoutExpired:
        return None, f"ERROR: gh timed out listing runs for {ref!r}"
    if r.returncode != 0:
        what = (f"workflow runs on commit {sha[:7]}" if sha
                else f"workflow runs for {ref!r}")
        return None, _format_error(r.stderr, what, commit=bool(sha))
    try:
        return json.loads(r.stdout or "[]"), ""
    except json.JSONDecodeError:
        return None, "ERROR: invalid JSON from gh run list"


def _jobs_for(run_id: int):
    """The job list for one run, or ``None`` when it did not come back.

    ``None`` and ``[]`` are different answers and stay different all the way to
    the verdict. An empty list is an established fact; a missing one is an
    absence in the tool, and counting it as zero passing legs is the guess this
    repository keeps re-filing.
    """
    try:
        r = _gh(["gh", "run", "view", str(run_id), "--json", "jobs"]
                + _repo_target.gh_args())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    jobs = d.get("jobs")
    return jobs if isinstance(jobs, list) else None


def _reconcile(repo: str, selected: dict, fetched: dict) -> tuple:
    """`(marker, lines)` squaring the legs read against the legs declared (#837).

    The second source is `jobs?filter=all`, per run, and the reasoning is in
    `presets/_declared_legs`. Branch scope adds one thing over `gh-pr`'s: the
    run ids come from the run *list*, so a workflow is reconciled whether or
    not anything it produced reached a check rollup.

    Runs whose job list never came back are skipped rather than reconciled —
    `verdict()` already answers UNKNOWN for those, and a second, differently
    worded doubt about the same absence is noise.

    The two sides are summed across workflows before `shortfall()` sees them,
    so the `Legs:` line and the marker beside it are the same arithmetic. Any
    single run that cannot be reconciled makes the whole answer unverified: a
    declared total summed over only the readable runs is smaller than the
    truth, and a smaller declared total is exactly what makes a short tally
    look complete.
    """
    owner, name = _declared_legs.owner_repo(repo)
    found_total = 0
    declared_total: int | None = 0
    missing: list = []
    for wf, run in sorted(selected.items()):
        jobs = fetched.get(wf)
        if jobs is None:
            continue
        found = [str(j.get("name") or "?") for j in jobs
                 if isinstance(j, dict)]
        found_total += len(found)
        if not _declared_legs.reconcilable(run.get("attempt")):
            declared_total = (declared_total + len(found)
                              if declared_total is not None else None)
            continue
        names = _declared_legs.legs_for_run(owner, name, _run_id(run))
        if names is None:
            declared_total = None
            continue
        if declared_total is not None:
            declared_total += len(names)
        missing.extend(f"{wf} / {n}" for n in
                       _declared_legs.missing_names(names, found))
    if not found_total and declared_total == 0:
        return ("", [])
    return _checks.shortfall(found_total, declared_total, missing)


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

#: Width of the `Run` column — the id plus ` attempt N`. Run ids are 11 digits
#: today and GitHub allocates them monotonically, so the slack is for the digit
#: it will grow, not decoration. A column narrower than its content does not
#: truncate here, it pushes the next one right and un-aligns the table.
RUN_COL = 26
PHASE_COL = 14


def run_cell(run: object) -> str:
    """`<id> attempt <n>` — the run this row is about, named so it can be used.

    Until #1409 the table named workflows and never named runs, so there was no
    route from this render to `gh-run:<id>` or to a re-run without a raw API
    call. Both fields were already fetched by `_run_list`; only the render was
    missing.

    `attempt` is printed **always**, including on attempt 1. A number that
    appears only when it is interesting cannot be told from a number the tool
    failed to read, and that pair is the defect this repository keeps filing.
    An unreadable id or attempt is `?` — never `-1` (what `_run_id` answers for
    an absence) and never a defaulted `1`.
    """
    rid = _run_id(run)
    ident = str(rid) if rid >= 0 else "id ?"
    attempt = "?"
    if isinstance(run, dict):
        try:
            attempt = str(int(run.get("attempt")))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            attempt = "?"
    return f"{ident} attempt {attempt}"


def table_header() -> str:
    """The column names. `Run` names the run; `Phase` is what `Run` used to hold.

    Split rather than crammed: the lifecycle word is the answer to "is this
    still moving" (#615 comment 1) and the id is the answer to "what do I call
    it next", and one column cannot carry both without a reader parsing it.
    """
    return (f"{'Workflow':<32} {'Run':<{RUN_COL}} {'Phase':<{PHASE_COL}} "
            f"{'Outcome':<14} Legs")


def run_id_note() -> str:
    """What the two new numbers are for, said once under the table.

    The `attempt` half is stated carefully because the issue that asked for it
    got it wrong: a re-run is a further **attempt on the same run object**, not
    a second run object on the same SHA — which is why
    `_declared_legs.reconcilable` pays for a second source exactly when the
    attempt is not 1, and why the tally on the row is the latest attempt only.
    """
    return ("Run ids: `gh-run:<id>` for one run's legs, `gh run rerun <id>` to "
            "retry it. `attempt N` is GitHub's `run_attempt` — N > 1 means the "
            "run was re-run and the tally beside it counts the latest attempt "
            "only, so legs from an earlier attempt are NOT in it. Only the "
            "newest run of each workflow on this commit is listed, so the row "
            "count is workflows, never attempts.")


def _row(name: str, run: dict, jobs) -> str:
    """One workflow's row. The name is flattened because it is not ours (#851).

    Weaker than the check-run half of #851 — renaming a workflow needs write
    access to the base repo, so a fork PR does not reach it — and the same
    missing boundary, in a fixed-width table where one extra line is one extra
    workflow that a reader will count as having run.
    """
    name = _untrusted.flat(name)
    phase = run_phase(run)
    if jobs is None:
        states = None
        tally = "UNREAD — the job list did not come back"
    else:
        states = [_checks.github_state(j) for j in jobs]
        tally = leg_summary(states)
    if phase == PHASE_CONCLUDED:
        outcome = _untrusted.flat(
            str(run.get("conclusion") or "no conclusion"))
        # #1408: the cell may not assert a conclusion the verdict has refused.
        # Marked rather than rewritten — `success` is what the run object says
        # and suppressing it would trade one silent disagreement for another —
        # and spelled out in `orphan_lines()` under the table.
        if orphaned_legs(run, states):
            outcome += " ⚠"
    elif phase == PHASE_RUNNING:
        outcome = "not yet"
    else:
        outcome = "not read"
    return (f"{name:<32} {run_cell(run):<{RUN_COL}} {phase:<{PHASE_COL}} "
            f"{outcome:<14} {tally}")


def main() -> int:
    args = [a for a in sys.argv[1:] if a != ""]

    repo, default_branch, err = _repo_identity()
    if err:
        print(err)
        return 1

    ref = args[0] if args else default_branch
    if not ref:
        print("ERROR: no branch given and the repository's default branch "
              "could not be resolved. Name one: gh-branch:BRANCH")
        return 1
    # #852: the same guard as `git/checkout.py:80`, `git/merge.py:140`,
    # `_git_common.py:142` and `mr.py`'s `_ORDINARY_REF` — the invariant
    # `fix/818-git-arg-injection` established, which this file dropped. `ref`
    # reaches `gh run list --branch <ref>` below, where `--output` or `-b` is a
    # flag and not a branch, and quoting does not stop a shell word from being
    # read as one.
    #
    # There was no exploit: `_head_commit` runs first and puts the ref in a URL
    # path, where a leading dash is not a flag, so it 404s and returns 1 before
    # the run list is reached. That is the reason for the guard rather than an
    # argument against it — the safety was a property of the *call order*,
    # invisible at the sink, and any refactor that hoisted the run list or made
    # the head lookup lazy would have removed it without touching anything that
    # looked security-relevant.
    #
    # Bare `-` is refused too, unlike in `checkout.py` where it means "the
    # previous branch". There is no previous branch here: this op asks GitHub
    # about a named ref, and `-` names nothing it could answer for.
    if ref.startswith("-"):
        print(f"ERROR: ref starts with '-' (refusing for safety): {ref!r}. "
              f"A leading dash is read as a flag by the commands this op "
              f"builds, not as a branch name — and git will not create a "
              f"branch with one. Name the branch: gh-branch:BRANCH")
        return 1

    sha, age, err = _head_commit(ref)
    if err:
        print(err)
        return 1

    mode = ref_mode(ref, sha)
    runs, err = _run_list(ref, sha if mode == MODE_COMMIT else "")
    if err:
        print(err)
        return 1

    selected = latest_per_workflow(runs, sha)
    if mode == MODE_COMMIT:
        # `--commit` returns one commit's runs, so there is no second commit in
        # the list to be the previous head. Branch mode's "ran last time and
        # not this time" evidence is simply not available here — and silence
        # about a check that did not run reads as the check passing, which is
        # the defect this op exists for. Said out loud, below the table.
        prev_sha, prev_names = "", set()
    else:
        prev_sha, prev_names = previous_head(runs, sha)
    missing = sorted(prev_names - set(selected))

    legs: dict = {}
    named: list = []
    if selected:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(JOB_WORKERS, len(selected))) as pool:
            fetched = dict(zip(
                selected,
                pool.map(lambda n: _jobs_for(_run_id(selected[n])), selected)))
        for name, jobs in fetched.items():
            if jobs is None:
                legs[name] = None
                continue
            legs[name] = [_checks.github_state(j) for j in jobs]
            for j in jobs:
                named.append((_untrusted.flat(f"{name} / {j.get('name', '?')}"),
                              _checks.github_state(j),
                              "job",
                              str(j.get("databaseId") or "")))
    else:
        fetched = {}

    marker, shortfall_lines = _reconcile(repo, selected, fetched)

    # #846: the second source one scope out. Bought only when there is a run
    # set to be short of — on a commit with no runs at all `no_run_verdict`
    # already declines, and two more API calls would buy nothing.
    scope, scope_lines, _unresolved = scope_for(repo, sha, selected)
    state, sentence = verdict(selected, legs, missing, sha, age, _GRACE,
                              marker, scope=scope)

    if mode == MODE_COMMIT:
        print(f"# Is commit `{sha[:7]}` green? — {repo}")
        print(f"Commit {sha[:7]}: {state}")
        print(f"Commit: {sha[:7]} ({sha}) — {_duration(age)} old")
    else:
        print(f"# Is `{ref}` green? — {repo}")
        print(f"Branch {ref}: {state}")
        print(f"Head: {sha[:7]} ({sha}) — {_duration(age)} old")
    print(f"Verdict: {sentence}")

    if selected:
        all_states = [s for v in legs.values() for s in (v or [])]
        print(f"Legs: {leg_summary(all_states)}"
              f"{' ' + marker if marker else ''}")
        for line in shortfall_lines:
            print(line)
        for line in _checks.named_disclosure(named):
            print(line)

        print()
        # `flat_note` rather than `banner()` (#819): this render fences nothing,
        # and a banner promising markers it never prints is a disclosure that
        # teaches a reader to skip the next one. Placed here because everything
        # below it — the table and the previous-head list — carries names, and
        # everything above it is this op's own arithmetic.
        print(_untrusted.flat_note("workflow and job names"))
        print(table_header())
        print("-" * 110)
        for name in sorted(selected):
            print(_row(name, selected[name], fetched.get(name)))
        print()
        print(run_id_note())

        orphans = orphan_lines(selected, fetched)
        if orphans:
            print()
            for line in orphans:
                print(line)

    if scope_lines:
        print()
        for line in scope_lines:
            print(line)

    if mode == MODE_COMMIT:
        print()
        print("Previous head: not read in commit mode — `gh run list --commit` "
              "returns this commit's runs and no others, so which workflows "
              "ran on the commit before this one is UNKNOWN here. `gh-branch:"
              "BRANCH` carries that comparison. What IS covered is the "
              f"declared set at {sha[:7]}, above, which is the stronger of the "
              "two and does not depend on history.")

    if missing:
        print()
        print(f"Workflows on the previous head {prev_sha[:7]} with no run on "
              f"{sha[:7]}:")
        for name in missing:
            print(f"  {_untrusted.flat(name)} — did NOT run on this commit. "
                  "That is a different "
                  "sentence from 'ran and passed'; whether a path filter "
                  "excluded it or a run is still to be created is UNKNOWN.")

    # Zero for every *established* verdict, green or not. Nonzero is reserved
    # for "this op could not answer" — the family's convention (`gh-run` exits
    # 0 on a run that concluded `failure`), and load-bearing here: supertool
    # renders a nonzero exit as `FAIL`, which would print the same banner for a
    # branch whose tests are merely still running as for a gh that is not
    # authenticated. Two different things rendering alike is the defect this op
    # exists to remove; it does not get to reintroduce it in its own exit code.
    # The verdict is the `Branch <name>: <STATE>` line, which a caller can
    # match on and a reader cannot miss.
    return 0


if __name__ == "__main__":
    sys.exit(main())
