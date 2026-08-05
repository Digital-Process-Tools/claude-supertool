#!/usr/bin/env python3
"""GitHub Actions workflow run details via gh CLI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _checks  # noqa: E402  (the one check tally, shared with gh-pr / gh-prs)
import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)

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


def status_line(run_status: object, conclusion: object,
                states: list[str] | None) -> str:
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
    *unread*.
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
    """Return a one-line local-branch-vs-source check for output.

    Empty string when not in a git repo, detached HEAD, or source is empty.
    """
    if not source or source == "?":
        return ""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            return ""
        local = r.stdout.strip()
        if not local or local == "HEAD":
            return ""
        if local == source:
            return f"You are on: {local} ✓"
        return f"You are on: {local} ⚠ MISMATCH — switch with: ./supertool 'git-checkout:{source}'"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


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
    return f"ERROR: gh failed for {resource} #{identifier}: {stderr.strip()}"


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: run.py RUN_ID")
        return 1

    run_id = sys.argv[1]

    # Fetch run metadata
    try:
        result = subprocess.run(
            ["gh", "run", "view", run_id, "--json",
             "databaseId,name,status,conclusion,event,headBranch,"
             "createdAt,updatedAt,url,jobs"] + _repo_target.gh_args(),
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("ERROR: gh not found — install from https://cli.github.com")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: gh timed out")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "Workflow run", run_id))
        return 1

    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: invalid JSON from gh\n{result.stdout[:500]}")
        return 1

    name = d.get("name", "?")
    status = d.get("status", "?")
    conclusion = d.get("conclusion", "")
    event = d.get("event", "?")
    branch = d.get("headBranch", "?")
    web_url = d.get("url", "")

    print(f"# Run #{run_id} — {name}")
    print(f"Status: {status_line(status, conclusion, job_states(d.get('jobs')))}")
    print(f"Event: {event} | Branch: {branch}")
    local_check = _local_branch_check(branch)
    if local_check:
        print(local_check)
    if web_url:
        print(f"URL: {web_url}")

    # Jobs
    raw_jobs = d.get("jobs")
    jobs = raw_jobs if isinstance(raw_jobs, list) else []
    if jobs:
        print(f"\n{'Job':<40} {'Status':<12} {'Conclusion':<12} {'Duration':<10}")
        print("-" * 74)

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

            print(f"{j_name:<40} {j_status:<12} {j_conclusion:<12} {duration_str:<10}{marker}")

        if failed:
            breakdown = red_breakdown([s for _, s in failed])
            print(f"\n## Failed jobs ({len(failed)}) — {breakdown}")
            for job, state in failed:
                j_name = job.get("name", "?")
                j_id = job.get("databaseId", "?")
                # The state is named per leg because the heading says
                # "Failed": a CANCELLED leg belongs in this section, and
                # letting it read as a test failure is its own wrong answer.
                print(f"  - {j_name} (job #{j_id}) — {_checks.label(state)}")
                names = red_steps(job)
                for step_name in names[:_STEP_CAP]:
                    print(f"    step: {step_name}")
                if len(names) > _STEP_CAP:
                    print(f"    +{len(names) - _STEP_CAP} more")
    elif isinstance(raw_jobs, list):
        print("\nNo jobs — GitHub reports zero jobs for this run.")
    else:
        print("\nJob list absent from this payload — not zero jobs, unread. "
              "Retry, or count by hand: gh run view <run-id> --json jobs")

    return 0


if __name__ == "__main__":
    sys.exit(main())
