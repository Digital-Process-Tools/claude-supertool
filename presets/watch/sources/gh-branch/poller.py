

































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






ratelimit = _load_watch("watch_gh_branch_ratelimit", "ratelimit.py")








GREEN = branch.GREEN
NOT_GREEN = branch.NOT_GREEN
NO_RUN = branch.NO_RUN
UNKNOWN = branch.UNKNOWN







NO_RUN_STALE = branch.NO_RUN_STALE















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















UNKNOWN_CONFIRM_STREAK = 2


def _snapshot(ref: str) -> tuple[str, str, str, str, str, bool, bool]:




























































    sha, age, err = branch._head_commit(ref)
    if err:
        return "", "", "", "", err, False, False
    runs, err = branch._run_list(ref)
    if err or runs is None:
        return ("", "", sha, "", err or "ERROR: gh run list returned nothing readable",
                False, False)

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
        return "", "", sha, "", repo_err, False, False
    marker, _shortfall = branch._reconcile(repo, selected, fetched)
    scope, _scope_lines, _unresolved = branch.scope_for(
        repo, sha, selected, age_secs=age, grace=branch._GRACE)
    state, sentence = branch.verdict(selected, legs, missing, sha, age,
                                     branch._GRACE, marker, scope=scope)











    has_failed_leg = bool(branch._red_workflows(selected, legs))



    has_unread_jobs = bool(branch._unread_workflows(legs))
    return state, sentence, sha, repo, "", has_failed_leg, has_unread_jobs


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    ref = str(ctx["id"])
    (branch_state, sentence, sha, repo, error, has_failed_leg,
     has_unread_jobs) = _snapshot(ref)

    if error:













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













    if branch_state == NOT_GREEN:
        branch_state = NOT_GREEN_FAILED if has_failed_leg else NOT_GREEN_PENDING

    prev_state = state.get("branch_state", "")
    prev_sha = str(state.get("sha") or "")











    prev_answer_state = state.get("answer_state", prev_state)
    prev_answer_sha = str(state.get("answer_sha") or prev_sha)
    sha_repeated = bool(sha) and sha == prev_sha













    prev_confirmed_runs = prev_state in (
        GREEN, NOT_GREEN, NOT_GREEN_PENDING, NOT_GREEN_FAILED, UNKNOWN)


    prev_no_run_streak = int(state.get("no_run_streak") or 0) if sha_repeated else 0




    raw_is_no_run = branch_state in (NO_RUN, NO_RUN_STALE)









    raw_is_unread_jobs = branch_state == UNKNOWN and has_unread_jobs




    raw_needs_guard = raw_is_no_run or raw_is_unread_jobs













































    if raw_needs_guard and sha_repeated and prev_confirmed_runs:
        confirmed_streak = prev_no_run_streak + 1
        if confirmed_streak < UNKNOWN_CONFIRM_STREAK:
            branch_state = prev_state
            sentence = ""
        elif confirmed_streak == UNKNOWN_CONFIRM_STREAK:
            branch_state = UNKNOWN










            if raw_is_unread_jobs:
                sentence = (
                    f"{UNKNOWN} — a previous poll confirmed runs on "
                    f"{sha[:7]}; {UNKNOWN_CONFIRM_STREAK} consecutive polls "
                    f"for the same commit have now failed to establish a "
                    f"leg count, most recently because the job list did not "
                    f"come back. The job list of a concluded run does not "
                    f"disappear either, so this is read as a fetch that did "
                    f"not answer rather than the run losing its job "
                    f"history. Original reading: {sentence}")
            else:
                sentence = (
                    f"{UNKNOWN} — a previous poll confirmed runs on "
                    f"{sha[:7]}; {UNKNOWN_CONFIRM_STREAK} consecutive polls "
                    f"for the same commit have now come back with an empty "
                    f"run list, most recently. Runs on a concluded commit "
                    f"do not disappear, so this is read as a fetch that did "
                    f"not answer rather than the commit losing its run "
                    f"history. Original reading: {sentence}")






    no_run_streak = (prev_no_run_streak + 1) if (raw_needs_guard and sha_repeated) else 0

    events: list[dict] = []





























    is_recovered_answer = (
        branch_state != UNKNOWN
        and branch_state == prev_answer_state
        and sha == prev_answer_sha
    )
    if (branch_state != prev_state or sha != prev_sha) and not is_recovered_answer:
        key = _EVENT_FOR_STATE.get(branch_state, "unknown")
        ev = {
            "event": key,
            "payload": {"ref": ref, "sha": sha, "sentence": sentence},
            "notify_title": f"{ref} — {branch_state.lower()}",
            "notify_message": sentence,
        }
        if repo:







            ev["repo"] = repo
        events.append(ev)




    if branch_state == UNKNOWN:
        answer_state, answer_sha = prev_answer_state, prev_answer_sha
    else:
        answer_state, answer_sha = branch_state, sha

    new_state = {
        "branch_state": branch_state,
        "sha": sha,
        "ref": ref,
        "lookup": LOOKUP_OK,
        "no_run_streak": no_run_streak,
        "answer_state": answer_state,
        "answer_sha": answer_sha,
    }
    return events, new_state


def is_terminal(state: dict) -> bool:

    return False
