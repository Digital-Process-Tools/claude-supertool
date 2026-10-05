#!/usr/bin/env python3





from __future__ import annotations

import json
import os
import subprocess
import sys



_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))  

from _git_common import (  
    NOT_A_REPO, TIMEOUT_RC, _git, _list_conflicts, probe_repo, st_hint,
    unanswered_repo_lines, use_utf8_stdout,
)
from _env import env_int  
import _untrusted  
from _spawnable import which_excluding_cwd  

DEFAULT_PREVIEW_LINES = 12

_STATE_TO_REF = {
    "merge": "MERGE_HEAD",
    "cherry-pick": "CHERRY_PICK_HEAD",
    "revert": "REVERT_HEAD",
}


def _all_conflict_blocks(path: str, max_lines_per_block: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return f"  (could not read: {e})"

    out: list[str] = []
    in_block = False
    block_idx = 0
    block_start = 0
    for i, line in enumerate(lines, 1):
        if line.startswith("<<<<<<<"):
            block_idx += 1
            in_block = True
            block_start = i
            out.append(f"  --- block {block_idx} ---")
            out.append(f"  L{i}: {line.rstrip()}")
            continue
        if in_block:
            out.append(f"  L{i}: {line.rstrip()}")
            if line.startswith(">>>>>>>"):
                in_block = False
            elif i - block_start >= max_lines_per_block:
                out.append(f"  … (truncated at {max_lines_per_block} lines)")

                in_block = False
    if not out:
        return "  (no <<<<<<< marker found — likely binary or stage-only conflict)"
    return "\n".join(out)


def _incoming_branch(ref: str) -> str:

    res = _git(["name-rev", "--name-only", "--exclude=refs/tags/*", ref])
    if res.returncode != 0:
        return ""
    name = res.stdout.strip()
    if not name or name == "undefined":
        return ""
    for prefix in ("remotes/origin/", "remotes/upstream/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.split("~", 1)[0].split("^", 1)[0]


def _incoming_mr(branch: str) -> str:






    if not branch:
        return ""
    glab_bin = which_excluding_cwd("glab")
    if glab_bin:
        try:
            res = subprocess.run(
                [glab_bin, "mr", "list", "--source-branch", branch, "--state", "opened", "--output", "json"],
                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
            )
            if res.returncode == 0 and res.stdout.strip().startswith("["):
                mrs = json.loads(res.stdout)
                if mrs:
                    mr = mrs[0]
                    return f"!{mr.get('iid', '?')} {mr.get('title', '')}".strip()
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass
    gh_bin = which_excluding_cwd("gh")
    if gh_bin:
        try:
            res = subprocess.run(
                [gh_bin, "pr", "list", "--head", branch, "--state", "open",
                 "--json", "number,title", "--limit", "1"],
                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
            )
            if res.returncode == 0 and res.stdout.strip().startswith("["):
                prs = json.loads(res.stdout)
                if prs:
                    pr = prs[0]
                    return f"#{pr.get('number', '?')} {pr.get('title', '')}".strip()
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass
    return ""


def _incoming_info(path: str, state: str) -> list[str]:

    ref = _STATE_TO_REF.get(state)
    if not ref:
        return []
    log = _git(["log", "-1", "--format=%h %an %ar :: %s", ref, "--", path])
    if log.returncode != 0 or not log.stdout.strip():
        return []
    lines = [f"  Last touched (theirs): {log.stdout.strip()}"]
    if state == "merge":
        branch = _incoming_branch(ref)
        if branch:
            lines.append(f"  Incoming branch: {branch}")
            mr = _incoming_mr(branch)
            if mr:
                lines.append(f"  MR: {mr}")
    return lines







STATE_UNKNOWN = "unknown"


def _detect_state() -> str:













    res = _git(["rev-parse", "--git-dir"])
    if res.returncode == TIMEOUT_RC:
        return STATE_UNKNOWN
    if res.returncode != 0:
        return ""
    gd = res.stdout.strip()
    from os.path import exists, join
    if exists(join(gd, "MERGE_HEAD")):
        return "merge"
    if exists(join(gd, "CHERRY_PICK_HEAD")):
        return "cherry-pick"
    if exists(join(gd, "REVERT_HEAD")):
        return "revert"
    if exists(join(gd, "rebase-merge")) or exists(join(gd, "rebase-apply")):
        return "rebase"
    return ""


def main() -> int:
    use_utf8_stdout()
    preview = env_int(os.environ.get("SUPERTOOL_PREVIEW_LINES"), "SUPERTOOL_PREVIEW_LINES", DEFAULT_PREVIEW_LINES, minimum=0)

    inside, why = probe_repo(_git)
    if inside is None:
        for line in unanswered_repo_lines(why):
            print(line)
        return 1
    if not inside:
        print(NOT_A_REPO)
        return 1

    state = _detect_state()
    conflicts, unavailable = _list_conflicts()

    print("# git-conflicts")
    if state == STATE_UNKNOWN:
        print("State: UNKNOWN — `git rev-parse --git-dir` did not answer. "
              "This is NOT 'nothing in progress'.")
    elif state:
        print(f"State: {state} in progress")
    else:
        print("State: no merge/rebase/cherry-pick in progress")

    if unavailable:
        print(f"Conflicts: UNKNOWN — `git diff --name-only --diff-filter=U` "
              f"did not answer: {unavailable}")
        print("This is NOT 'no conflicts'. Nothing was inspected, so do not "
              "commit or resolve on the strength of this report — re-run it.")
        return 1

    if not conflicts:
        print("No conflicted files.")





        return 1 if state == STATE_UNKNOWN else 0

    print(f"Conflicts: {len(conflicts)} file(s)")
    if state == "merge":
        print("Abort: git merge --abort")
    elif state == "rebase":
        print("Abort: git rebase --abort")
    elif state == "cherry-pick":
        print("Abort: git cherry-pick --abort")

    for path in conflicts:






        print(f"\n## {_untrusted.flat(path, disclose_newline=True)}")
        for line in _incoming_info(path, state):
            print(line)
        print(_all_conflict_blocks(path, preview))




    print("\nResolve: " + st_hint("git-resolve:::ours:::PATH")
          + " | " + st_hint("git-resolve:::theirs:::PATH"))
    print("Keep both sides (union): " + st_hint("git-resolve:::both:::PATH"))
    print("Or edit manually, then: git add PATH && git commit")




    return 1 if state == STATE_UNKNOWN else 0


if __name__ == "__main__":
    sys.exit(main())
