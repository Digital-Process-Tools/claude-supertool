#!/usr/bin/env python3
"""Git conflicts — list UU files + extract conflict blocks.

For when you're already mid-merge (or mid-rebase / mid-cherry-pick)
and want to see all conflicts in one call without re-running merge.
"""
from __future__ import annotations

import os
import subprocess
import sys

DEFAULT_PREVIEW_LINES = 12


def _git(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _list_conflicts() -> list[str]:
    res = _git(["diff", "--name-only", "--diff-filter=U"])
    if res.returncode != 0:
        return []
    return [l for l in res.stdout.splitlines() if l.strip()]


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
                # skip to end of block
                in_block = False
    if not out:
        return "  (no <<<<<<< marker found — likely binary or stage-only conflict)"
    return "\n".join(out)


def _detect_state() -> str:
    res = _git(["rev-parse", "--git-dir"])
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
    preview = int(os.environ.get("SUPERTOOL_PREVIEW_LINES", str(DEFAULT_PREVIEW_LINES)))

    if _git(["rev-parse", "--git-dir"]).returncode != 0:
        print("ERROR: not inside a git repository.")
        return 1

    state = _detect_state()
    conflicts = _list_conflicts()

    print("# git-conflicts")
    if state:
        print(f"State: {state} in progress")
    else:
        print("State: no merge/rebase/cherry-pick in progress")

    if not conflicts:
        print("No conflicted files.")
        return 0

    print(f"Conflicts: {len(conflicts)} file(s)")
    if state == "merge":
        print("Abort: git merge --abort")
    elif state == "rebase":
        print("Abort: git rebase --abort")
    elif state == "cherry-pick":
        print("Abort: git cherry-pick --abort")

    for path in conflicts:
        print(f"\n## {path}")
        print(_all_conflict_blocks(path, preview))

    print("\nResolve: ./supertool 'git-resolve:::ours:::PATH' | ./supertool 'git-resolve:::theirs:::PATH'")
    print("Or edit manually, then: git add PATH && git commit")

    return 0


if __name__ == "__main__":
    sys.exit(main())
