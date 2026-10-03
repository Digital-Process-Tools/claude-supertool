#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from typing import Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  

import _checks  
import _declared_legs  
import _repo_target  
import _branch_locale  
import _untrusted  
import _auth_probe  
import _status_probe  
import _digits  







_FIELD = "run-level field"






_TERMINAL_RUN_STATUS = "completed"






_STEP_CAP = _checks.NAMED_CAP







_DIGITS = _digits.DIGITS




ATTEMPT_PREFIX = "attempt="




_RUN_JSON = ("databaseId,name,status,conclusion,event,headBranch,"
             "createdAt,updatedAt,url,jobs,attempt")


def refuse_run_id(run_id: str) -> str:












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

    return (f"ERROR: gh-run's attempt must be a whole number 1 or greater, and "
            f"got {value!r}.\n"
            f"Nothing was read. GitHub numbers attempts from 1, and this value "
            f"is built into the argv of `gh run view --attempt`, so a token "
            f"that is not an attempt is refused before anything is fetched "
            f"rather than relayed to gh.\n"
            f"Usage: gh-run:RUN_ID:attempt=1")


def refuse_token(token: str) -> str:

    return (f"ERROR: gh-run does not take a {token!r} token.\n"
            f"Nothing was read. Core refused a token this op's cmd could not "
            f"reach until that cmd widened to take every one of them "
            f"(#873/#1715), so the refusal lives here now. Dropping it would "
            f"render the LATEST attempt under a call that asked for something "
            f"else, which is an answer to a question nobody put.\n"
            f"The only token is attempt=N. Usage: gh-run:RUN_ID[:attempt=N]")


def refuse_past_latest(run_id: str, attempt: int, latest: int) -> str:







    plural = "" if latest == 1 else "s"
    tail = f" | earliest: gh-run:{run_id}:attempt=1" if latest > 1 else ""
    return (f"ERROR: run #{run_id} has {latest} attempt{plural}; attempt "
            f"{attempt} does not exist.\n"
            f"Nothing further was read — the count comes from the run payload "
            f"this op already fetched, not from a 404.\n"
            f"Latest: gh-run:{run_id}{tail}")


def refuse_duplicate(first: str, second: str) -> str:

    return (f"ERROR: gh-run was given two attempt tokens, {first!r} then "
            f"{second!r}.\n"
            f"Nothing was read. Taking the last one silently discards the "
            f"first, which is the same wrong answer as dropping an "
            f"unreachable token (#873) with the token still visible in the op "
            f"string.\n"
            f"One attempt per call; two attempts is two ops in the same call.")


def parse_argv(argv: Sequence[str]) -> tuple[str, int | None, str]:








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




            return ("", None, refuse_duplicate(seen, token))
        seen = token
        attempt = int(value)
    return (run_id, attempt, "")


def latest_attempt(field: object) -> int | None:






    try:
        n = int(field)  
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def attempts_line(run_id: str, latest: int | None, showing: int | None) -> str:

















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
















    counts = Counter(
        "failed" if _checks.bucket(s) == "failed" else _checks.label(s)
        for s in states
    )
    ordered = sorted(counts.items(),
                     key=lambda kv: (kv[0] != "failed", -kv[1], kv[0]))
    return ", ".join(f"{n} {lab}" for lab, n in ordered)


def steps_resolved(steps: object) -> int:

















    if not isinstance(steps, list):
        return 0
    n = 0
    for step in steps:
        state = _checks.github_state(step) if isinstance(step, dict) else _checks.UNKNOWN
        if state == _checks.UNKNOWN or _checks.bucket(state) == "pending":
            continue
        n += 1
    return n





JOB_ID_COL = 18


def job_id_cell(job: object) -> str:































    if not isinstance(job, dict):
        return "id unread"
    ident = job.get("databaseId")
    if isinstance(ident, bool) or not isinstance(ident, int):
        return "id unread"
    return f"job #{ident}"


def job_id_note() -> str:





    return ("Job ids: `gh-job:<id>` for one leg's log, `gh-job:<id>:fail` for "
            "just its failing steps. These are Actions **job** ids, not check "
            "run ids — this run's job list holds no check runs, so `gh-job` "
            "resolves every id above in the job namespace.")


def red_steps(job: object) -> list[str]:







    if not isinstance(job, dict) or not isinstance(job.get("steps"), list):
        return []
    return [str(s.get("name", "?")) for s in job["steps"]
            if isinstance(s, dict) and _checks.is_red(_checks.github_state(s))]


def job_states(jobs: object) -> list[str] | None:














    if not isinstance(jobs, list):
        return None
    return [_checks.github_state(j) for j in jobs]


def declared_legs(url: str, run_id: str, attempt: object,
                  found_names: Sequence[str]) -> tuple[int | None, list[str]]:



















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










    return _branch_locale.describe(source)


def _format_error(stderr: str, resource: str, identifier: str) -> str:

    s = stderr.lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return _repo_target.no_repo_error("gh-run:12345")
    if _status_probe.says_not_found(s):
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. "
                f"{_repo_target.not_found_hint()}")








    if _auth_probe.says_not_authenticated(s):
        return f"ERROR: gh CLI not authenticated. Run: gh auth login (verify with: gh auth status)"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes and retry."
    if _status_probe.says_forbidden(s):
        return f"ERROR: permission denied for {resource} #{identifier}. Check repo access (gh auth status)."

    return (f"ERROR: gh failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def fetch_run(run_id: str, attempt: int | None) -> tuple[dict | None, str]:













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
        return None, "ERROR: gh not found — install the GitHub CLI"
    except subprocess.TimeoutExpired:
        return None, "ERROR: gh timed out"

    if result.returncode != 0:
        return None, _format_error(result.stderr, "Workflow run", run_id)

    try:
        return json.loads(result.stdout), ""
    except json.JSONDecodeError:





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



            pinned, err = fetch_run(run_id, attempt)
            if err:




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










    superseded = (attempt is not None and latest is not None
                  and attempt < latest)
    declared: int | None = None
    marker, shortfall_lines = "", []
    if states is not None and not superseded:
        declared, missing = declared_legs(
            web_url, run_id, d.get("attempt"), found_names)
        marker, shortfall_lines = _checks.shortfall(
            len(states), declared, missing)





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


    jobs = raw_jobs if isinstance(raw_jobs, list) else []
    if jobs:
        header = (f"{'Job':<40} {'Job id':<{JOB_ID_COL}} {'Status':<12} "
                  f"{'Conclusion':<12} {'Duration':<10}")
        print(f"\n{header}")


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



                print(f"  - {_untrusted.flat(j_name)} ({job_id_cell(job)}) — "
                      f"{_checks.label(state)}")
                names = red_steps(job)
                for step_name in names[:_STEP_CAP]:




                    print(f"    step: {_untrusted.flat(step_name)}")
                if len(names) > _STEP_CAP:
                    print(f"    +{len(names) - _STEP_CAP} more")




        print(f"\n{job_id_note()}")
    elif isinstance(raw_jobs, list):
        print("\nNo jobs — GitHub reports zero jobs for this run.")
    else:
        print("\nJob list absent from this payload — not zero jobs, unread. "
              "Retry, or count by hand: gh run view <run-id> --json jobs")

    return 0


if __name__ == "__main__":
    sys.exit(main())
