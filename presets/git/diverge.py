#!/usr/bin/env python3
"""Git diverge — what's in BRANCH vs BASE in one call.

Combines: ahead/behind count + commits-only-in-branch (oneline) +
files changed (name-status) + +/- line totals. Replaces the
log-A..B / log-B..A / diff--stat trio.
"""
from __future__ import annotations

import os
import subprocess
import sys

DEFAULT_BASE = "master"
DEFAULT_MAX_COMMITS = 30


def _git(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _resolve_base(arg: str) -> str:
    """Use arg if given, else master, else main."""
    if arg:
        return arg
    for c in ("master", "main"):
        if _git(["rev-parse", "--verify", "--quiet", c]).returncode == 0:
            return c
    return DEFAULT_BASE


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: diverge.py BRANCH [BASE]")
        print("  BRANCH — branch to inspect (or 'HEAD')")
        print("  BASE   — defaults to master, fallback main")
        return 1

    branch = sys.argv[1]
    base = _resolve_base(sys.argv[2] if len(sys.argv) > 2 else "")
    max_commits = int(os.environ.get("SUPERTOOL_MAX_COMMITS", str(DEFAULT_MAX_COMMITS)))

    # Verify both refs exist
    for ref in (branch, base):
        if _git(["rev-parse", "--verify", "--quiet", ref]).returncode != 0:
            print(f"ERROR: ref {ref!r} not found. Did you fetch?")
            return 1

    print(f"# git-diverge: {branch} vs {base}")

    # Ahead/behind
    ab = _git(["rev-list", "--left-right", "--count", f"{base}...{branch}"])
    if ab.returncode != 0:
        print(f"ERROR: {ab.stderr.strip()}")
        return 1
    parts = ab.stdout.strip().split()
    if len(parts) != 2:
        print("ERROR: unexpected rev-list output")
        return 1
    behind, ahead = int(parts[0]), int(parts[1])
    print(f"Ahead: {ahead}, Behind: {behind}")

    if ahead == 0 and behind == 0:
        print("Branches identical.")
        return 0
    if ahead and behind:
        print(f"Next: ./supertool 'git-merge:{base}' (merge) or git rebase {base} (rebase)")
    elif behind and not ahead:
        print(f"Next: git reset --hard {base} (or fast-forward via merge)")

    # Merge base
    mb_res = _git(["merge-base", base, branch])
    if mb_res.returncode == 0:
        print(f"Merge-base: {mb_res.stdout.strip()[:12]}")

    # Commits in branch but not base
    if ahead:
        log = _git(["log", f"-{max_commits}", f"{base}..{branch}",
                    "--format=%h %ad %an | %s", "--date=short"])
        if log.returncode == 0 and log.stdout.strip():
            shown = log.stdout.strip().splitlines()
            print(f"\n## Commits in {branch} not in {base} ({len(shown)} of {ahead})")
            for line in shown:
                print(f"  {line}")
            if ahead > len(shown):
                print(f"  … {ahead - len(shown)} more")

    # Files changed (name-status)
    if ahead:
        ns = _git(["diff", "--name-status", f"{base}...{branch}"])
        if ns.returncode == 0 and ns.stdout.strip():
            files = ns.stdout.strip().splitlines()
            print(f"\n## Files changed ({len(files)})")
            for line in files[:50]:
                print(f"  {line}")
            if len(files) > 50:
                print(f"  … {len(files) - 50} more")

        # +/- totals
        stat = _git(["diff", "--shortstat", f"{base}...{branch}"])
        if stat.returncode == 0 and stat.stdout.strip():
            print(f"\n{stat.stdout.strip()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
