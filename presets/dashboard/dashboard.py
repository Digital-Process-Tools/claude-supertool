#!/usr/bin/env python3























































from __future__ import annotations

import concurrent.futures as _futures
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRESETS = os.path.dirname(_HERE)
sys.path.insert(0, _PRESETS)
from _console import use_utf8_stdout  

import _checks  
import _git_run  
import _pr_board  
import _untrusted  


def _sibling(preset: str, name: str, alias: str):








    path = os.path.join(_PRESETS, preset, f"{name}.py")
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:  
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    saved = sys.path[:]
    sys.path.insert(0, os.path.join(_PRESETS, preset))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    return mod


_gh_pr = _sibling("github", "pr", "dashboard_gh_pr")
_gh_branch = _sibling("github", "branch", "dashboard_gh_branch")
_worktrees = _sibling("git", "worktrees", "dashboard_git_worktrees")




MERGE = "MERGE"
RED = "RED"
WAITING = "WAITING"
REBASE = "REBASE"
DRAFT = "DRAFT"
UNKNOWN = "UNKNOWN"



VERDICT_ORDER = (UNKNOWN, RED, REBASE, WAITING, DRAFT, MERGE)

LANE_OCCUPIED = "occupied"
LANE_FREE = "free"
LANE_UNKNOWN = "unknown"



















LANE_PREFIX_ENV = "SUPERTOOL_LANE_PREFIX"

NO_LANE_PREFIX = (
    "no lane vocabulary configured, so the lane board is not built rather than "
    "built from a guess. Add ops.dashboard.lane_prefix to .supertool.json — "
    'e.g. {"ops": {"dashboard": {"lane_prefix": "lane-"}}} where the labels are '
    "lane-watch, lane-release. There is no default on purpose: a prefix nobody "
    "chose would select zero labels and print as a lane board with nothing on it"
)




LIVE_WORKTREE_STATES = (_worktrees.STATE_OCCUPIED, _worktrees.STATE_UNKNOWN)





BUDGET_DEFAULT = 90




WORKERS_DEFAULT = 8

_ISSUE_IN_BRANCH = re.compile(r"(?<![0-9])([0-9]{2,6})(?![0-9])")


def _positive_int(raw: "str | None", default: int) -> int:




    try:
        value = int(str(raw if raw is not None else "").strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default








class PullRequest:
    __slots__ = ("number", "branch", "title", "states", "tally_marker",
                 "mergeable", "merge_state", "draft", "lanes", "red_ref")

    def __init__(self, number, branch, title, states, tally_marker="",
                 mergeable="", merge_state="", draft=False, lanes=None,
                 red_ref=None):
        self.number = number
        self.branch = branch
        self.title = title


        self.states = states
        self.tally_marker = tally_marker
        self.mergeable = mergeable
        self.merge_state = merge_state
        self.draft = draft
        self.lanes = list(lanes or [])

        self.red_ref = red_ref


class Worktree:
    __slots__ = ("path", "branch", "state", "lanes")

    def __init__(self, path, branch, state, lanes=None):
        self.path = path
        self.branch = branch
        self.state = state
        self.lanes = list(lanes or [])


class Section:






    __slots__ = ("name", "lines", "error", "warning")

    def __init__(self, name, lines=None, error="", warning=""):
        self.name = name
        self.lines = list(lines or [])
        self.error = error



        self.warning = warning

    @property
    def unread(self) -> bool:
        return bool(self.error)


class Report:
    __slots__ = ("repo", "sections", "prs", "lanes", "lane_prefix")

    def __init__(self, repo, sections=None, prs=None, lanes=None,
                 lane_prefix=""):
        self.repo = repo
        self.sections = dict(sections or {})
        self.prs = list(prs or [])



        self.lanes = lanes

        self.lane_prefix = lane_prefix




def pr_verdict(pr: PullRequest) -> tuple:







    if pr.states is None:
        return (UNKNOWN, "the check rollup did not come back — nothing about "
                         "this PR's CI is established, so it is not a merge signal")

    if pr.tally_marker:
        return (UNKNOWN, f"the tally does not sum — {pr.tally_marker}. The legs "
                         "that were read say nothing about the ones that were not")

    if not pr.states:
        return (UNKNOWN, _checks.NO_CHECKS + " — nothing has passed, so this is "
                                             "not green, it is unestablished")

    buckets = [_checks.bucket(s) for s in pr.states]

    if "failed" in buckets:
        n = buckets.count("failed")
        return (RED, f"{n} of {len(buckets)} legs failed")

    others = [_checks.normalize(s) for s, b in zip(pr.states, buckets)
              if b == "other"]
    if others:
        named = ", ".join(f"{c} {_checks.label(s)}" for s, c in
                          sorted({s: others.count(s) for s in others}.items()))
        return (UNKNOWN, f"{named} — neither a pass nor a failure, so whether "
                         "this commit is covered is UNKNOWN")

    if "pending" in buckets:
        n = buckets.count("pending")
        return (WAITING, f"{n} of {len(buckets)} legs still moving")



    if not _checks.all_green(pr.states):  
        return (UNKNOWN, "the tally is green by count but not by identity")

    if pr.draft:
        return (DRAFT, "green, but the PR is a draft — not offered for merge")

    mergeable = _checks.normalize(pr.mergeable)
    state = _checks.normalize(pr.merge_state)

    if mergeable == "CONFLICTING" or state == "DIRTY":
        return (REBASE, "green, but the branch conflicts with the base")
    if state == "BEHIND":
        return (REBASE, "green, but the branch is behind the base")
    if mergeable == "MERGEABLE" and state in ("CLEAN", "HAS_HOOKS"):
        return (MERGE, f"{len(pr.states)} of {len(pr.states)} legs passed, "
                       "mergeable, no conflicts")
    if state == "BLOCKED":
        return (WAITING, "green and conflict-free, but GitHub reports the merge "
                         "BLOCKED — a required review or ruleset is outstanding")
    return (UNKNOWN, f"green, but mergeability is {mergeable}/{state} — not a "
                     "state this op will read as mergeable")




def read_lane_prefix(raw=None):









    raw = os.environ.get("SUPERTOOL_LANE_PREFIX", "") if raw is None else raw
    raw = str(raw).strip()
    if not raw:
        return None, NO_LANE_PREFIX
    return raw, ""


def _lane_stem(prefix: str) -> str:

    return re.sub(r"[^0-9A-Za-z]+$", "", str(prefix))


def select_lane_universe(labels, prefix: str):








    if not isinstance(labels, list):
        return None, [], "gh label list did not return a list"
    names = [str(item.get("name")) for item in labels if isinstance(item, dict)]
    lanes = sorted(name for name in names if name.startswith(prefix))
    stem = _lane_stem(prefix)
    near = sorted(name for name in names
                  if stem and not name.startswith(prefix)
                  and name.startswith(stem) and len(name) > len(stem)
                  and not name[len(stem)].isalnum())
    return lanes, near, ""


def lane_universe_note(prefix: str, lanes, near) -> str:








    if lanes:
        return ""
    if near:
        shown = ", ".join(_untrusted.flat(name) for name in near[:4])
        more = f", +{len(near) - 4} more" if len(near) > 4 else ""
        other = near[0][:len(_lane_stem(prefix)) + 1]
        return (f"no label matches {prefix!r}, so the lane universe is empty and "
                f"no lane can be reported free. {len(near)} label(s) share its "
                f"stem behind a different separator ({shown}{more}) — if the "
                f"vocabulary here is {other!r}, set ops.dashboard.lane_prefix to "
                "it; nothing is assumed on your behalf")
    return (f"no label matches {prefix!r}, and nothing resembling it exists "
            "either. Either this repository declares no lanes — which is fine — "
            "or the prefix belongs to a different repository. Nothing here "
            "distinguishes those, so no lane is reported free")


def render_lane_refusal(complaint: str) -> Section:

    return Section("lanes", error=complaint)


def stray_worktrees(worktrees, default_branch: str = "") -> list:






    return [w for w in worktrees
            if w.state in LIVE_WORKTREE_STATES and not w.lanes
            and not (default_branch and w.branch == default_branch)]


def lane_states(lanes, prs, worktrees, default_branch: str = ""):

















    if lanes is None:
        return None

    stray = stray_worktrees(worktrees, default_branch)

    out = {}
    for lane in lanes:
        hard: list = []
        soft: list = []
        for pr in prs:
            if lane in pr.lanes:
                hard.append(f"#{pr.number} open ({_untrusted.flat(str(pr.branch))})")
        for wt in worktrees:
            if lane not in wt.lanes:
                continue
            where = _untrusted.flat(str(wt.path))
            if wt.state == _worktrees.STATE_OCCUPIED:
                hard.append(f"{where} occupied")
            elif wt.state == _worktrees.STATE_UNKNOWN:
                soft.append(f"{where} cannot tell — undecidable, so this lane "
                            "declines rather than reporting itself free")

        if hard:
            out[lane] = (LANE_OCCUPIED, hard + soft)
        elif soft:
            out[lane] = (LANE_UNKNOWN, soft)
        elif stray:
            out[lane] = (LANE_UNKNOWN, [
                f"nothing points here, but {len(stray)} live worktree(s) could "
                "not be placed in any lane (named above) — one of them could be "
                "this one, so 'free' is not claimed"
            ])
        else:
            out[lane] = (LANE_FREE, ["no open PR and no live worktree points here"])
    return out




def next_action(report: Report) -> str:







    board = report.sections.get("board")
    if board is None or board.unread:
        why = board.error if board is not None else "the board section never ran"
        return (f"next: UNKNOWN — the board could not be read ({why}), so nothing "
                "is claimed about what to do next")

    graded = [(pr, pr_verdict(pr)[0]) for pr in report.prs]

    ready = [pr for pr, word in graded if word == MERGE]
    if len(ready) == 1:
        return (f"next: 1 PR is ready — review and merge it: "
                f"gh-pr:{ready[0].number}:diff")
    if len(ready) > 1:
        numbers = ", ".join(f"#{pr.number}" for pr in ready)
        return (f"next: {len(ready)} PRs are equally ready ({numbers}) — no single "
                "highest-value action; take them in number order, starting with "
                f"gh-pr:{ready[0].number}:diff")

    reds = [pr for pr, word in graded if word == RED]
    if reds:
        pr = reds[0]
        if pr.red_ref and pr.red_ref[0] == "job":
            probe = f"gh-job:{pr.red_ref[1]}:fail"
        elif pr.red_ref and pr.red_ref[0] == "check":
            probe = f"gh-check:{pr.red_ref[1]}"
        else:
            probe = f"gh-pr:{pr.number}"
        return (f"next: nothing is ready to merge; #{pr.number} is red and a red "
                f"blocks its lane — read it: {probe}")

    unsure = [pr for pr, word in graded if word == UNKNOWN]
    if unsure:
        return (f"next: nothing is ready to merge, and #{unsure[0].number} is "
                "UNKNOWN — resolve the doubt before anything else: "
                f"gh-pr:{unsure[0].number}:status")

    prefix = report.lane_prefix
    free = sorted(lane for lane, (state, _e) in (report.lanes or {}).items()
                  if state == LANE_FREE)
    if free:
        shown = ", ".join(name[len(prefix or ""):] for name in free)
        return (f"next: nothing ready on the board — {len(free)} lane(s) free "
                f"({shown}); pick work for one: gh-issues:label={free[0]}")
    if prefix is None:
        return ("next: nothing ready on the board, and the lane vocabulary is "
                "unconfigured (ops.dashboard.lane_prefix), so whether a lane is "
                "free is UNKNOWN")
    if report.lanes is None:
        return ("next: nothing ready on the board, and the lane board is unread, "
                "so whether a lane is free is UNKNOWN")
    if not report.lanes:
        return ("next: nothing ready on the board, and the lane universe is "
                f"empty — no label matches {prefix!r}, so no lane can be "
                "reported free")
    return "next: nothing ready — no PR is mergeable and no lane is free"




def render(report: Report) -> str:

    out = [f"# dashboard — {_untrusted.flat(str(report.repo))}", ""]

    unread = 0
    degraded = 0
    for name, section in report.sections.items():
        out.append(name)
        if section.unread:
            unread += 1
            out.append(f"  !! unread — {_untrusted.flat(str(section.error))}")
            out.append("  this section is missing, not empty; nothing about it "
                       "is claimed below")
        elif section.warning:
            degraded += 1
            out.append(f"  !! degraded — {_untrusted.flat(str(section.warning))}")
            out.extend(f"  {line}" for line in section.lines)
        elif not section.lines:
            out.append("  (none)")
        else:
            out.extend(f"  {line}" for line in section.lines)
        out.append("")

    out.append(next_action(report))
    out.append("")

    tally = {word: 0 for word in VERDICT_ORDER}
    for pr in report.prs:
        tally[pr_verdict(pr)[0]] = tally.get(pr_verdict(pr)[0], 0) + 1
    board = ", ".join(f"{tally[w]} {w}" for w in VERDICT_ORDER if tally.get(w))

    if report.lane_prefix is None:
        lanes = "lanes UNCONFIGURED (ops.dashboard.lane_prefix)"
    elif report.lanes is None:
        lanes = "lanes UNREAD"
    elif not report.lanes:
        lanes = f"lane universe EMPTY — no label matches {report.lane_prefix!r}"
    else:
        counts = {LANE_FREE: 0, LANE_OCCUPIED: 0, LANE_UNKNOWN: 0}
        for state, _evidence in report.lanes.values():
            counts[state] = counts.get(state, 0) + 1
        lanes = (f"{counts[LANE_FREE]} lanes free, {counts[LANE_OCCUPIED]} "
                 f"occupied, {counts[LANE_UNKNOWN]} unknown")

    noun = "section" if unread == 1 else "sections"
    extra = f", {degraded} degraded" if degraded else ""
    out.append(f"[result] dashboard: {board or 'no open PRs'} · {lanes} · "
               f"{unread} {noun} unread{extra}")
    return "\n".join(out)




def _run(argv: list, timeout: int = 30):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def _json_cmd(argv: list, timeout: int = 30):

    try:
        res = _run(argv, timeout=timeout)
    except FileNotFoundError:
        return None, f"{argv[0]} not found on PATH"
    except subprocess.TimeoutExpired:
        return None, f"{' '.join(argv[:3])} timed out after {timeout}s"
    except OSError as exc:
        return None, f"{' '.join(argv[:3])} failed: {exc}"
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        return None, (f"{' '.join(argv[:3])} exited {res.returncode}: "
                      f"{detail[0] if detail else 'no output'}")
    try:
        return json.loads(res.stdout or "null"), ""
    except json.JSONDecodeError:
        return None, f"{' '.join(argv[:3])} returned unparseable JSON"







_GIT_TIMEOUT_DEFAULT = 15


def _git(args: list, timeout: int | None = None):




















    budget = _git_run.git_timeout(_GIT_TIMEOUT_DEFAULT) if timeout is None else timeout
    try:
        res = _git_run._git(args, timeout=budget)
    except (FileNotFoundError, OSError) as exc:
        return "", f"git {' '.join(args[:2])}: {exc}"
    if res.returncode != 0:
        return "", (f"git {' '.join(args[:2])} exited {res.returncode}: "
                    f"{(res.stderr or '').strip().splitlines()[:1] or ['no output']}")
    return res.stdout.strip(), ""




def collect_local(default_branch: str) -> Section:






    head, err = _git(["rev-parse", "--short", "HEAD"])
    if err:
        return Section("local", error=err)
    branch, _e = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    lines = [f"branch {_untrusted.flat(branch or '?')} @ {head}"]

    if not default_branch:
        lines.append("clone currency: UNKNOWN — the repository's default branch "
                     "was not established, so there is nothing to compare against")
        return Section("local", lines)

    ref = f"origin/{default_branch}"
    local_sha, local_err = _git(["rev-parse", ref])
    remote_out, remote_err = _git(["ls-remote", "origin",
                                   f"refs/heads/{default_branch}"])
    remote_sha = remote_out.split()[0] if remote_out.split() else ""

    if local_err or not remote_sha:
        lines.append(f"clone currency: UNKNOWN — {local_err or remote_err or 'the remote ref did not come back'}")
        return Section("local", lines)

    if local_sha == remote_sha:
        lines.append(f"{ref} {local_sha[:7]} — matches the remote, clone is current")
    else:
        lines.append(f"{ref} {local_sha[:7]} is STALE — the remote is at "
                     f"{remote_sha[:7]}. Refresh before acting on anything below")

    counts, count_err = _git(["rev-list", "--left-right", "--count",
                              f"{ref}...HEAD"])
    parts = counts.split()
    if len(parts) == 2:
        lines.append(f"HEAD is {parts[1]} ahead / {parts[0]} behind {ref}")
    else:
        lines.append(f"ahead/behind: UNKNOWN — {count_err or 'unreadable'}")
    return Section("local", lines)




def collect_default(repo: str, default_branch: str) -> Section:






    if not default_branch:
        return Section("default", error="the repository's default branch was "
                                        "not established")






    sha, age, runs, err = _pr_board.head_and_runs(_gh_branch, default_branch)
    if err:
        return Section("default", error=err)

    selected = _gh_branch.runs_on_sha(runs, sha)
    if not selected:
        state, sentence = _gh_branch.no_run_verdict(sha, age)
        return Section("default", [f"{default_branch} {sha[:7]}", sentence])

    fetched = {wf: _gh_branch._jobs_for(_gh_branch._run_id(run))
               for wf, run in selected.items()}
    legs = {wf: (None if jobs is None
                 else [_checks.github_state(j) for j in jobs])
            for wf, jobs in fetched.items()}
    marker, detail = _gh_branch._reconcile(repo, selected, fetched)
    _prev_sha, prev_names = _gh_branch.previous_head(runs, sha)





    owner, name = _gh_branch._declared_legs.owner_repo(repo)
    declared_pair = _gh_branch._declared_workflows.declared_at(owner, name, sha)



    missing = _gh_branch.missing_workflows(prev_names, selected, declared_pair[0])




    scope, scope_lines, _unresolved = _gh_branch.scope_for(
        repo, sha, selected, declared_pair=declared_pair,
        age_secs=age, grace=_gh_branch._GRACE)
    state, sentence = _gh_branch.verdict(selected, legs, missing, sha, age,
                                         unreconciled=marker, scope=scope)

    read = [s for group in legs.values() if group is not None for s in group]
    lines = [f"{default_branch} {sha[:7]} — {sentence}",
             f"legs: {_checks.summarize(read)}"]
    if marker:
        lines.append(marker)
        lines.extend(line.strip() for line in detail)
    lines.extend(line.strip() for line in scope_lines)
    return Section("default", lines)




_PR_FIELDS = ("number,headRefName,title,url,headRefOid,mergeable,"
              "mergeStateStatus,isDraft,statusCheckRollup,body,labels")


def _red_ref(rollup) -> object:







    for _name, state, kind, ident in _checks.github_named_live(rollup):
        if _checks.bucket(state) == "failed" and kind and ident:
            return (kind, ident)
    return None


def _build_pr(payload: dict, issue_lanes: dict, prefix: str) -> PullRequest:
    rollup = payload.get("statusCheckRollup")





    states = (_checks.github_live_states(rollup)
              if isinstance(rollup, list) else None)
    marker, _lines = _gh_pr._reconcile_checks(payload)

    lanes = {label.get("name") for label in (payload.get("labels") or [])
             if isinstance(label, dict)
             and str(label.get("name") or "").startswith(prefix)}
    for ref in _checks.closing_issue_refs(payload.get("body")):
        if ref.startswith("#"):
            lanes.update(issue_lanes.get(ref[1:], ()))
    lanes.update(issue_lanes.get(str(payload.get("number")), ()))

    return PullRequest(
        number=payload.get("number"),
        branch=str(payload.get("headRefName") or "?"),
        title=str(payload.get("title") or ""),
        states=states,
        tally_marker=marker,
        mergeable=str(payload.get("mergeable") or ""),
        merge_state=str(payload.get("mergeStateStatus") or ""),
        draft=bool(payload.get("isDraft")),
        lanes=sorted(lanes),
        red_ref=_red_ref(rollup),
    )


def collect_board(issue_lanes: dict, workers: int, prefix: str = ""):











    data, err, _rc, _raw = _pr_board.run_pr_list(
        ["gh", "pr", "list", "--state", "open", "--limit", "50", "--json",
         _PR_FIELDS], timeout=60)
    if err:
        return Section("board", error=err), []
    if not data:
        return Section("board", ["no open PRs"]), []

    with _futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        prs = list(pool.map(lambda d: _build_pr(d, issue_lanes, prefix), data))

    prs.sort(key=lambda p: (VERDICT_ORDER.index(pr_verdict(p)[0]),
                            -int(p.number or 0)))
    lines = []
    for pr in prs:
        word, why = pr_verdict(pr)
        lanes = " ".join(name[len(prefix):] for name in pr.lanes) or "lane ?"
        lines.append(f"{word:<8} #{pr.number:<5} "
                     f"{_untrusted.flat(pr.branch):<20} {lanes:<14} {why}")
    return Section("board", lines), prs




def _branch_lanes(branch: str, issue_lanes: dict, pr_by_branch: dict) -> list:







    lanes = set(pr_by_branch.get(branch, ()))
    for number in _ISSUE_IN_BRANCH.findall(str(branch or "")):
        lanes.update(issue_lanes.get(number, ()))
    return sorted(lanes)


def collect_worktrees(issue_lanes: dict, pr_by_branch: dict, prefix: str = ""):







    listing, err = _git(["worktree", "list", "--porcelain"])
    if err:
        return Section("worktrees", error=err), []

    entries = _worktrees.parse_worktree_list(listing)
    for entry in entries:
        entry["gitdir"] = _worktrees.resolve_gitdir(entry["path"])

    memo: dict = {}
    trees = []
    for entry in entries:
        verdict = _worktrees.assess(
            entry, scan=_worktrees._cwd_scan(entry["path"], memo))
        branch = entry.get("branch") or ("(detached)" if entry.get("detached")
                                         else "?")
        trees.append(Worktree(path=entry.get("path", "?"), branch=branch,
                              state=verdict.state,
                              lanes=_branch_lanes(branch, issue_lanes,
                                                  pr_by_branch)))

    lines = []
    for tree in sorted(trees, key=lambda t: (t.state != _worktrees.STATE_OCCUPIED,
                                             t.path)):
        lanes = " ".join(name[len(prefix):] for name in tree.lanes) or "lane ?"
        lines.append(f"{tree.state:<12} {_untrusted.flat(tree.path):<40} "
                     f"{_untrusted.flat(tree.branch):<22} {lanes}")
    lines.append("'cannot tell' is NOT 'idle' — expand one with "
                 "git-worktrees:<path>")
    return Section("worktrees", lines), trees




def collect_lane_universe(prefix: str):











    data, err = _json_cmd(["gh", "label", "list", "--limit", "200", "--json",
                           "name"], timeout=30)
    if err:
        return None, [], err
    return select_lane_universe(data, prefix)


def collect_issue_lanes(prefix: str):











    data, err = _json_cmd(["gh", "issue", "list", "--state", "all", "--limit",
                           "400", "--json", "number,labels"], timeout=60)
    if err:
        return None, err
    if not isinstance(data, list):
        return None, "gh issue list did not return a list"
    out: dict = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        lanes = [str(label.get("name")) for label in (item.get("labels") or [])
                 if isinstance(label, dict)
                 and str(label.get("name") or "").startswith(prefix)]
        if lanes:
            out[str(item.get("number"))] = lanes
    return out, ""


def render_lanes(states, strays=(), prefix: str = "", note: str = "") -> Section:
    if states is None:
        return Section("lanes", error="the lane label universe could not be read")
    if not states:



        return Section("lanes", lines=[],
                       warning=note or lane_universe_note(prefix, [], []))
    lines = []
    if strays:
        named = ", ".join(_untrusted.flat(f"{w.path} ({w.branch})")
                          for w in strays[:4])
        more = f", +{len(strays) - 4} more" if len(strays) > 4 else ""
        lines.append(f"unplaced: {len(strays)} live worktree(s) belong to no "
                     f"lane — {named}{more}. No lane is reported free while one "
                     "of them could be in it")
    order = {LANE_OCCUPIED: 0, LANE_UNKNOWN: 1, LANE_FREE: 2}
    for lane in sorted(states, key=lambda name: (order[states[name][0]], name)):
        state, evidence = states[lane]
        lines.append(f"{state:<10} {lane[len(prefix):]:<14} "
                     f"{' · '.join(evidence)}")
    lines.append("'unknown' is NOT 'free' — an undecidable or unplaced "
                 "occupancy could be in it")
    return Section("lanes", lines)




def _repo_identity():
    name, default, err = _gh_branch._repo_identity()
    return name, default, err


def build_report(budget: int, workers: int) -> Report:
    started = time.monotonic()
    repo, default_branch, repo_err = _repo_identity()

    def _left():
        return max(1, int(budget - (time.monotonic() - started)))




    prefix, prefix_complaint = read_lane_prefix()

    sections: dict = {}
    with _futures.ThreadPoolExecutor(max_workers=4) as pool:
        f_issues = pool.submit(collect_issue_lanes, prefix) if prefix else None
        f_labels = pool.submit(collect_lane_universe, prefix) if prefix else None
        f_local = pool.submit(collect_local, default_branch)
        f_default = pool.submit(collect_default, repo, default_branch)

        if f_issues is None:
            issue_lanes, issue_err = {}, ""
        else:
            try:
                issue_lanes, issue_err = f_issues.result(timeout=_left())
            except _futures.TimeoutError:
                issue_lanes, issue_err = None, f"budget of {budget}s ran out"

        board, prs = collect_board(issue_lanes or {}, workers, prefix or "")
        pr_by_branch = {pr.branch: pr.lanes for pr in prs}
        worktrees_section, trees = collect_worktrees(issue_lanes or {},
                                                     pr_by_branch, prefix or "")

        for key, future in (("local", f_local), ("default", f_default)):
            try:
                sections[key] = future.result(timeout=_left())
            except _futures.TimeoutError:
                sections[key] = Section(key, error=f"budget of {budget}s ran out")
            except Exception as exc:  
                sections[key] = Section(key, error=f"{type(exc).__name__}: {exc}")

        if f_labels is None:
            lanes, near, lane_err = None, [], ""
        else:
            try:
                lanes, near, lane_err = f_labels.result(timeout=_left())
            except _futures.TimeoutError:
                lanes, near, lane_err = None, [], f"budget of {budget}s ran out"

    sections["board"] = board
    sections["worktrees"] = worktrees_section

    states = lane_states(lanes, prs, trees, default_branch=default_branch)
    if prefix is None:
        lane_section = render_lane_refusal(prefix_complaint)
    elif lanes is None:
        lane_section = Section("lanes", error=lane_err or "unread")
    else:
        lane_section = render_lanes(
            states, stray_worktrees(trees, default_branch), prefix=prefix,
            note=lane_universe_note(prefix, lanes, near))
        if issue_lanes is None:
            lane_section.warning = (
                f"the issue labels could not be read ({issue_err}), so no PR or "
                "worktree could be placed in a lane — every lane below is "
                "'unknown' for that reason and not because it was inspected")
    sections["lanes"] = lane_section

    ordered = {name: sections[name] for name in
               ("local", "default", "board", "worktrees", "lanes")
               if name in sections}
    report = Report(repo=repo or "?", sections=ordered, prs=prs, lanes=states,
                    lane_prefix=prefix)
    if repo_err:
        report.sections["local"] = Section("local", error=repo_err)
    return report


def main() -> int:
    use_utf8_stdout()
    args = [a for a in sys.argv[1:] if a]
    if args:
        print(f"ERROR: refused — dashboard takes no arguments, got {args[0]!r}")
        print("  usage: dashboard   (read-only; no repo target, GitHub only)")
        return 2

    budget = _positive_int(os.environ.get("SUPERTOOL_DASHBOARD_BUDGET"), BUDGET_DEFAULT)
    workers = _positive_int(os.environ.get("SUPERTOOL_DASHBOARD_WORKERS"), WORKERS_DEFAULT)
    report = build_report(budget, workers)
    print(render(report))
    return 0 if not any(s.unread for s in report.sections.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
