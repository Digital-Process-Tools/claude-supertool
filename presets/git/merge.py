#!/usr/bin/env python3
"""Git merge — merge REF and on conflict surface what to fix.

Combines: fetch (optional) + merge + on conflict: list UU files +
extract first conflict block per file + show merge-base/ours/theirs
SHAs + suggest abort command. Replaces the merge/status/read-each-file
hunt with one round-trip.
"""
from __future__ import annotations

import os
import subprocess
import sys

DEFAULT_PREVIEW_LINES = 12


def _git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _list_conflicts() -> list[str]:
    res = _git(["diff", "--name-only", "--diff-filter=U"])
    if res.returncode != 0:
        return []
    return [l for l in res.stdout.splitlines() if l.strip()]


def _first_conflict_block(path: str, max_lines: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return f"  (could not read: {e})"

    out: list[str] = []
    in_block = False
    block_start = 0
    for i, line in enumerate(lines, 1):
        if line.startswith("<<<<<<<"):
            in_block = True
            block_start = i
            out.append(f"  L{i}: {line.rstrip()}")
            continue
        if in_block:
            out.append(f"  L{i}: {line.rstrip()}")
            if line.startswith(">>>>>>>"):
                break
            if i - block_start >= max_lines:
                out.append(f"  … (block continues, truncated at {max_lines} lines)")
                break
    if not out:
        return "  (no <<<<<<< marker found — likely binary or already resolved)"
    return "\n".join(out)


def _count_blocks(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.startswith("<<<<<<<"))
    except OSError:
        return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: merge.py REF")
        return 1

    ref = sys.argv[1]
    # #150: a REF like `--abort` would call `git merge --abort` (state-mutating).
    # `-X theirs` smuggles a strategy option. Refuse leading-dash refs.
    if ref.startswith("-"):
        print(f"ERROR: ref starts with '-' (refusing for safety): {ref!r}")
        return 1
    preview = int(os.environ.get("SUPERTOOL_PREVIEW_LINES", str(DEFAULT_PREVIEW_LINES)))

    if _git(["rev-parse", "--verify", "--quiet", ref]).returncode != 0:
        print(f"ERROR: ref {ref!r} not found. Try `git fetch` first.")
        return 1

    head_before = _git(["rev-parse", "--short", "HEAD"]).stdout.strip()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    their_sha = _git(["rev-parse", "--short", ref]).stdout.strip()
    mb_res = _git(["merge-base", "HEAD", ref])
    merge_base = mb_res.stdout.strip()[:12] if mb_res.returncode == 0 else "?"

    print(f"# git-merge: {ref}@{their_sha} into {branch}@{head_before}")
    print(f"Merge-base: {merge_base}")

    result = _git(["merge", "--no-edit", ref])
    head_after = _git(["rev-parse", "--short", "HEAD"]).stdout.strip()

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode == 0:
        print(f"Status: clean merge ({head_before} → {head_after})")
        if stdout:
            print(stdout)
        return 0

    conflicts = _list_conflicts()
    if not conflicts:
        print(f"Status: merge failed (no conflicts detected — abort or fix)")
        if stderr:
            print(stderr)
        if stdout:
            print(stdout)
        return 1

    print(f"Status: CONFLICT ({len(conflicts)} file(s))")
    print(f"Ours: {head_before} | Theirs: {their_sha} | Base: {merge_base}")
    print("Next:")
    print("  - Edit files manually, then ./supertool 'git-commit:::resolve merge'")
    print("  - Or pick a side: ./supertool 'git-resolve:::ours:::PATH' (or theirs, or all)")
    print("  - Or abort: git merge --abort")

    for path in conflicts:
        nblocks = _count_blocks(path)
        print(f"\n## {path} ({nblocks} block(s))")
        print(_first_conflict_block(path, preview))

    return 1


if __name__ == "__main__":
    sys.exit(main())
