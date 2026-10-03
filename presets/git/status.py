#!/usr/bin/env python3












from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time



sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _checks  
import _untrusted  
from _env import env_int  
from _git_common import ANSWERED_NONE, TIMEOUT_RC, st_hint, use_utf8_stdout  
from _git_common import unanswered_repo_lines  
from _git_common import foreign_worktree, foreign_worktree_note  
from _git_common import _git as _spawn_git  







_GIT_TIMEOUT_DEFAULT = 5

INCOMPLETE_MARKER = "git-status INCOMPLETE"





STAGED_ABSENT_MARKER = "STAGED CONTENT NOT IN THIS TREE"



STAGED_PROVENANCE_UNKNOWN = "Staged provenance UNKNOWN"










ACTIVE_WINDOW_DEFAULT = 900







MTIME_UNREADABLE_MARKER = "mtime UNREADABLE"










_UNANSWERED: list[tuple[str, str]] = []

















_ANSWERED_NONE = ANSWERED_NONE


def _git_timeout(default: int | None = None) -> int:






    base = _GIT_TIMEOUT_DEFAULT if default is None else default
    return env_int("SUPERTOOL_GIT_TIMEOUT", base, minimum=1)


def _git(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:

















    budget = _git_timeout() if timeout is None else timeout
    res = _spawn_git(args, timeout=budget)
    if res.returncode == TIMEOUT_RC:










        _UNANSWERED.append((_untrusted.flat("git " + " ".join(args)), res.stderr))
    return res



_PATH_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
                 "\\": 92, '"': 34}


def _unquote_path(path: str) -> str:















    if len(path) < 2 or not path.startswith('"') or not path.endswith('"'):
        return path
    body = path[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.extend(ch.encode("utf-8"))
            i += 1
            continue
        nxt = body[i + 1:i + 2]
        if nxt in _PATH_ESCAPES:
            out.append(_PATH_ESCAPES[nxt])
            i += 2
            continue
        octal = body[i + 1:i + 4]
        if len(octal) == 3 and all(c in "01234567" for c in octal):
            out.append(int(octal, 8))
            i += 4
            continue




        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace")


def _unborn_head() -> bool:











    probe = _git(["rev-parse", "--verify", "--quiet", "HEAD"])


    said = bool(probe.stderr.strip())
    return (probe.returncode != 0
            and probe.returncode != TIMEOUT_RC
            and not said)


def _staged_path(line: str) -> str:







    rest = line[3:]
    if line[:1] in ("R", "C"):
        if rest.startswith('"'):
            i = 1
            while i < len(rest):
                if rest[i] == "\\":
                    i += 2
                    continue
                if rest[i] == '"':
                    break
                i += 1
            tail = rest[i + 1:]
            if tail.startswith(" -> "):
                rest = tail[4:]
        elif " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
    return _unquote_path(rest)


def _age(seconds: float) -> str:








    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _head_age_note() -> str:





















    r = _git(["log", "-1", "--format=%ct"], timeout=3)
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    try:
        commit_ts = int(r.stdout.strip().splitlines()[0])
    except ValueError:
        return ""
    return f", HEAD {_age(time.time() - commit_ts)} old"


def _worktree_root() -> str | None:
















    top = _git(["rev-parse", "--show-toplevel"])
    if top.returncode == 0 and top.stdout.strip():
        return top.stdout.strip()
    return None


def _untracked_age(root: str | None, porcelain_path: str, now: float):


















    if root is None:
        return None, "this tree's top level could not be resolved"
    raw = _unquote_path(porcelain_path).rstrip("/")
    try:
        st = os.stat(os.path.join(root, raw), follow_symlinks=False)
    except (OSError, ValueError, NotImplementedError) as exc:









        said = getattr(exc, "strerror", None) or type(exc).__name__
        return None, _untrusted.flat(str(said))
    return now - st.st_mtime, None


def _untracked_row(line: str, age, why: str, window: int) -> str:








    shown = line[3:]
    if age is None:
        return f"  {shown}  ({MTIME_UNREADABLE_MARKER}: {why})"
    if age <= window:
        return (f"  {shown}  (written {_age(age)} ago — inside the "
                f"{_age(window)} activity window)")
    return f"  {shown}  (written {_age(age)} ago)"


def _reason(returncode: int, stderr: str) -> str:














    said = _untrusted.flat(" ".join(stderr.split()))
    return f"exit {returncode}: {said[:100]}" if said else f"exit {returncode}"


def _note_failed(cmd: list[str], r: subprocess.CompletedProcess[str]) -> None:













    if r.returncode in (0, TIMEOUT_RC):
        return
    _UNANSWERED.append(("git " + " ".join(cmd), _reason(r.returncode, r.stderr)))







_CHERRY_CMD = ["rev-list", "--count", "--right-only", "--cherry-pick",
               "HEAD...@{upstream}"]


def _divergence_line(behind: int) -> str:

























    res = _git(_CHERRY_CMD)
    _note_failed(_CHERRY_CMD, res)
    out = res.stdout.strip()
    if res.returncode != 0 or not out.isdigit():
        return (f"Diverged: UNKNOWN whether those {behind} remote commit(s) "
                f"are replays of your own — `git {' '.join(_CHERRY_CMD)}` did "
                f"not answer ({_reason(res.returncode, res.stderr)}). This is "
                f"not saying nothing was lost.")
    only_theirs = int(out)
    if only_theirs == 0:
        return (f"Diverged: REBASED — every one of those {behind} remote "
                f"commit(s) is patch-equivalent to a commit you already have, "
                f"so nothing is lost and the remote is stale. Push: "
                + st_hint("git-push:force-with-lease"))
    return (f"Diverged: {only_theirs} of those {behind} remote commit(s) are "
            f"NOT in your history — a genuine divergence. Reconcile (rebase "
            f"or merge) before pushing; a force push discards them.")


def _hosted_request(cmd: list[str]) -> dict | None:


















    budget = _git_timeout()
    label = " ".join(cmd[:3])
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=budget,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        _UNANSWERED.append((label, f"timed out after {budget}s"))
        return None
    except OSError as e:
        _UNANSWERED.append((label, f"could not be run: {e}"))
        return None
    if r.returncode != 0:
        said = (r.stderr + r.stdout).lower()
        if not any(phrase in said for phrase in _ANSWERED_NONE):
            _UNANSWERED.append((label, _reason(r.returncode, r.stderr)))
        return None
    try:
        parsed = json.loads(r.stdout)
    except json.JSONDecodeError:



        _UNANSWERED.append((label, "answered with output that is not JSON"))
        return None
    if not isinstance(parsed, dict):
        _UNANSWERED.append((label, "answered with JSON that is not an object"))
        return None
    return parsed


def _incomplete_note() -> str:

    if not _UNANSWERED:
        return ""
    shown = [f"`{c}` ({w})" for c, w in _UNANSWERED[:3]]
    more = len(_UNANSWERED) - len(shown)
    calls = ", ".join(shown) + (f" (+{more} more)" if more else "")
    plural = "s" if len(_UNANSWERED) != 1 else ""
    return (
        f"\n{INCOMPLETE_MARKER} — {len(_UNANSWERED)} call{plural} did not answer "
        f"and {'were' if len(_UNANSWERED) != 1 else 'was'} skipped: {calls}. "
        f"Sections that depend on them are missing because the call did not "
        f"answer, not because there was nothing to report. "
        f"Raise SUPERTOOL_GIT_TIMEOUT if a timeout recurs."
    )


def _head_commit_age_secs(sha: str) -> int | None:


























    if not _checks.is_full_sha(sha):
        return None
    r = _git(["log", "-1", "--format=%ct", f"{sha}^{{commit}}"], timeout=3)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        committed = int(r.stdout.strip())
    except ValueError:
        return None
    return max(0, int(time.time()) - committed)


def main() -> int:
    use_utf8_stdout()



    _UNANSWERED.clear()



    mode = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    full = mode in ("full", "porcelain")






    brief = mode == "brief"



    unknown_mode = bool(mode) and not full and not brief


    branch_result = _git(["branch", "-vv", "--no-color"])














    if branch_result.returncode == TIMEOUT_RC:
        for line in unanswered_repo_lines(
                branch_result.stderr.strip() or f"exit {TIMEOUT_RC}",
                probe="git branch -vv --no-color"):
            print(line)
        return 1
    if branch_result.returncode != 0:
        stderr = branch_result.stderr.lower()
        if "not a git repository" in stderr:
            print("ERROR: not inside a git repository.")
        else:




            print("ERROR: git failed: "
                  f"{_reason(branch_result.returncode, branch_result.stderr)}")
        return 1









    branch_name_result = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    branch_name = branch_name_result.stdout.strip() if branch_name_result.returncode == 0 else "?"


    ahead_behind = ""
    ahead = behind = 0
    ab_result = _git(["rev-list", "--left-right", "--count", f"HEAD...@{{upstream}}"])
    if ab_result.returncode == 0:
        parts = ab_result.stdout.strip().split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
            if ahead and behind:
                ahead_behind = f"ahead {ahead}, behind {behind}"
            elif ahead:
                ahead_behind = f"ahead {ahead}"
            elif behind:
                ahead_behind = f"behind {behind}"
            else:
                ahead_behind = "up to date"




    divergence = ""
    if ahead and behind:
        divergence = _divergence_line(behind)


    base_divergence = ""
    base_branch = ""
    for candidate in ("master", "main"):
        check = _git(["rev-parse", "--verify", "--quiet", candidate])
        if check.returncode == 0:
            base_branch = candidate
            break
    if base_branch and branch_name != base_branch:
        base_ab = _git(["rev-list", "--left-right", "--count",
                        f"{base_branch}...HEAD"])
        if base_ab.returncode == 0:
            parts = base_ab.stdout.strip().split()
            if len(parts) == 2:
                behind_base, ahead_base = int(parts[0]), int(parts[1])
                if ahead_base == 0:
                    suffix = f", {behind_base} behind" if behind_base else ""
                    base_divergence = (f"vs {base_branch}: 0 ahead{suffix} "
                                       f"— branch has no own commits!")
                else:
                    parts_str = f"{ahead_base} ahead"
                    if behind_base:
                        parts_str += f", {behind_base} behind"
                    base_divergence = f"vs {base_branch}: {parts_str}"

    print(f"# git-status")



    _copy = foreign_worktree()
    if _copy is not None:
        print(foreign_worktree_note(_copy))
        print(f"  `cp` cannot copy a worktree — its `.git` is a pointer, not a "
              f"repository. `git worktree add` is the operation. Nothing below "
              f"describes {_copy[0]}.")
    if unknown_mode:
        print(f"⚠ mode {mode!r} is not one of full|porcelain|brief — it was "
              f"ignored, and what follows is the DEFAULT render, not the one "
              f"you asked for.")
    print(f"Branch: {branch_name}" + (f" ({ahead_behind})" if ahead_behind else ""))
    if divergence:
        print(divergence)
    if base_divergence:
        print(base_divergence)


    origin_head = _git(["log", "-1", "--format=%h %s", "@{upstream}"])
    if origin_head.returncode == 0 and origin_head.stdout.strip():
        print(f"Origin HEAD: {origin_head.stdout.strip()}")




    others = _git(["for-each-ref",
                   "--format=%(refname:short)\t%(upstream:track)", "refs/heads"])
    if others.returncode == 0 and not brief:
        rows = []









        for line in _untrusted.split_lines(others.stdout):
            name, _, track = _untrusted.visible(line, keep="\t").partition("\t")
            track = track.strip()


            if name and name != branch_name and ("ahead" in track or "behind" in track):


                rows.append((name, track.strip("[]")))
        if rows:
            print("\n## Other branches with unpushed/unpulled work")
            for name, track in (rows if full else rows[:10]):
                print(f"  {name}  {track}")
            if not full and len(rows) > 10:
                print(f"  ... ({len(rows) - 10} more)")


    log_result = _git(["log", "-5", "--format=%h %ad %an | %s", "--date=short"])
    if log_result.returncode == 0 and log_result.stdout.strip() and not brief:
        print(f"\n## Last 5 commits")


        for line in _untrusted.split_lines(log_result.stdout.strip()):
            print(f"  {_untrusted.visible(line)}")


    status_cmd = ["status", "--porcelain=v1"]
    status_result = _git(status_cmd)
    _note_failed(status_cmd, status_result)
    if status_result.returncode != 0:






        print(f"\n## Working tree: UNKNOWN — `git {' '.join(status_cmd)}` did "
              f"not answer "
              f"({_reason(status_result.returncode, status_result.stderr)}). "
              f"This run did not look — it is not 'clean'.")
    if status_result.returncode == 0:
        lines = [l for l in status_result.stdout.splitlines() if l.strip()]
        staged = [l for l in lines if l[0] != " " and l[0] != "?"]
        unstaged = [l for l in lines if len(l) > 1 and l[1] != " " and l[0] != "?"]
        untracked = [l for l in lines if l.startswith("??")]

        if not lines:
            print(f"\n## Working tree: clean{_head_age_note()}")
        else:
            print(f"\n## Working tree ({len(lines)} changes)")
            if staged:
                print(f"\n### Staged ({len(staged)})")
                for l in (staged if full else staged[:20]):
                    print(f"  {l}")
                if not full and len(staged) > 20:
                    print(f"  ... ({len(staged) - 20} more)")









                diff_cmd = ["diff", "--name-only", "-z", "HEAD"]
                diff_head = _git(diff_cmd)
                if diff_head.returncode != 0 and _unborn_head():






                    pass
                elif diff_head.returncode != 0:
                    _note_failed(diff_cmd, diff_head)
                    print(f"⚠ {STAGED_PROVENANCE_UNKNOWN} — `git "
                          f"{' '.join(diff_cmd)}` did not answer "
                          f"({_reason(diff_head.returncode, _untrusted.flat(diff_head.stderr))}). "
                          f"This run did not check whether the staged content "
                          f"exists in any file here.")
                else:
                    differs = {p for p in diff_head.stdout.split("\0") if p}
                    absent = [l for l in staged if _staged_path(l) not in differs]
                    if absent:
                        print(f"⚠ {STAGED_ABSENT_MARKER} ({len(absent)}) — the "
                              f"index differs from HEAD while the file on disk "
                              f"matches it, so committing these would write "
                              f"content no file here has. Who staged them "
                              f"cannot be told from here: it is equally the "
                              f"shape of a `git checkout <sha> -- <path>` run "
                              f"by another process through a copied worktree "
                              f"(#1536) and of a stage you undid by hand.")
                        for l in (absent if full else absent[:20]):
                            print(f"    {l}")
                        if not full and len(absent) > 20:
                            print(f"    ... ({len(absent) - 20} more)")
            if unstaged:
                print(f"\n### Unstaged ({len(unstaged)})")
                for l in (unstaged if full else unstaged[:20]):
                    print(f"  {l}")
                if not full and len(unstaged) > 20:
                    print(f"  ... ({len(unstaged) - 20} more)")
            if untracked:





















                now = time.time()
                window = env_int("SUPERTOOL_WORKTREE_ACTIVE_WINDOW",
                                 ACTIVE_WINDOW_DEFAULT, minimum=1)
                root = _worktree_root()
                timed = [_untracked_age(root, l[3:], now) for l in untracked]
                print(f"\n### Untracked ({len(untracked)})")
                print(f"  (write time per path — nothing on disk records who "
                      f"wrote a file, so this is a time and not a verdict; "
                      f"#1724)")
                for l, (age, why) in zip(untracked if full else untracked[:10],
                                         timed):
                    print(_untracked_row(l, age, why, window))
                if not full and len(untracked) > 10:
















                    cut = timed[10:]
                    known = [a for a, _w in cut if a is not None]
                    blind = len(cut) - len(known)
                    if not known:
                        extra = (f", newest write among them UNKNOWN — no "
                                 f"mtime here could be read")
                    else:
                        extra = f", newest of them written {_age(min(known))} ago"
                        if blind:
                            extra += (f"; {blind} of {len(cut)} mtimes "
                                      f"unreadable, so a newer write may be "
                                      f"among them")
                    print(f"  ... ({len(untracked) - 10} more{extra})")


    stash_cmd = ["stash", "list"]
    stash_result = _git(stash_cmd)
    _note_failed(stash_cmd, stash_result)
    if stash_result.returncode != 0:



        print(f"\n## Stashes: UNKNOWN — `git {' '.join(stash_cmd)}` did not "
              f"answer "
              f"({_reason(stash_result.returncode, stash_result.stderr)}).")
    if stash_result.returncode == 0 and stash_result.stdout.strip():



        stashes = [_untrusted.visible(ln)
                   for ln in _untrusted.split_lines(stash_result.stdout.strip())]
        print(f"\n## Stashes ({len(stashes)})")
        for s in (stashes if full else stashes[:5]):
            print(f"  {s}")
        if not full and len(stashes) > 5:
            print(f"  ... ({len(stashes) - 5} more)")


    mr = _hosted_request(["glab", "mr", "view", branch_name, "--output", "json"])
    if mr is not None:
        mr_iid = mr.get("iid", "?")
        mr_title = _untrusted.flat(str(mr.get("title", "?")))
        mr_state = mr.get("state", "?")













        mr_target_raw = str(mr.get("target_branch", "?"))
        mr_target = _untrusted.flat(mr_target_raw)
        pipeline = mr.get("pipeline") or mr.get("head_pipeline") or {}
        if not isinstance(pipeline, dict):
            pipeline = {}



        pipe_status = pipeline.get("status") or _checks.NO_PIPELINE

        print(f"\n## MR !{mr_iid} — {mr_title}")
        print(f"State: {mr_state} | Target: {mr_target} | Pipeline: {pipe_status}")























        changes_count = mr.get("changes_count")
        if changes_count is None or changes_count == "" or changes_count == "0":
            print("Diff: EMPTY — branch has no commits ahead of target!")
        else:
            diff_line = f"Diff: {changes_count} files"
            target_ref = f"origin/{mr_target_raw}" if mr_target_raw != "?" else ""
            if target_ref:
                shortstat = _git(["diff", "--shortstat",
                                  f"{target_ref}...HEAD"], timeout=3)
                if shortstat.returncode == 0:
                    text = shortstat.stdout.strip()
                    if text:

                        adds = re.search(r"(\d+) insertions?", text)
                        dels = re.search(r"(\d+) deletions?", text)
                        a = adds.group(1) if adds else "0"
                        d = dels.group(1) if dels else "0"
                        diff_line += f" (+{a} -{d})"
                    else:
                        diff_line += " (+0 -0)"
                else:
                    diff_line += (
                        " (+? -? — `git diff --shortstat` did not answer: "
                        + _reason(shortstat.returncode, shortstat.stderr) + ")")
            else:
                diff_line += " (+? -? — no target branch reported for this MR/PR)"
            print(diff_line)


        desc = mr.get("description") or ""
        issue_match = re.search(r'#(\d{4,})', desc)
        if issue_match:
            print(f"Issue: #{issue_match.group(1)}")


    if mr is None:
        pr = _hosted_request(
            ["gh", "pr", "view", branch_name, "--json",
             "number,title,state,baseRefName,statusCheckRollup,body,"
             "additions,deletions,changedFiles,headRefOid,mergeable"])
        if pr is not None:
            pr_num = pr.get("number", "?")
            pr_title = _untrusted.flat(str(pr.get("title", "?")))
            pr_state = pr.get("state", "?")
            pr_target = _untrusted.flat(str(pr.get("baseRefName", "?")))


            pr_head = str(pr.get("headRefOid") or "")
            local = _git(["rev-parse", "HEAD"], timeout=3)
            local_head = local.stdout.strip() if local.returncode == 0 else ""





            relation = _checks.head_relation(local_head, pr_head, pr_num)

            check_states = _checks.github_states(pr.get("statusCheckRollup"))
            if check_states:





                check_summary = _checks.summarize_github(
                    pr.get("statusCheckRollup"))
            else:













                check_summary, _unused_merge_note = _checks.absence(
                    pr_state, _head_commit_age_secs(pr_head),
                    mergeable=pr.get("mergeable") if relation == "" else None,
                )

            print(f"\n## PR #{pr_num} — {pr_title}")
            print(f"State: {pr_state} | Target: {pr_target} | Checks: {check_summary}")



            if relation:
                print(relation)

            changed_files = pr.get("changedFiles", 0)
            if changed_files == 0:
                print("Diff: EMPTY — branch has no commits ahead of target!")
            else:
                print(f"Diff: {changed_files} files (+{pr.get('additions', 0)} -{pr.get('deletions', 0)})")





            print(_checks.linked_issue_line(
                _checks.closing_issue_refs(pr.get("body"))))




    note = _incomplete_note()
    if note:
        print(note)

    return 0


if __name__ == "__main__":
    sys.exit(main())
