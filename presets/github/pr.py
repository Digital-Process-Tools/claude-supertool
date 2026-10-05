#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
from collections import Counter
from typing import Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _body  
import _mirror  
import _untrusted  
import _classify_render  
import _auth_probe  
import _status_probe  
import _checks  
import _declared_legs  
import _repo_target  
import _branch_locale  
import _digits  
import _statusline_fragments  

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _console import use_utf8_stdout  
import _pr_diff  

DESCRIPTION_MAX = 2000
COMMENT_MAX = 500


_CLASSIFY_LEVEL = _classify_render.level_from_env()


DIFF_TIMEOUT = 60







MODES = ("status", "full", "diff", "threads")


def _relative_age(iso: str) -> str:

    if not iso:
        return "?"
    try:
        from datetime import datetime, timezone
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except (ValueError, ImportError):
        return "?"


def _mode_refusal(flags: Sequence[str]) -> str:






    for tok in flags:
        if not tok:
            continue
        if tok == "diff":
            return ""
        if tok in MODES:
            continue
        return (
            f"ERROR: gh-pr does not have a {tok!r} mode.\n"
            f"Nothing was read. This used to fall through to the default "
            f"dashboard at exit 0, which reads as an answer to the question "
            f"you asked rather than as a mode that was never applied — and "
            f"that dashboard's own header advertises the review threads a "
            f"caller typing {tok!r} is usually after.\n"
            f"Modes: status, full, diff[:PATH], threads. Usage: "
            f"gh-pr:NUMBER_OR_BRANCH[:status|:full|:diff[:PATH]|:threads]"
        )
    return ""


def _gh(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:







    if args and args[0] != "api":
        args = args + _repo_target.gh_args()
    return subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


_RUN_ID_IN_URL = re.compile(r"/actions/runs/([0-9]+)(?:[/?#]|$)")













MAX_RECONCILED_RUNS = 8


def _rollup_run_ids(rollup: object) -> list[str]:








    if not isinstance(rollup, list):
        return []
    seen: list[str] = []
    for c in rollup:
        if not isinstance(c, dict):
            continue
        m = _RUN_ID_IN_URL.search(str(c.get("detailsUrl") or ""))
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def _actions_leg_names(rollup: object) -> list[str]:

    if not isinstance(rollup, list):
        return []
    out: list[str] = []
    for c in rollup:
        if not isinstance(c, dict):
            continue
        if _RUN_ID_IN_URL.search(str(c.get("detailsUrl") or "")):
            out.append(str(c.get("name") or c.get("context") or "?"))
    return out


def _missing_names(declared: Sequence[str], found: Sequence[str]) -> list[str]:

    remaining = Counter(found)
    out: list[str] = []
    for name in declared:
        if remaining.get(name, 0):
            remaining[name] -= 1
        else:
            out.append(name)
    return out


def _runs_on_commit(owner: str, repo: str, sha: str) -> list | None:

























    if not owner or not repo or not sha:
        return None
    try:
        r = _gh(["api", f"repos/{owner}/{repo}/actions/runs"
                        f"?head_sha={sha}&per_page=100"])
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return None
    out = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        rid = str(run.get("id") or "").strip()
        if rid:
            out.append((rid, str(run.get("name") or f"run #{rid}"),
                        str(run.get("workflow_id") or "").strip()))
    return out


def _one_run_per_workflow(ordered: list, keys_by_id: dict) -> list:




















    out: list = []
    seen: set[str] = set()
    for rid, name in ordered:
        key = keys_by_id.get(rid) or f"#{rid}"
        if key in seen:
            continue
        seen.add(key)
        out.append((rid, name))
    return out


def _declared_for_commit(d: dict) -> tuple:
























    rollup = d.get("statusCheckRollup")
    rollup_ids = _rollup_run_ids(rollup)
    owner, repo = _declared_legs.owner_repo(d.get("url") or "")
    runs = _runs_on_commit(owner, repo, str(d.get("headRefOid") or ""))
    if runs is None:
        return (None, [], [], "the run list for this commit could not be read")

    ordered: list = [(rid, "") for rid in rollup_ids]
    known = set(rollup_ids)
    keys_by_id = {}
    for rid, name, workflow_id in runs:
        keys_by_id[rid] = workflow_id
        if rid not in known:
            ordered.append((rid, name))
            known.add(rid)
    ordered = _one_run_per_workflow(ordered, keys_by_id)
    if not ordered:
        return (0, [], [], "")
    if len(ordered) > MAX_RECONCILED_RUNS:
        return (None, [], [],
                f"{len(ordered)} distinct workflows on this commit exceed the "
                f"reconciliation cap of {MAX_RECONCILED_RUNS}")

    declared_names: list[str] = []
    uncovered: list[str] = []
    for rid, name in ordered:
        names = _declared_legs.legs_for_run(owner, repo, rid)
        if names is None:
            return (None, [], [],
                    f"the job list for run {name or rid} could not be read")
        declared_names.extend(names)
        if not names and rid not in rollup_ids:
            uncovered.append(name or f"run #{rid}")
    return (len(declared_names), declared_names, uncovered, "")


def _reconcile_checks(d: dict) -> tuple[str, list[str]]:
















    found_names = _actions_leg_names(d.get("statusCheckRollup"))
    declared, declared_names, uncovered, reason = _declared_for_commit(d)
    if declared is None and not found_names and not uncovered:
        return ("", [])

    missing = _missing_names(declared_names, found_names)
    marker, lines = _checks.shortfall(len(found_names), declared, missing,
                                      reason=reason)
    if uncovered:
        shown = ", ".join(uncovered[:_checks.NAMED_CAP])
        if len(uncovered) > _checks.NAMED_CAP:
            shown += f", +{len(uncovered) - _checks.NAMED_CAP} more"
        one = len(uncovered) == 1
        lines = list(lines) + [
            f"  not covered: {shown} — {'that run' if one else 'those runs'} on "
            f"this commit ha{'s' if one else 've'} no job yet, so how many legs "
            f"{'it declares' if one else 'they declare'} is UNKNOWN and none of "
            "them are in this tally."
        ]
        marker = marker or _checks.INCOMPLETE_MARK
    return (marker, lines)

def _leg_unit_line(check_states: Sequence[str]) -> str:

















    if not any(_checks.bucket(s) == "failed" for s in check_states):
        return ""
    return ("  (those are LEGS — one check run each, not one test each. For the "
            "test counts read a leg's own summary: ./supertool 'gh-job:ID')")


def _local_branch_check(source: str) -> str:






    return _branch_locale.check(source)









THREAD_INDEX_MAX = 20




THREAD_EXCERPT_MAX = 90










THREADS_PAGE_MAX = 100






COMMENTS_PAGE_MAX = 50

_THREADS_QUERY = (
    "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r)"
    "{pullRequest(number:$n){reviewThreads(first:"
    + str(THREADS_PAGE_MAX) + "){nodes{"
    "isResolved isOutdated path line originalLine "
    "comments(first:" + str(COMMENTS_PAGE_MAX) + "){nodes{body createdAt url "
    "author{login}}}"
    "}}}}}"
)


def _fetch_review_threads_detailed(
        url: str, number: int | str) -> tuple[list | None, str]:









    if not url:
        return (None, "the PR has no URL, so its owner/repo could not be read")
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/\d+", url)
    if not m:
        return (None, f"the PR URL {url!r} is not a github.com pull URL, so "
                      f"its owner/repo could not be read")
    owner, repo = m.group(1), m.group(2)
    try:
        r = _gh([
            "api", "graphql",
            "-f", f"query={_THREADS_QUERY}",
            "-F", f"o={owner}", "-F", f"r={repo}", "-F", f"n={number}",
        ], timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return (None, f"the GraphQL call did not complete: {exc}")
    if r.returncode != 0:









        tail = _untrusted.split_lines((r.stderr or r.stdout or "").strip())
        return (None, _untrusted.flat(tail[-1]) if tail
                else f"gh exited {r.returncode}")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return (None, "the GraphQL reply was not JSON")





    node: object = data
    for key in ("data", "repository", "pullRequest", "reviewThreads"):
        if not isinstance(node, dict):
            return (None, f"the GraphQL reply was not shaped like a "
                          f"pull request: {key!r} sits under a "
                          f"{type(node).__name__}, not an object")
        node = node.get(key)
    if not isinstance(node, dict) or not isinstance(node.get("nodes"), list):
        return (None, "the GraphQL reply carried no reviewThreads list")
    return (node["nodes"], "")


def _thread_page_floors(threads: list, unresolved: int) -> tuple[str, str, str]:














    if len(threads) < THREADS_PAGE_MAX:
        return (str(unresolved), str(len(threads)), "")
    return (f"at least {unresolved}", f"at least {len(threads)}",
            f" — the fetch stops at the first page of {THREADS_PAGE_MAX} "
            f"review threads, so both numbers are floors and not counts")


def _render_threads(number: str, comment_max: int | None) -> int:












    try:
        meta = _gh(["pr", "view", number, "--json",
                    "number,title,url,headRefName,baseRefName"])
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"ERROR: gh pr view did not complete: {exc}")
        return 1
    if meta.returncode != 0:
        print(_format_error(meta.stderr, "PR", str(number)))
        return 1
    try:
        d = json.loads(meta.stdout)
    except json.JSONDecodeError:


        print("ERROR: invalid JSON from gh — its body, verbatim, below")
        print(_untrusted.banner())
        print(_untrusted.fence(meta.stdout[:500]))
        return 1

    iid = d.get("number", number)
    print(_untrusted.banner())
    print(f"# Review threads on PR #{iid} "
          f"{_untrusted.flat(str(d.get('title') or '?'))}")
    print(f"Branch: {_untrusted.flat(str(d.get('headRefName') or '?'))} -> "
          f"{_untrusted.flat(str(d.get('baseRefName') or '?'))}")
    if d.get("url"):
        print(f"URL: {d['url']}")

    threads, err = _fetch_review_threads_detailed(d.get("url", ""), iid)
    if threads is None:



        print(f"Threads: UNKNOWN — they could not be read "
              f"({_untrusted.flat(err)}).")
        print("This is not 'none'. Nothing here establishes whether this PR "
              "has review threads; the default view's own count comes off the "
              "same call and would be equally silent.")
        return 1
    if not threads:
        print("Threads: no review threads on this PR — read, not assumed.")
        return 0

    unresolved = sum(1 for t in threads if not t.get("isResolved"))
    shown, total, floor_note = _thread_page_floors(threads, unresolved)
    print(f"Threads: {shown} unresolved of {total}{floor_note}")


    ordered = sorted(threads, key=lambda t: bool(t.get("isResolved")))
    for t in ordered:
        state = "RESOLVED" if t.get("isResolved") else "UNRESOLVED"
        where = _untrusted.flat(str(t.get("path") or "(no file)"))
        line_no = t.get("line")
        if isinstance(line_no, int):
            where += f":{line_no}"
        outdated = " [outdated]" if t.get("isOutdated") else ""
        print(f"\n## {state} — {where}{outdated}")
        comments = ((t.get("comments") or {}).get("nodes")) or []
        for c in comments:
            author = _untrusted.flat(
                str((c.get("author") or {}).get("login") or "?"))
            body = c.get("body") or ""
            trunc = ""
            if comment_max is not None and len(body) > comment_max:
                body = body[:comment_max]
                trunc = _body.comment_cut_notice(comment_max)
            print(f"\n**{author}** ({str(c.get('createdAt') or '')[:10]}):")
            print(_untrusted.fence(body))
            if trunc:
                print(trunc)
            if c.get("url"):





                print(_untrusted.flat(str(c["url"])))



        if len(comments) >= COMMENTS_PAGE_MAX:
            print()
            print(f"[the first {COMMENTS_PAGE_MAX} comments on this thread — "
                  f"the fetch stops there and does not establish whether more "
                  f"exist. This is not the end of the thread.]")
    return 0


def _thread_excerpt(thread: dict) -> str:















    comments = ((thread.get("comments") or {}).get("nodes")) or []
    if not comments:
        return "(no comment body)"
    body = _untrusted.flat(str(comments[0].get("body") or ""))
    body = " ".join(body.split()).strip()
    if not body:
        return "(no comment body)"
    if len(body) > THREAD_EXCERPT_MAX:
        return body[:THREAD_EXCERPT_MAX] + "…"
    return body


def _thread_index(threads: list | None, err: str, number: int | str) -> list[str]:




















    if threads is None:
        return [
            f"Unresolved threads: UNKNOWN — they could not be read "
            f"({_untrusted.flat(err)}).",
            "  This is not zero. Nothing here establishes whether this PR has "
            "review threads; this line used to be omitted entirely in that "
            "case, which reads as a PR with nothing on it.",
        ]
    if not threads:
        return ["Unresolved threads: 0 / 0 — read, not assumed."]

    unresolved = sum(1 for t in threads if not t.get("isResolved"))
    shown, total, floor_note = _thread_page_floors(threads, unresolved)
    out = [f"Unresolved threads: {shown} / {total}{floor_note}"]
    ordered = sorted(threads, key=lambda t: bool(t.get("isResolved")))
    for t in ordered[:THREAD_INDEX_MAX]:
        state = "resolved  " if t.get("isResolved") else "UNRESOLVED"
        where = _untrusted.flat(str(t.get("path") or "(no file)"))






        line_no = t.get("line")
        if not isinstance(line_no, int):
            line_no = t.get("originalLine")
        if isinstance(line_no, int):
            where += f":{line_no}"
        comments = ((t.get("comments") or {}).get("nodes")) or []
        author = "?"
        if comments:
            author = _untrusted.flat(
                str((comments[0].get("author") or {}).get("login") or "?"))
        outdated = " [outdated]" if t.get("isOutdated") else ""
        out.append(f"  {state}  {where}{outdated}  {author}  "
                   f"{_thread_excerpt(t)}")
    withheld = len(ordered) - THREAD_INDEX_MAX
    if withheld > 0:


        at_least = "at least " if len(threads) >= THREADS_PAGE_MAX else ""
        out.append(f"  … {at_least}{withheld} more not indexed here")
    out.append(f"  bodies: gh-pr:{number}:threads — the rows above are one "
               f"line each, not the finding")
    return out


def _head_commit_age_secs(url: str, number: int | str) -> int | None:


















    if not url:
        return None
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/\d+", url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    query = (
        "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r)"
        "{pullRequest(number:$n){commits(last:1){nodes{commit"
        "{pushedDate committedDate}}}}}}"
    )
    try:
        r = _gh([
            "api", "graphql",
            "-f", f"query={query}",
            "-F", f"o={owner}", "-F", f"r={repo}", "-F", f"n={number}",
        ])
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        repo_node = (data.get("data") or {}).get("repository") or {}
        pr_node = repo_node.get("pullRequest") or {}
        nodes = (pr_node.get("commits") or {}).get("nodes") or []
        if not nodes:
            return None
        commit = nodes[-1].get("commit") or {}
        stamp = commit.get("pushedDate") or commit.get("committedDate") or ""
        if not stamp:
            return None
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError,
            AttributeError, TypeError, ImportError):
        return None


def _absence_lines(d: dict, number: int | str) -> tuple[str, str]:

    return _checks.absence(
        d.get("state"),
        _head_commit_age_secs(d.get("url") or "", number),
        mergeable=d.get("mergeable"),
    )


def _format_error(stderr: str, resource: str, identifier: str) -> str:

    s = stderr.lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return _repo_target.no_repo_error("gh-pr:265:status")
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


def _diff_header(number: str) -> list[str]:







    head = [_untrusted.banner()]
    try:
        meta = _gh(["pr", "view", number, "--json",
                    "number,title,headRefName,baseRefName,url"])
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        head.append(f"# PR #{number} (title unavailable: {exc})")
        return head
    if meta.returncode != 0:
        head.append(


            f"# PR #{number} (title unavailable: "
            f"{_untrusted.flat((meta.stderr or '').strip()[:80]) or 'gh pr view failed'})")
        return head
    try:
        d = json.loads(meta.stdout)
    except json.JSONDecodeError:
        head.append(f"# PR #{number} (title unavailable: unparseable JSON)")
        return head
    head.append(f"# PR #{d.get('number', number)} "
                f"{_untrusted.flat(str(d.get('title') or '?'))}")
    head.append(f"Branch: {_untrusted.flat(str(d.get('headRefName') or '?'))} "
                f"-> {_untrusted.flat(str(d.get('baseRefName') or '?'))}")
    if d.get("url"):
        head.append(f"URL: {d['url']}")
    return head


def _run_diff(number: str, path: str | None) -> int:



















    header = _diff_header(number)
    files: list[dict] | None
    reason: str | None = None
    try:
        result = _gh(["pr", "diff", number], timeout=DIFF_TIMEOUT)
    except subprocess.TimeoutExpired:
        files, reason = None, f"gh pr diff timed out after {DIFF_TIMEOUT}s"
    except (FileNotFoundError, OSError) as exc:
        files, reason = None, f"gh pr diff could not run: {exc}"
    else:
        if result.returncode != 0:
            files = None
            reason = (f"gh pr diff exited {result.returncode}: "
                      f"{_untrusted.flat((result.stderr or '').strip()[:200]) or 'no stderr'}")
        else:
            files = _pr_diff.parse(result.stdout)
    text, code = _pr_diff.render(files, header=header, path=path,
                                 reason=reason, number=str(number))
    print(text)
    return code


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:



        print("ERROR: usage: pr.py NUMBER_OR_BRANCH "
              "[status|full|diff[:PATH]|threads]")
        return 1

    arg = sys.argv[1]
    flags = sys.argv[2:]


    refusal = _mode_refusal(flags)
    if refusal:
        print(refusal)
        return 1
    slim = "status" in flags




    full = "full" in flags
    desc_max = None if full else DESCRIPTION_MAX
    comment_max = None if full else COMMENT_MAX




    if not _digits.is_ascii_int(arg):
        try:
            branch_result = _gh([
                "pr", "list", "--head", arg, "--json", "number",
                "--limit", "1"
            ])
            if branch_result.returncode == 0:
                prs = json.loads(branch_result.stdout)
                if prs:
                    arg = str(prs[0].get("number", arg))
                else:

                    branch_result2 = _gh([
                        "pr", "list", "--head", arg, "--state", "all",
                        "--json", "number", "--limit", "1"
                    ])
                    if branch_result2.returncode == 0:
                        prs2 = json.loads(branch_result2.stdout)
                        if prs2:
                            arg = str(prs2[0].get("number", arg))
                        else:
                            print(f"ERROR: no PR found for branch {arg!r}")
                            return 1
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"ERROR: branch lookup failed: {e}")
            return 1



    if "diff" in flags:
        rest = flags[flags.index("diff") + 1:]
        return _run_diff(arg, rest[0] if rest else None)


    if "threads" in flags:
        return _render_threads(arg, comment_max)


    try:
        result = _gh([
            "pr", "view", arg, "--json",
            "number,title,state,author,headRefName,baseRefName,labels,"
            "milestone,reviewDecision,reviews,mergeCommit,mergedAt,mergeable,"
            "isDraft,url,body,comments,additions,deletions,changedFiles,"
            "statusCheckRollup,assignees,createdAt,updatedAt,headRefOid"
        ])
    except FileNotFoundError:
        print("ERROR: gh not found — install the GitHub CLI")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: gh timed out")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "PR", arg))
        return 1

    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:


        print("ERROR: invalid JSON from gh — its body, verbatim, below")
        print(_untrusted.banner())
        print(_untrusted.fence(result.stdout[:500]))
        return 1







    _statusline_fragments.publish("gh-pr", os.getcwd(), {
        "summary": _checks.summarize_github(d.get("statusCheckRollup")),
        "number": d.get("number"),
        "branch": d.get("headRefName"),
        "mergeable": d.get("mergeable"),
    })









    mirror_cfg = _mirror.load_config(pathlib.Path.cwd().resolve())
    if mirror_cfg.error is not None:
        print(f"note: gh mirror not written -- {mirror_cfg.error}")
    elif mirror_cfg.path is not None:



        mirror_number = str(d.get("number", arg))
        if _digits.is_ascii_int(mirror_number):
            mirror_err = _mirror.write_pr(mirror_cfg.path, mirror_number, d)
            if mirror_err is not None:
                print(f"note: gh mirror not written -- {mirror_err}")
        else:
            print(
                f"note: gh mirror not written -- the API reply's PR "
                f"number ({mirror_number!r}) is not a plain integer"
            )

    if slim:
        iid = d.get("number", arg)
        state = d.get("state", "?")
        mergeable = d.get("mergeable", "?")
        review_decision = d.get("reviewDecision") or "none"
        check_states = _checks.github_states(d.get("statusCheckRollup"))




        live_states = _checks.github_live_states(d.get("statusCheckRollup"))
        merge_commit = (d.get("mergeCommit") or {}).get("oid", "")








        merged_at = d.get("mergedAt") or "-"
        web_url = d.get("url", "")
        conflicts = "yes" if mergeable == "CONFLICTING" else "no"
        print(f"#{iid} | state: {state} | mergeable: {mergeable} | conflicts: {conflicts}")
        print(f"branch: {_untrusted.flat(d.get('headRefName') or '?')} -> "
              f"{_untrusted.flat(d.get('baseRefName') or '?')}")
        shortfall_lines: list[str] = []
        if check_states:



            checks_text = _checks.summarize_github(
                d.get("statusCheckRollup"), with_age=True)
            marker, shortfall_lines = _reconcile_checks(d)
            if marker:
                checks_text += f" {marker}"
        else:
            checks_text, _ = _absence_lines(d, iid)
        print(f"checks: {checks_text}")
        for line in _checks.github_pending_lines(d.get("statusCheckRollup")):
            print(line)
        for line in shortfall_lines:
            print(line)
        for line in _checks.named_disclosure(
            _checks.github_named_live(d.get("statusCheckRollup"))
        ):
            print(line)



        for line in _checks.superseded_disclosure(
            _checks.github_named_superseded(d.get("statusCheckRollup"))
        ):
            print(line)
        unit_line = _leg_unit_line(live_states)
        if unit_line:
            print(unit_line)
        print(f"review: {review_decision}")
        print(f"merged_at: {merged_at}")
        if merge_commit:
            print(f"merge_commit: {merge_commit[:12]}")
        if web_url:
            print(f"url: {web_url}")
        return 0


    title = _untrusted.flat(d.get("title", "?"))
    state = d.get("state", "?")
    iid = d.get("number", arg)
    source = _untrusted.flat(d.get("headRefName", "?"))
    target = _untrusted.flat(d.get("baseRefName", "?"))
    author = _untrusted.flat((d.get("author") or {}).get("login", "?"))
    web_url = d.get("url", "")
    labels = _untrusted.flat(", ".join(l.get("name", "?") for l in d.get("labels", [])) or "none")
    milestone = _untrusted.flat((d.get("milestone") or {}).get("title", "none"))
    draft = d.get("isDraft", False)
    mergeable = d.get("mergeable", "?")
    review_decision = d.get("reviewDecision") or "none"
    merge_commit = (d.get("mergeCommit") or {}).get("oid", "")
    additions = d.get("additions", "?")
    deletions = d.get("deletions", "?")
    changed_files = d.get("changedFiles", "?")

    body = d.get("body") or ""
    body_total = len(body)
    body, body_withheld = _body.cut(body, desc_max)



    draft_marker = " [DRAFT]" if draft else ""
    print(_untrusted.banner())
    print(f"# #{iid} {title}{draft_marker}")
    print(f"State: {state} | Author: {author}")
    print(f"Branch: {source} -> {target}")
    local_check = _local_branch_check(source)
    if local_check:
        print(local_check)
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")
    if body_withheld:


        print(_body.header_notice(body, body_total, body_withheld))


    assignees = d.get("assignees") or []
    assignee_names = [a.get("login", "?") for a in assignees]
    print(f"Assignees: {', '.join(assignee_names) if assignee_names else 'none'}")


    created_at = d.get("createdAt") or ""
    updated_at = d.get("updatedAt") or ""
    if created_at:
        age_str = f"Created: {_relative_age(created_at)}"
        if updated_at and updated_at != created_at:
            age_str += f" | Updated: {_relative_age(updated_at)}"
        print(age_str)






    review_threads, threads_err = _fetch_review_threads_detailed(
        d.get("url", ""), iid)
    for line in _thread_index(review_threads, threads_err, iid):
        print(line)


    reviews = d.get("reviews", [])
    if reviews:
        reviewers = {}
        for r in reviews:
            login = _untrusted.flat((r.get("author") or {}).get("login", "?"))
            r_state = r.get("state", "?")
            reviewers[login] = r_state  
        parts = [f"{login} ({state})" for login, state in reviewers.items()]
        print(f"Reviews: {', '.join(parts)}")
    else:
        print("Reviews: none")
    print(f"Review decision: {review_decision}")






    check_states = _checks.github_states(d.get("statusCheckRollup"))





    live_states = _checks.github_live_states(d.get("statusCheckRollup"))
    shortfall_lines: list[str] = []
    if check_states:

        checks_text = _checks.summarize_github(
            d.get("statusCheckRollup"), with_age=True)
        merge_note = "" if _checks.all_green(live_states) else (
            f" — checks {_checks.NOT_GREEN}, see Checks above"
        )




        marker, shortfall_lines = _reconcile_checks(d)
        if marker:
            checks_text += f" {marker}"
            if not merge_note:
                merge_note = f" — checks {marker}, see Checks above"
    else:
        checks_text, merge_note = _absence_lines(d, iid)
    print(f"Checks: {checks_text}")
    for line in _checks.github_pending_lines(d.get("statusCheckRollup")):
        print(line)
    for line in shortfall_lines:
        print(line)
    for line in _checks.named_disclosure(
        _checks.github_named_live(d.get("statusCheckRollup"))
    ):
        print(line)

    for line in _checks.superseded_disclosure(
        _checks.github_named_superseded(d.get("statusCheckRollup"))
    ):
        print(line)
    unit_line = _leg_unit_line(live_states)
    if unit_line:
        print(unit_line)


    print(f"Changes: {changed_files} files, +{additions} -{deletions}")







    if mergeable == "CONFLICTING":
        print(f"Conflicts: YES — cannot merge{merge_note}")
    elif mergeable == "MERGEABLE":
        print(f"Mergeable: yes (no merge conflicts){merge_note}")
    else:
        print(f"Mergeable: {mergeable}{merge_note}")


    if merge_commit:
        print(f"Merge commit: {merge_commit[:12]}")

    if web_url:
        print(f"URL: {web_url}")





    issue_refs = _checks.closing_issue_refs(d.get("body"))
    if not issue_refs:
        print(f"\n{_checks.linked_issue_line(issue_refs)}")
    for ref in issue_refs:




        if not ref.startswith("#"):
            print(f"\nIssue: {ref} — in another repository, not fetched")
            continue
        issue_num = ref[1:]
        try:
            issue_result = _gh([
                "issue", "view", issue_num, "--json",
                "number,title,state,labels,assignees"
            ])
            if issue_result.returncode == 0:
                issue_data = json.loads(issue_result.stdout)
                i_title = issue_data.get("title", "?")
                i_state = issue_data.get("state", "?")
                i_labels = ", ".join(l.get("name", "") for l in issue_data.get("labels", []))
                i_assignees = ", ".join(a.get("login", "") for a in issue_data.get("assignees", []))
                print(f"\n## Issue #{issue_num} — {i_title}")
                info = f"State: {i_state}"
                if i_labels:
                    info += f" | Labels: {i_labels}"
                if i_assignees:
                    info += f" | Assignees: {i_assignees}"
                print(info)
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            print(f"\nIssue: {ref}")



    classify_budget = _classify_render.Budget()


    if body:
        print(f"\n## Description\n{_untrusted.fence(body)}")
        if body_withheld:
            print(f"\n{_body.cut_notice(body_withheld)}")
        print(classify_budget.line(body, level=_CLASSIFY_LEVEL))
    else:
        print("\n## Description\n_(empty)_")






    comments = d.get("comments", [])
    shown, gap_hidden = ((list(comments), 0) if full
                         else _body.comment_window(comments))
    print(f"\n{_body.comments_heading(len(shown), len(comments))}")
    for position, c in enumerate(shown):
        if gap_hidden and position == _body.COMMENT_HEAD:
            print(f"\n{_body.comments_gap_notice(gap_hidden)}")
        c_author = _untrusted.flat((c.get("author") or {}).get("login", "?"))
        c_body = c.get("body") or ""


        c_trunc = ""
        if comment_max is not None and len(c_body) > comment_max:
            c_body = c_body[:comment_max]
            c_trunc = _body.comment_cut_notice(comment_max)
        c_created = (c.get("createdAt") or "")[:10]
        print(f"\n**{c_author}** ({c_created}):")
        print(_untrusted.fence(c_body))
        if c_trunc:
            print(c_trunc)
        print(classify_budget.line(c_body, level=_CLASSIFY_LEVEL))

    return 0


if __name__ == "__main__":
    sys.exit(main())
