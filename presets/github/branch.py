#!/usr/bin/env python3







































































from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  

import _checks  
import _declared_legs  
import _declared_workflows  
import _repo_target  
import _untrusted  
import _auth_probe  
import _status_probe  





GREEN = "GREEN"
NOT_GREEN = "NOT GREEN"
NO_RUN = "NO RUN"
UNKNOWN = "UNKNOWN"








NO_RUN_STALE = "NO RUN — STALE"





PHASE_CONCLUDED = "concluded"
PHASE_RUNNING = "running"
PHASE_UNESTABLISHED = "unestablished"





_TERMINAL_RUN_STATUS = "completed"






RUN_LIST_LIMIT = 60






JOB_WORKERS = 4

_GRACE = _checks.CHECK_CREATION_GRACE_SECS









NO_RUN_STALE_SECS = 2700






def runs_on_sha(runs: object, sha: str) -> dict:













































    if not isinstance(runs, list) or not sha:
        return {}
    by_id: dict = {}
    order: list = []
    for i, r in enumerate(runs):
        if not isinstance(r, dict) or r.get("headSha") != sha:
            continue
        rid = _run_id(r)


        key = rid if rid >= 0 else ("unreadable", i)
        prev = by_id.get(key)
        if prev is None:
            by_id[key] = r
            order.append(key)
        elif _attempt(r) >= _attempt(prev):
            by_id[key] = r

    counts: dict = {}
    for key in order:
        name = _workflow_name(by_id[key])
        counts[name] = counts.get(name, 0) + 1

    out: dict = {}
    for key in order:
        run = by_id[key]
        name = _workflow_name(run)
        shown = _neutralise_run_tag(name)
        if counts[name] == 1:
            label = shown
        else:
            rid = _run_id(run)
            label = f"{shown} (run {rid if rid >= 0 else '?'})"
        if label in out:
            n = 2
            while f"{label} #{n}" in out:
                n += 1
            label = f"{label} #{n}"
        out[label] = run
    return out


def _workflow_name(run: object) -> str:
    if not isinstance(run, dict):
        return "?"
    return str(run.get("workflowName") or "?")




_RUN_TAG = re.compile(r"\(run (?:[0-9]+|\?)\)")





RUN_TAG_NEUTRALISED = "(run-tag in name, neutralised)"


def _neutralise_run_tag(name: str) -> str:























    return _RUN_TAG.sub(RUN_TAG_NEUTRALISED, name)


def _attempt(run: object) -> int:






    try:
        return int(run.get("attempt"))  
    except (AttributeError, TypeError, ValueError):
        return -1


def workflow_names(selected: dict) -> set:








    return {_workflow_name(r) for r in (selected or {}).values()}


def missing_workflows(prev_names, selected: dict,
                       declared: list | None = None) -> list:























    missing = sorted(set(prev_names or ()) - workflow_names(selected))
    if not declared:
        return missing
    by_name = {w.get("name"): w for w in declared if isinstance(w, dict)}
    return [name for name in missing
            if _declared_workflows.is_push_triggered(
                (by_name.get(name) or {}).get("triggers")) is not False]


def _run_id(run: object) -> int:
    try:
        return int(run.get("databaseId"))  
    except (AttributeError, TypeError, ValueError):
        return -1





_HEX_REF = re.compile(r"^[0-9a-fA-F]{7,40}\Z")  

MODE_BRANCH = "branch"
MODE_COMMIT = "commit"


def ref_mode(ref: str, resolved_sha: str) -> str:




















    ref = str(ref or "").strip()
    sha = str(resolved_sha or "").strip().lower()
    if not ref or not _HEX_REF.match(ref):
        return MODE_BRANCH
    return MODE_COMMIT if sha.startswith(ref.lower()) else MODE_BRANCH


_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _created(run: object):





    if not isinstance(run, dict):
        return None
    try:
        return datetime.strptime(
            str(run.get("createdAt") or "").strip(), _ISO).replace(
                tzinfo=timezone.utc)
    except ValueError:
        return None


def _prev_candidates(runs: object, sha: str) -> list:






    if not isinstance(runs, list):
        return []
    return [r for r in runs if isinstance(r, dict)
            and str(r.get("headSha") or "") not in ("", sha)]


def previous_head(runs: object, sha: str) -> tuple[str, set]:























    others = _prev_candidates(runs, sha)
    if not others:
        return "", set()
    dated = [r for r in others if _created(r) is not None]
    newest = (max(dated, key=lambda r: (_created(r), _run_id(r))) if dated
              else others[0])
    prev = str(newest.get("headSha") or "")
    names = {str(r.get("workflowName") or "?") for r in runs
             if isinstance(r, dict) and str(r.get("headSha") or "") == prev}
    return prev, names





BASIS_TIME = "time"
BASIS_POSITION = "position"
BASIS_NONE = "none"


def previous_head_basis(runs: object, sha: str) -> tuple[str, int, int]:














    others = _prev_candidates(runs, sha)
    if not others:
        return BASIS_NONE, 0, 0
    undated = sum(1 for r in others if _created(r) is None)
    basis = BASIS_POSITION if undated == len(others) else BASIS_TIME
    return basis, undated, len(others)


def previous_head_lines(runs: object, sha: str, prev_sha: str) -> list:











    basis, undated, candidates = previous_head_basis(runs, sha)
    if basis == BASIS_NONE or not undated:
        return []
    short = (prev_sha or "")[:7] or "?"
    if basis == BASIS_POSITION:
        return [f"  Previous head {short} was taken from LIST POSITION, not "
                f"from time: none of the {candidates} runs on other commits "
                f"carried a readable `createdAt` (this op reads `{_ISO}` and "
                "counts every other spelling as undated). That is the "
                "behaviour #1618 removed — GitHub does not guarantee the "
                "listing is newest-first, and on 2026-08-13 it was not — so "
                f"whether {short} is really the previous head is UNKNOWN, and "
                "every line below that names it inherits that."]
    return [f"  {undated} of {candidates} runs on other commits could not be "
            f"dated and were NOT ranked (`createdAt` not in `{_ISO}` form). "
            f"Previous head {short} is the newest of the "
            f"{candidates - undated} that could be dated; an undated run may "
            "be newer than it, so it is the best available answer rather than "
            "an established one."]


def run_phase(run: object) -> str:






    if not isinstance(run, dict):
        return PHASE_UNESTABLISHED
    raw = str(run.get("status") or "").strip().lower()
    if not raw:
        return PHASE_UNESTABLISHED
    return PHASE_CONCLUDED if raw == _TERMINAL_RUN_STATUS else PHASE_RUNNING


def orphaned_legs(run: object, states) -> int:


















    if run_phase(run) != PHASE_CONCLUDED:
        return 0
    return sum(1 for s in (states or []) if _checks.bucket(s) == "pending")


def orphan_lines(selected: dict, fetched: dict) -> list:










    lines = []
    for name in sorted(selected):
        jobs = fetched.get(name)
        if jobs is None:
            continue
        states = [_checks.github_state(j) for j in jobs]
        n = orphaned_legs(selected[name], states)
        if not n:
            continue






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





    return _checks.summarize(states)






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


def no_run_verdict(sha: str, age_secs: object, grace: int = _GRACE,
                    stale_grace: int = NO_RUN_STALE_SECS) -> tuple:

















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
    if int(age_secs) <= stale_grace:
        return (NO_RUN, f"{NO_RUN} — zero workflow runs on {short}, head commit "
                        f"{_duration(age_secs)} old and past the {_window(grace)} "
                        "window in which a first run normally appears. Whether any "
                        "workflow covers this ref is UNKNOWN — a path filter and a "
                        "workflow that never fired look identical from here. "
                        "Check the repo's Actions tab.")
    return (NO_RUN_STALE,
            f"{NO_RUN_STALE} — zero workflow runs on {short}, head commit "
            f"{_duration(age_secs)} old and past the {_window(stale_grace)} "
            "window past which a commit with still nothing recorded is no "
            "longer read as ordinary listing lag or a plausible path filter "
            "— those explanations wear out with time and this one already "
            "has (#2362). Something is wrong with how this commit was "
            "expected to trigger CI, or a webhook delivery was dropped "
            "outright. Check the repo's Actions tab and the merge mechanism "
            "itself.")


def listing_behind_secs(runs: object, age_secs: object):











    if age_secs is None:
        return None
    stamps = [t for t in
              (_created(r) for r in (runs if isinstance(runs, list) else []))
              if t is not None]
    if not stamps:
        return None
    head_at = datetime.now(timezone.utc) - timedelta(seconds=int(age_secs))
    return int((head_at - max(stamps)).total_seconds())


def stale_listing_lines(runs: object, selected: dict, sha: str,
                        age_secs: object, grace: int = _GRACE) -> list:













    if selected or age_secs is None or int(age_secs) > grace:
        return []
    lines = [f"  This listing did not see the head commit at all: it returned "
             f"zero runs on {sha[:7]}, so the line above rests on it having "
             f"caught up. It need not have — on 2026-08-13 `gh run list` "
             f"omitted two runs that had existed for 12 minutes and returned "
             f"them 3 minutes later, unchanged (#1618). Re-ask before acting "
             f"on this."]
    behind = listing_behind_secs(runs, age_secs)
    if behind is not None and behind > 0:
        lines.append(f"  Newest run anywhere in the listing: {_duration(behind)}"
                     f" older than the head commit itself.")
    return lines


def scope_clause(undispatched: list, unestablished: str, n_wf: int, *,
                 waiting: int = 0, grace: int = _GRACE) -> str:




























    if unestablished:






        subject = _agrees(n_wf, "this 1 workflow is",
                          f"these {n_wf} workflows are")
        return (f" The set of workflows declared at this commit is "
                f"UNESTABLISHED ({unestablished}), so whether {subject} "
                f"all of them is UNKNOWN.")
    if not undispatched:
        return ""
    n = len(undispatched)






    shown = [_untrusted.flat(str(w.get("name")))
             for w in undispatched[:_checks.NAMED_CAP]]
    names = ", ".join(f"`{s}`" for s in shown)
    if n > _checks.NAMED_CAP:
        names += f", +{n - _checks.NAMED_CAP} more"



    tail = ""
    if waiting:
        tail = (f" {waiting} of {n} {_agrees(waiting, 'is', 'are')} still "
                f"inside the {_window(grace)} creation window and "
                f"{_agrees(waiting, 'is', 'are')} expected to run.")
    return (f" This covers the {n_wf} "
            f"{_agrees(n_wf, 'workflow', 'workflows')} that produced a run; "
            f"{n} declared in {_declared_workflows.WORKFLOW_DIR} at this commit "
            f"produced none and {_agrees(n, 'is', 'are')} NOT covered: "
            f"{names}.{tail}")


def scope_for(repo: str, sha: str, selected: dict, *,
              declared_pair: tuple | None = None,
              age_secs: object = None, grace: int = _GRACE) -> tuple[str, list[str], str]:














































    if not selected:
        return "", [], ""
    if declared_pair is not None:


        declared, why = declared_pair
    else:
        owner, name = _declared_legs.owner_repo(repo)
        declared, why = _declared_workflows.declared_at(owner, name, sha)
    if declared is None:
        n_wf = len(workflow_names(selected))
        return (
            scope_clause([], why, n_wf),
            [f"Declared workflow set at {sha[:7]}: UNESTABLISHED — {why}. The "
             f"verdict above covers the {n_wf} workflow(s) that "
             f"produced a run and cannot say whether that is all of them."],
            f"the declared workflow set at {sha[:7]} is UNESTABLISHED")
    present = workflow_names(selected)
    undispatched = [w for w in declared if w.get("name") not in present]
    loud = [w for w in undispatched
            if _declared_workflows.is_push_triggered(w.get("triggers"))
            is not False]
    still_open = [w for w in loud if not _waiting_on_first_run(w, age_secs, grace)]
    unresolved = ""
    if still_open:
        unresolved = (f"{len(still_open)} declared workflow(s) a push should reach "
                      f"produced no run on {sha[:7]}")




    return (scope_clause(undispatched, "", len(present),
                         waiting=len(loud) - len(still_open), grace=grace),
            undispatched_lines(undispatched, age_secs, grace), unresolved)


def _waiting_on_first_run(wf: dict, age_secs: object, grace: int) -> bool:







    return (wf.get("triggers") is not None
            and age_secs is not None and int(age_secs) <= grace)


def undispatched_lines(undispatched: list, age_secs: object = None,
                       grace: int = _GRACE) -> list[str]:


















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
        elif _waiting_on_first_run(wf, age_secs, grace):
            said = (f"triggers: {', '.join(_untrusted.flat(str(t)) for t in triggers)} "
                    f"— a push trigger IS declared; no run yet, and the head "
                    f"commit is {_duration(age_secs)} old, inside the "
                    f"{_window(grace)} window in which a first run has always "
                    f"appeared, so one is still expected")
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


def _unread_workflows(legs: dict) -> list:









    return sorted(n for n, v in legs.items() if v is None)


def _red_workflows(selected: dict, legs: dict) -> list:















    return sorted(n for n, states in legs.items()
                  if any(_checks.is_red(s) for s in (states or []))
                  or _checks.is_red(_run_conclusion(selected[n])))


def verdict(selected: dict, legs: dict, missing, sha: str,
            age_secs: object, grace: int = _GRACE,
            unreconciled: str = "", *, scope: str) -> tuple:


































    if not selected:
        return no_run_verdict(sha, age_secs, grace)

    short = sha[:7] if sha else "?"

    unread = _unread_workflows(legs)
    if unread:
        return (UNKNOWN, f"{UNKNOWN} — the job "
                         f"{_agrees(len(unread), 'list', 'lists')} for "
                         f"{_names(unread)} did "
                         f"not come back, so how many legs ran on {short} is "
                         "UNKNOWN and nothing here establishes green. Re-run "
                         "the op; if it persists, count by hand with "
                         "`gh run view <run-id> --json jobs`.")

    red_wfs = _red_workflows(selected, legs)
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
    n_runs = len(selected)
    if unreconciled:
        return (UNKNOWN, f"{UNKNOWN} — every one of the {n_legs} legs read on "
                         f"{short} passed, but the tally could not be squared "
                         f"with what the runs declare ({unreconciled}), so "
                         "whether these are all of the legs is UNKNOWN. "
                         "Detailed below. Nothing here has failed.")







    return (GREEN, f"{GREEN} — every run on {short} concluded and every "
                   f"leg passed ({n_legs} legs across {n_runs} "
                   f"{_agrees(n_runs, 'run', 'runs')}).{scope}")


def _run_conclusion(run: object) -> str:
    if not isinstance(run, dict):
        return _checks.UNKNOWN
    raw = str(run.get("conclusion") or "").strip()
    if raw:
        return _checks.normalize(raw)

    return "PENDING"


def _names(names) -> str:





















    items = [f"`{_untrusted.flat(str(n))}`" for n in names]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _agrees(n: int, singular: str, plural: str) -> str:













    return singular if n == 1 else plural






def _gh(argv: list, timeout: int = 20):
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _format_error(stderr: str, what: str, commit: bool = False) -> str:
    s = (stderr or "").lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return _repo_target.no_repo_error("gh-branch:master")




    if (_status_probe.says_not_found(s)
            or "422" in s or "no commit found" in s):



        target = _repo_target.target()
        where = f" (gh repo view {target})" if target else ""











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





    if _auth_probe.says_not_authenticated(s):
        return "ERROR: gh CLI not authenticated. Run: gh auth login"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes."
    if _status_probe.says_forbidden(s):
        return f"ERROR: permission denied for {what}. Check repo access."

    return (f"ERROR: gh failed for {what}: "
            f"{_untrusted.flat((stderr or '').strip())}")


def _repo_identity():






    target = _repo_target.target()
    argv = ["gh", "repo", "view"] + ([target] if target else []) + \
        ["--json", "nameWithOwner,defaultBranchRef"]
    try:
        r = _gh(argv)
    except FileNotFoundError:
        return "", "", "ERROR: gh not found — install the GitHub CLI"
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












    try:
        r = _gh(["gh", "api", _repo_target.api_path(f"commits/{ref}")])
    except FileNotFoundError:
        return "", None, "ERROR: gh not found — install the GitHub CLI"
    except subprocess.TimeoutExpired:
        return "", None, f"ERROR: gh timed out resolving ref {ref!r}"
    if r.returncode != 0:



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









    selector = ["--commit", sha] if sha else ["--branch", ref]
    try:
        r = _gh(["gh", "run", "list", *selector, "--limit",
                 str(RUN_LIST_LIMIT), "--json",
                 "workflowName,headSha,databaseId,status,conclusion,event,"
                 "createdAt,attempt"] + _repo_target.gh_args())
    except FileNotFoundError:
        return None, "ERROR: gh not found — install the GitHub CLI"
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





        missing.extend(_untrusted.flat(f"{wf} / {n}") for n in
                       _declared_legs.missing_names(names, found))
    if not found_total and declared_total == 0:
        return ("", [])
    return _checks.shortfall(found_total, declared_total, missing)










RUN_COL = 26
PHASE_COL = 14


def run_cell(run: object) -> str:













    rid = _run_id(run)
    ident = str(rid) if rid >= 0 else "id ?"
    attempt = "?"
    if isinstance(run, dict):
        try:
            attempt = str(int(run.get("attempt")))  
        except (TypeError, ValueError):
            attempt = "?"
    return f"{ident} attempt {attempt}"


def table_header() -> str:






    return (f"{'Workflow':<32} {'Run':<{RUN_COL}} {'Phase':<{PHASE_COL}} "
            f"{'Outcome':<14} Legs")


def run_id_note() -> str:








    return ("Run ids: `gh-run:<id>` for one run's legs, `gh run rerun <id>` to "
            "retry it. `attempt N` is GitHub's `run_attempt` — N > 1 means the "
            "run was re-run and the tally beside it counts the latest attempt "
            "only, so legs from an earlier attempt are NOT in it. EVERY run on "
            "this commit is listed, so the row count is runs, never attempts — "
            "one workflow can have two runs on one commit (GitHub's default "
            "code scanning emits two per push), and both then carry `(run "
            "<id>)` in the first column and both must pass (#1640). That "
            "`(run <id>)` is supertool's, not the workflow's: a workflow name "
            f"containing the same shape reads `{RUN_TAG_NEUTRALISED}` instead, "
            "so a name chosen by whoever writes the workflow files cannot "
            "arrive as this annotation (#1687).")


def _row(name: str, run: dict, jobs) -> str:







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




        if orphaned_legs(run, states):
            outcome += " ⚠"
    elif phase == PHASE_RUNNING:
        outcome = "not yet"
    else:
        outcome = "not read"
    return (f"{name:<32} {run_cell(run):<{RUN_COL}} {phase:<{PHASE_COL}} "
            f"{outcome:<14} {tally}")


def main() -> int:
    use_utf8_stdout()
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

    selected = runs_on_sha(runs, sha)
    if mode == MODE_COMMIT:





        prev_sha, prev_names = "", set()
    else:
        prev_sha, prev_names = previous_head(runs, sha)






    declared_pair = (None, "")
    if selected and mode != MODE_COMMIT:
        owner, repo_name = _declared_legs.owner_repo(repo)
        declared_pair = _declared_workflows.declared_at(owner, repo_name, sha)
    missing = missing_workflows(prev_names, selected, declared_pair[0])

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




    scope, scope_lines, _unresolved = scope_for(
        repo, sha, selected,
        declared_pair=declared_pair if mode != MODE_COMMIT else None,
        age_secs=age, grace=_GRACE)
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




    basis_lines = ([] if mode == MODE_COMMIT
                   else previous_head_lines(runs, sha, prev_sha))
    if basis_lines:
        print()
        for line in basis_lines:
            print(line)

    if missing:
        print()
        print(f"Workflows with a run on the previous head {prev_sha[:7]} and "
              f"none returned on {sha[:7]}:")
        for name in missing:
            print(f"  {_untrusted.flat(name)} — no run for it was RETURNED on "
                  "this commit. That is a different sentence from 'ran and "
                  "passed', and a different one again from 'did not run': an "
                  "empty listing is an absence produced by this query, not an "
                  "observed one (#1618). Whether a path filter excluded it, a "
                  "run is still to be created, or the listing has not caught "
                  "up is UNKNOWN.")
        for line in stale_listing_lines(runs, selected, sha, age):
            print(line)










    return 0


if __name__ == "__main__":
    sys.exit(main())
