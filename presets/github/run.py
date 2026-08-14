#!/usr/bin/env python3
"""GitHub Actions workflow run details via gh CLI."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from typing import Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)

import _checks  # noqa: E402  (the one check tally, shared with gh-pr / gh-prs)
import _declared_legs  # noqa: E402  (the second leg count, shared with gh-pr / gh-branch)
import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)
import _branch_locale  # noqa: E402  (where the branch is checked out — shared by all five #850)
import _untrusted  # noqa: E402  (a run's branch and its workflow/job names are remote text — #851/#965)

# Parenthetical that keeps GitHub's own run-level field visible next to the
# computed tally (#789). Visible, but never leading: `Status: queued` was
# printed above a table of ten legs `completed success`, two running and two
# queued, and read as "nothing has started" twice in one session. The field is
# not wrong — it is a run-lifecycle field being read as a leg summary, the same
# shape as #445/#454, where a summary line did not sum what was beneath it.
_FIELD = "run-level field"

# GitHub never transitions a run out of `completed`. The `gh-run` watcher keys
# its `is_terminal` on the same string. This is a *run-lifecycle* field, which
# is why comparing it to a literal is right here and wrong for a leg state:
# the lifecycle has one terminal value and GitHub owns it, while the set of
# leg states grows (#803).
_TERMINAL_RUN_STATUS = "completed"

# Steps named under one red leg before the list elides. A cancelled job can
# carry two dozen cancelled steps, and the thing meant to answer "where did it
# break" must not become the thing that fills the screen — this repo's
# established disclosure cap and its `+N more` spelling (#605), shared with
# `_checks.named_disclosure` so the two never diverge.
_STEP_CAP = _checks.NAMED_CAP


#: Anchored with `\Z` and not `$`: Python's `$` also matches immediately before
#: a final newline, so `^[0-9]+$` accepts `"5\n"` — through the one guard whose
#: whole purpose is to refuse before anything is fetched (#1188). Both values it
#: gates are built into a subprocess argv.
_DIGITS = re.compile(r"^[0-9]+\Z")

#: The attempt selector's token. `attempt=N` and not a bare second number:
#: `gh-run:31815095925:1` says nothing at the call site about what the 1 is, and
#: this op's neighbours already spell an option this way (`gh-labels:tally=`).
ATTEMPT_PREFIX = "attempt="

#: The fields this op reads. Named once because two call sites request them —
#: the default read and the pinned-attempt read — and a set that drifted between
#: the two would render an attempt missing fields the default render has.
_RUN_JSON = ("databaseId,name,status,conclusion,event,headBranch,"
             "createdAt,updatedAt,url,jobs,attempt")


def refuse_run_id(run_id: str) -> str:
    """Message refusing a run id that is not one. Empty string when it is.

    `gh-job`'s guard (#1145), one op over, and for the same reason plus one:
    GitHub's REST API coerces a numeric id with trailing text back to the
    number and answers 200, so a mangled id used to render in the header as the
    thing that was read. Since #1715 this value is also interpolated into a
    `gh run view --attempt` argv, which makes the check a precondition of the
    fetch rather than a courtesy about the header.

    `str.isdigit()` is not the test — it accepts Arabic-Indic digits and
    superscripts, neither of which is an id GitHub will answer for.
    """
    if _DIGITS.match(run_id):
        return ""
    stray = "".join(sorted({c for c in run_id if not _DIGITS.match(c)}))
    digits = "".join(c for c in run_id if _DIGITS.match(c)) or "RUN_ID"
    return (f"ERROR: gh-run takes a numeric run id and got {run_id!r} "
            f"(not a digit: {stray!r}).\n"
            f"Nothing was read. A non-numeric id is the tell that the op string "
            f"was mangled before it arrived, and GitHub cannot be relied on to "
            f"reject it — it coerces `actions/runs/123ep` back to 123 and "
            f"answers 200.\n"
            f"Re-run with the digits alone: gh-run:{digits}")


def refuse_attempt(value: str) -> str:
    """Message refusing an attempt token that is not a positive integer."""
    return (f"ERROR: gh-run's attempt must be a whole number 1 or greater, and "
            f"got {value!r}.\n"
            f"Nothing was read. GitHub numbers attempts from 1, and this value "
            f"is built into the argv of `gh run view --attempt`, so a token "
            f"that is not an attempt is refused before anything is fetched "
            f"rather than relayed to gh.\n"
            f"Usage: gh-run:RUN_ID:attempt=1")


def refuse_token(token: str) -> str:
    """Message refusing a token this op has no reading for."""
    return (f"ERROR: gh-run does not take a {token!r} token.\n"
            f"Nothing was read. Core refused a token this op's cmd could not "
            f"reach until that cmd widened to take every one of them "
            f"(#873/#1715), so the refusal lives here now. Dropping it would "
            f"render the LATEST attempt under a call that asked for something "
            f"else, which is an answer to a question nobody put.\n"
            f"The only token is attempt=N. Usage: gh-run:RUN_ID[:attempt=N]")


def refuse_past_latest(run_id: str, attempt: int, latest: int) -> str:
    """Message refusing an attempt number the run does not have.

    Named rather than fetched. The run's own payload carries the count, so a
    404 would spend a call to learn something already read — and `gh` renders
    that 404 as `failed to get run`, which reads as *no such run* and is a
    different, wrong answer.
    """
    plural = "" if latest == 1 else "s"
    tail = f" | earliest: gh-run:{run_id}:attempt=1" if latest > 1 else ""
    return (f"ERROR: run #{run_id} has {latest} attempt{plural}; attempt "
            f"{attempt} does not exist.\n"
            f"Nothing further was read — the count comes from the run payload "
            f"this op already fetched, not from a 404.\n"
            f"Latest: gh-run:{run_id}{tail}")


def refuse_duplicate(first: str, second: str) -> str:
    """Message refusing a second attempt token. One call answers for one attempt."""
    return (f"ERROR: gh-run was given two attempt tokens, {first!r} then "
            f"{second!r}.\n"
            f"Nothing was read. Taking the last one silently discards the "
            f"first, which is the same wrong answer as dropping an "
            f"unreachable token (#873) with the token still visible in the op "
            f"string.\n"
            f"One attempt per call; two attempts is two ops in the same call.")


def parse_argv(argv: Sequence[str]) -> tuple[str, int | None, str]:
    """`(run_id, attempt, refusal)`. A non-empty refusal means fetch nothing.

    Core splits the op string on every ':' and hands this op every token since
    #1715, so the tokens core used to refuse on its behalf (#873) arrive here
    and this is what refuses them. `attempt` comes back as an `int` — the
    caller's text is matched, then re-rendered from the integer, so no string
    the caller typed reaches the argv.
    """
    tokens = [str(t) for t in argv]
    if not tokens or not tokens[0]:
        return ("", None, "ERROR: usage: run.py RUN_ID [attempt=N]")

    run_id = tokens[0]
    bad = refuse_run_id(run_id)
    if bad:
        return ("", None, bad)

    attempt: int | None = None
    seen = ""
    for token in tokens[1:]:
        if not token:
            continue
        if not token.startswith(ATTEMPT_PREFIX):
            return ("", None, refuse_token(token))
        value = token[len(ATTEMPT_PREFIX):]
        if not _DIGITS.match(value) or int(value) < 1:
            return ("", None, refuse_attempt(value))
        if attempt is not None:
            # Last-wins is the #873 defect with the token kept rather than
            # dropped: `attempt=1:attempt=2` would render attempt 2 under a
            # call that also named attempt 1, and nothing in the output would
            # say the first one was discarded. Two attempts is two calls.
            return ("", None, refuse_duplicate(seen, token))
        seen = token
        attempt = int(value)
    return (run_id, attempt, "")


def latest_attempt(field: object) -> int | None:
    """The run's `run_attempt`, or None when the payload did not carry one.

    None is not 1. A field nobody read must not settle whether this run was
    re-run, because that answer decides whether a whole attempt's legs are
    missing from the table below it.
    """
    try:
        n = int(field)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def attempts_line(run_id: str, latest: int | None, showing: int | None) -> str:
    """Which attempt this table is, and which ones it is not (#1715).

    Three states, not two (`docs/validators.md`, "Declining instead of
    guessing"). The table below has only ever been **one attempt's** legs, and
    until #1715 nothing said so: a re-run run rendered its latest attempt with
    no hint that an earlier one existed, so the earlier legs' absence read as
    an absence of legs — this repository's own defect inside a render nobody
    suspected. Recovering the evidence that settled #1709 meant leaving
    supertool for `gh api .../attempts/1/jobs`.

    `showing` is the attempt the caller asked for, or None for the default.
    **The default is and stays the latest**; nothing here changes what is
    selected, it states what was. A superseded attempt is labelled HISTORICAL
    because a stale red that does not announce itself as stale is read as the
    state of the run now — and `gh-branch` and the merge gate read the latest,
    which is what makes them honest.
    """
    if latest is None:
        return ("Attempts: UNKNOWN — this payload carried no readable "
                "run_attempt, so whether this run was re-run is unread. The "
                "table below is one attempt's legs and which one is not "
                "established.")
    if latest == 1:
        return ("Attempts: 1 of 1 — this run was never re-run, so no earlier "
                "attempt exists and the table below is its whole history.")
    if showing is not None and showing < latest:
        return (f"Attempts: {showing} of {latest} — HISTORICAL. Attempt "
                f"{latest} superseded this one and is what gh-branch and the "
                f"merge gate read, so nothing below is a statement about this "
                f"run now. Current: gh-run:{run_id}")
    earlier = "attempt 1" if latest == 2 else f"attempts 1-{latest - 1}"
    return (f"Attempts: {latest} of {latest} — the table below is attempt "
            f"{latest} only; {earlier} ran and those legs are NOT in it. "
            f"Read one: gh-run:{run_id}:attempt=1")


def red_breakdown(states: list[str]) -> str:
    """`3 failed, 2 cancelled, 1 unknown` — the header's terms, re-used (#803).

    `## Failed jobs (N)` and the header tally are two numbers on one screen,
    and two numbers that disagree are worse than one that was too small. They
    cannot disagree here because they are not two derivations: both bucket
    through `_checks.bucket` and both spell leftovers with `_checks.label`, so
    every term printed below is the same term `summarize()` printed above, and
    the section's `N` is their sum.

    The section is deliberately *wider* than the header's `N failed` term —
    it is the set of legs a reader must act on, which is `is_red()`: the
    failed bucket, plus `CANCELLED` and every state this module has not been
    taught about. Those extra legs do not disappear from the reconciliation,
    they appear as their own named terms, which is why the breakdown is
    printed at all rather than left as a bare count a reader has to trust.
    """
    counts = Counter(
        "failed" if _checks.bucket(s) == "failed" else _checks.label(s)
        for s in states
    )
    ordered = sorted(counts.items(),
                     key=lambda kv: (kv[0] != "failed", -kv[1], kv[0]))
    return ", ".join(f"{n} {lab}" for lab, n in ordered)


def steps_resolved(steps: object) -> int:
    """How many of a job's steps will not change again (#803).

    The `N/M steps` column answers **how much is left to happen**, not how
    much went well — the verdict is the `Conclusion` cell immediately to its
    left, and duplicating it here would say nothing new while hiding the one
    thing the column is for. So a step counts as resolved whatever its
    verdict: a `cancelled` or `timed_out` step is over, and the literal list
    it used to be tested against (`success`/`failure`/`skipped`) rendered a
    finished job as `8/10 steps`, which reads as still working.

    Two states are not resolved, for opposite reasons. A pending step has not
    finished — that is the whole point of the count. An `UNKNOWN` step (an
    entry carrying neither `conclusion` nor `status`) was not *read*, and
    counting an unread step as done is the guess this repo keeps re-filing;
    it depresses the numerator instead, so the column under-claims progress
    rather than over-claiming it.
    """
    if not isinstance(steps, list):
        return 0
    n = 0
    for step in steps:
        state = _checks.github_state(step) if isinstance(step, dict) else _checks.UNKNOWN
        if state == _checks.UNKNOWN or _checks.bucket(state) == "pending":
            continue
        n += 1
    return n


#: Width of the `Job id` column — `job #` plus the integer. Actions job ids are
#: 11 digits today and GitHub allocates them monotonically, so the slack is for
#: the digit they will grow, not decoration (`gh-branch`'s `RUN_COL`, #1409).
JOB_ID_COL = 18


def job_id_cell(job: object) -> str:
    """`job #<id>` for one leg, or `id unread` when the payload carried none.

    **The word is minted here and nowhere else**, so the table and the
    failed-jobs section cannot disagree about what an id is called or about
    what its absence looks like.

    `job`, never `check`, and that is established rather than inferred (#827).
    `gh-job` answers for both of GitHub's id namespaces and they overlap in one
    direction, which is why `gh-pr:N:status` has to decide per leg — its source
    is a status rollup and mixes CheckRun with StatusContext. This op's source
    cannot: `gh run view --json jobs` reads
    `repos/{o}/{r}/actions/runs/{id}/jobs`, an Actions-jobs endpoint, and a
    check run is in no run's job list — that is the documented reason
    `gh-job`'s old "use gh-run to list jobs first" advice could not work
    (`docs/presets/github.md`, the #827 table). So every id here is a job id by
    construction of the request, and labelling it per row is a restatement of
    that fact, not a guess about it.

    The label is written per row rather than left to the column header because
    a row gets quoted on its own, into a report or another agent's brief, where
    the header is not travelling with it.

    An unreadable id renders `id unread`, not `job #?` and not `job #None` —
    the latter is what `.get("databaseId", "?")` printed for a key present with
    a null value. An absence must not wear the shape of an id: the reader's
    next move is one op on that integer, and a plausible-looking non-integer
    buys them a 404 that reads as "no such job".

    `bool` is refused with the other non-integers. `True` is an `int` in Python
    and would render `job #True`, which is neither an id nor an absence.
    """
    if not isinstance(job, dict):
        return "id unread"
    ident = job.get("databaseId")
    if isinstance(ident, bool) or not isinstance(ident, int):
        return "id unread"
    return f"job #{ident}"


def job_id_note() -> str:
    """What the ids in the table are for, said once beneath it (#1482).

    Printed only when there is a table — a pointer at ids nobody was given
    reads as a table that failed to render.
    """
    return ("Job ids: `gh-job:<id>` for one leg's log, `gh-job:<id>:fail` for "
            "just its failing steps. These are Actions **job** ids, not check "
            "run ids — this run's job list holds no check runs, so `gh-job` "
            "resolves every id above in the job namespace.")


def red_steps(job: object) -> list[str]:
    """Names of the steps that put a red leg in the failed-jobs section.

    Same predicate as the leg itself. Listing only `conclusion == "failure"`
    steps left a `cancelled` leg named with no step under it at all, so the
    section said *which* job without saying *where* — and for a timed-out or
    cancelled job that is the only detail there is.
    """
    if not isinstance(job, dict) or not isinstance(job.get("steps"), list):
        return []
    return [str(s.get("name", "?")) for s in job["steps"]
            if isinstance(s, dict) and _checks.is_red(_checks.github_state(s))]


def job_states(jobs: object) -> list[str] | None:
    """One state token per job, or `None` when the payload carried no job list.

    `None` and `[]` are different answers and are rendered differently. An
    empty list is an established fact about the run — GitHub has created no
    jobs. A missing list is an absence in the *tool*: nothing was read, so
    nothing may be concluded. Collapsing the second into the first prints a
    verdict about legs that were never looked at, which is the failure mode
    this repository keeps re-filing.

    Length always equals `len(jobs)`. `_checks.github_state` resolves each
    entry the same way the PR rollup is resolved — `conclusion` once the job
    finishes, `status` while it moves, `UNKNOWN` for an entry carrying
    neither — so a job can never drop out of the count.
    """
    if not isinstance(jobs, list):
        return None
    return [_checks.github_state(j) for j in jobs]


def declared_legs(url: str, run_id: str, attempt: object,
                  found_names: Sequence[str]) -> tuple[int | None, list[str]]:
    """`(declared, missing)` for this run — `(None, [])` when unestablished.

    The second source is `jobs?filter=all`, and the reason is measured in
    `_declared_legs`: `gh run view --json jobs` is `filter=latest`, which dips
    to a strict subset of the matrix for ~18s after a partial re-run.

    **On attempt 1 the answer is the tally itself, and no call is made.**
    `filter=all` and `filter=latest` are the same rows when there is only one
    attempt, so buying the second source there is paying for a number that
    equals the first by construction. It returns `len(found_names)` rather
    than `None` because nothing failed — declining would print
    `TALLY UNVERIFIED` over every ordinary run in the repository, and a
    warning that is always on is one nobody reads.

    That silence is not a claim that the tally is complete on a first attempt.
    A run still creating its jobs is short at *both* sources, and no second
    count can see a leg GitHub has not made yet; the run-level field carries
    that state instead, in `status_line`.
    """
    if not _declared_legs.reconcilable(attempt):
        return (len(found_names), [])
    owner, repo = _declared_legs.owner_repo(url)
    total, names = _declared_legs.legs_for_runs(owner, repo, [run_id])
    if total is None:
        return (None, [])
    return (total, _declared_legs.missing_names(names, found_names))


def status_line(run_status: object, conclusion: object,
                states: list[str] | None,
                declared: int | None = None) -> str:
    """The header's verdict: what the job table adds up to, then GitHub's field.

    **Which leads when the two disagree is not one answer, because they answer
    two different questions.** The tally leads on *are the legs green* — it is
    arithmetic over what actually ran and it can be audited term by term. The
    run-level field leads on *is the run over* — and only it can, because the
    tally structurally cannot see a leg GitHub has not created yet (a `needs:`
    gated job appears only once its dependency finishes). So an unfinished run
    whose every read leg passed says so explicitly rather than reading as done,
    and a run GitHub calls `completed` while a leg still reads as running
    reports the disagreement as UNKNOWN instead of picking a winner. Picking
    one would reproduce #789 in whichever direction the loser was right.

    Every leg state is counted by `_checks.summarize`, whose terms sum back to
    the leg count, so `CANCELLED`, `SKIPPED`, `NEUTRAL` and any state GitHub
    adds after this was written surface under their own name instead of
    evaporating (#445/#454). `TIMED_OUT` and `ACTION_REQUIRED` count as failed,
    not as benign — a job that ran out of wall clock produced no verdict and
    one waiting on a human is blocking.

    Zero legs is three states, not two (`docs/validators.md`, "Declining
    instead of guessing"): not created yet, created none and finished, and
    *unread*. `declared` adds the fourth, and it is the one that was being
    printed as a falsehood: **zero legs read while the run declares some**.
    `completed failure, and zero legs ran — GitHub created no job for this
    run, so nothing was tested` was printed about a run that had executed
    fourteen legs seconds earlier (#804, measured 15:57:31 on run
    30997282630). That is not an unsupported claim, it is one the second
    source contradicts, so it is only ever printed when nothing contradicts
    it.
    """
    raw = str(run_status or "").strip().lower()
    concl = str(conclusion or "").strip().lower()
    field = f"({_FIELD}: {raw or 'unestablished'})"
    over = raw == _TERMINAL_RUN_STATUS
    ended = f"completed {concl}" if concl else "completed with no conclusion"

    if states is None:
        return (f"UNKNOWN — this run payload carried no job list, so nothing "
                f"was tallied and whether any leg passed is UNKNOWN {field}. "
                f"Count by hand: gh run view <run-id> --json jobs")

    if not states:
        if declared:
            legword = "leg" if declared == 1 else "legs"
            return (f"{ended if over else 'in progress'} — zero legs read "
                    f"while this run declares {declared} {legword}, so the "
                    f"job list is being re-created and nothing here says the "
                    f"run tested nothing {field} {_checks.NOT_GREEN}")
        if over:
            return (f"{ended}, and zero legs ran — GitHub created no job for "
                    f"this run, so nothing was tested {field} "
                    f"{_checks.NOT_GREEN}")
        if not raw:
            return (f"UNKNOWN — zero legs, and the run-level field is empty, "
                    f"so whether this run has started is UNKNOWN {field}")
        return (f"no legs yet — GitHub has created no job for this run. "
                f"Nothing has passed and nothing has failed; whether any leg "
                f"appears is not established {field}")

    tally = _checks.summarize(states)
    pending = sum(1 for s in states if _checks.bucket(s) == "pending")

    if over and pending:
        legs = "leg reads" if pending == 1 else "legs read"
        verdict = (f"{ended}, but {pending} {legs} as running or queued — "
                   f"the run-level field and the legs disagree and which one "
                   f"is current is UNKNOWN")
    elif over:
        verdict = ended
    elif pending:
        verdict = "in progress"
    else:
        verdict = ("in progress — every leg read has resolved, but the run is "
                   "not marked complete, so more legs may still be created")

    return f"{verdict} — {tally} {field}"


def _local_branch_check(source: str) -> str:
    """Return a one-line local-branch-vs-source line for output.

    `describe`, not `check` (#1056). Reading a run is not a claim about wanting
    its branch: the run you need to read is routinely one you are not on and
    should not switch to, so the `⚠ MISMATCH — switch with: …` form framed the
    ordinary case as an error and prescribed an action that moves `HEAD` in a
    worktree mid-work. The state is still stated; only the imperative is gone.

    The other four sites of #850 keep `check` — see that issue, still open.
    """
    return _branch_locale.describe(source)


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify gh errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return _repo_target.no_repo_error("gh-run:12345")
    if "could not resolve" in s or "404" in s or "not found" in s:
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. "
                f"{_repo_target.not_found_hint()}")
    if "401" in s or "unauthorized" in s or "not logged in" in s or "token" in s:
        return f"ERROR: gh CLI not authenticated. Run: gh auth login (verify with: gh auth status)"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes and retry."
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check repo access (gh auth status)."
    # The remote host wrote this text — flattened, never relayed raw (#1606).
    return (f"ERROR: gh failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def fetch_run(run_id: str, attempt: int | None) -> tuple[dict | None, str]:
    """One `gh run view`, optionally pinned to an attempt — `(payload, error)`.

    `attempt` is an `int` by the time it arrives: `parse_argv` matched the
    caller's text against `_DIGITS` and this re-renders the integer, so what
    reaches the argv is the tool's own number and never the caller's string.
    `run_id` is digit-gated by the same function for the same reason.

    `--attempt` returns the pinned attempt's own payload — its `attempt`,
    `status`, `conclusion`, `url` and **its** job list, in the same shape and
    under the same field names as the default read. That is why this op hosts
    the selector rather than a new one: the render below is unchanged, only the
    payload that reaches it moves.
    """
    argv = ["gh", "run", "view", run_id, "--json", _RUN_JSON]
    if attempt is not None:
        argv += ["--attempt", str(attempt)]
    argv += _repo_target.gh_args()
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return None, "ERROR: gh not found — install from https://cli.github.com"
    except subprocess.TimeoutExpired:
        return None, "ERROR: gh timed out"

    if result.returncode != 0:
        return None, _format_error(result.stderr, "Workflow run", run_id)

    try:
        return json.loads(result.stdout), ""
    except json.JSONDecodeError:
        # A block, not a field: an unparseable body keeps its lines, so it is
        # fenced rather than flattened (#1648). `scrub()` inside `fence()`
        # discloses every separator and neutralises the marker shape, so the
        # fence cannot be closed from inside it. Slice first, fence second —
        # the markers are the structure and have to be the outermost thing.
        return None, "\n".join([
            "ERROR: invalid JSON from gh — its body, verbatim, below",
            _untrusted.banner(),
            _untrusted.fence(result.stdout[:500]),
        ])


def main() -> int:
    use_utf8_stdout()
    run_id, attempt, refusal = parse_argv(sys.argv[1:])
    if refusal:
        print(refusal)
        return 1

    # The default read happens first even when an attempt was asked for, and it
    # is the read this op has always made. It is also the only one that knows
    # how many attempts exist — `--attempt` answers for the attempt it was
    # given and 404s past it — so the range check below is made against a
    # number that was read rather than against an error bought from GitHub.
    d, err = fetch_run(run_id, None)
    if err:
        print(err)
        return 1

    latest = latest_attempt(d.get("attempt"))

    if attempt is not None:
        if latest is not None and attempt > latest:
            print(refuse_past_latest(run_id, attempt, latest))
            return 1
        if attempt != latest:
            # `attempt == latest` needs no second call: the payload in hand
            # already IS that attempt. A caller who names the current attempt
            # explicitly gets the same render for the same one round trip.
            pinned, err = fetch_run(run_id, attempt)
            if err:
                # Refused, never half-rendered. The payload still in `d` is the
                # LATEST attempt, and printing it under a header naming the one
                # that was asked for is the substitution this op exists to make
                # impossible.
                print(f"ERROR: attempt {attempt} of run #{run_id} was not "
                      f"read, so nothing about it is rendered. What gh said:")
                print(f"  {err}")
                return 1
            d = pinned

    name = d.get("name", "?")
    status = d.get("status", "?")
    conclusion = d.get("conclusion", "")
    event = d.get("event", "?")
    branch = d.get("headBranch", "?")
    web_url = d.get("url", "")

    raw_jobs = d.get("jobs")
    states = job_states(raw_jobs)
    found_names = ([str(j.get("name") or "?") for j in raw_jobs
                    if isinstance(j, dict)]
                   if isinstance(raw_jobs, list) else [])

    # Reconciliation is bought only when there is a tally to reconcile. An
    # absent job list was not read at all, so a second count would answer a
    # question nobody can ask yet — and it would be a call spent on it.
    #
    # And not on a superseded attempt (#1715). `_declared_legs` counts distinct job
    # names under `filter=all`, which is the union across EVERY attempt, and
    # comparing one attempt's legs against that union manufactures a shortfall.
    # A disclosure that fires when nothing is wrong is one nobody reads, which
    # is the same reason `reconcilable()` already declines on attempt 1.
    superseded = (attempt is not None and latest is not None
                  and attempt < latest)
    declared: int | None = None
    marker, shortfall_lines = "", []
    if states is not None and not superseded:
        declared, missing = declared_legs(
            web_url, run_id, d.get("attempt"), found_names)
        marker, shortfall_lines = _checks.shortfall(
            len(states), declared, missing)

    # The attempt is in the H1 only when one was named, because a render gets
    # quoted on its own into a report and `# Run #N` alone cannot say which
    # attempt it was. The default render says it on the `Attempts:` line below
    # instead, so its header is byte-identical to what it has always printed.
    pinned_note = ""
    if attempt is not None:
        pinned_note = (f" (attempt {attempt} of "
                       f"{latest if latest is not None else '?'})")
    print(f"# Run #{run_id}{pinned_note} — {_untrusted.flat(name)}")
    line = status_line(status, conclusion, states, declared)
    print(f"Status: {line}{' ' + marker if marker else ''}")
    for text in shortfall_lines:
        print(text)
    print(f"Event: {event} | Branch: {_untrusted.flat(branch)}")
    local_check = _local_branch_check(branch)
    if local_check:
        print(local_check)
    if web_url:
        print(f"URL: {web_url}")
    print(attempts_line(run_id, latest, attempt))

    # Jobs
    jobs = raw_jobs if isinstance(raw_jobs, list) else []
    if jobs:
        header = (f"{'Job':<40} {'Job id':<{JOB_ID_COL}} {'Status':<12} "
                  f"{'Conclusion':<12} {'Duration':<10}")
        print(f"\n{header}")
        # Derived, never a literal: a rule frozen at the old width while the
        # header grew reads as a table that has been truncated.
        print("-" * len(header))

        failed: list[tuple[dict, str]] = []
        for job in jobs:
            j_name = job.get("name", "?")
            j_status = job.get("status", "?")
            j_conclusion = job.get("conclusion") or "-"

            steps = job.get("steps", [])
            duration_str = "-"
            if isinstance(steps, list) and steps:
                duration_str = f"{steps_resolved(steps)}/{len(steps)} steps"

            # Membership is the shared predicate, never `== "failure"` (#803):
            # a leg that timed out, was cancelled or is waiting on a human is
            # exactly what a reader skipping to the section below is looking
            # for, and the literal put it in no section at all.
            state = _checks.github_state(job)
            marker = ""
            if _checks.is_red(state):
                marker = " <!"
                failed.append((job, state))

            print(f"{_untrusted.flat(j_name):<40} "
                  f"{job_id_cell(job):<{JOB_ID_COL}} {j_status:<12} "
                  f"{j_conclusion:<12} {duration_str:<10}{marker}")

        if failed:
            breakdown = red_breakdown([s for _, s in failed])
            print(f"\n## Failed jobs ({len(failed)}) — {breakdown}")
            for job, state in failed:
                j_name = job.get("name", "?")
                # The state is named per leg because the heading says
                # "Failed": a CANCELLED leg belongs in this section, and
                # letting it read as a test failure is its own wrong answer.
                print(f"  - {_untrusted.flat(j_name)} ({job_id_cell(job)}) — "
                      f"{_checks.label(state)}")
                names = red_steps(job)
                for step_name in names[:_STEP_CAP]:
                    # The job-name cell above has been flattened since #965;
                    # this line was not. A step name is whatever the branch's
                    # own CI config called it, which for a fork PR is not this
                    # repository's text (#1522).
                    print(f"    step: {_untrusted.flat(step_name)}")
                if len(names) > _STEP_CAP:
                    print(f"    +{len(names) - _STEP_CAP} more")

        # Under everything that printed an id, so it covers the table and the
        # failed-jobs section at once, and inside `if jobs:` so it is never a
        # pointer at ids the reader was not given.
        print(f"\n{job_id_note()}")
    elif isinstance(raw_jobs, list):
        print("\nNo jobs — GitHub reports zero jobs for this run.")
    else:
        print("\nJob list absent from this payload — not zero jobs, unread. "
              "Retry, or count by hand: gh run view <run-id> --json jobs")

    return 0


if __name__ == "__main__":
    sys.exit(main())
