#!/usr/bin/env python3
"""Git resolve — pick ours/theirs for a conflicted PATH (or all) + stage.

Atomic: checkout --SIDE PATH + git add PATH. Receipt shows which
files were resolved and how many conflicts remain.
"""
import subprocess
import sys


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


def main() -> int:
    if len(sys.argv) < 3:
        print("ERROR: usage: resolve.py SIDE PATH")
        print("  SIDE — 'ours' or 'theirs'")
        print("  PATH — conflicted file path, or 'all' for every UU file")
        return 1

    side = sys.argv[1].lower()
    target = sys.argv[2]

    if side not in ("ours", "theirs"):
        print(f"ERROR: SIDE must be 'ours' or 'theirs', got {side!r}")
        return 1

    if _git(["rev-parse", "--git-dir"]).returncode != 0:
        print("ERROR: not inside a git repository.")
        return 1

    all_conflicts = _list_conflicts()
    if not all_conflicts:
        print("# git-resolve")
        print("No conflicted files. Nothing to resolve.")
        return 0

    if target == "all":
        targets = all_conflicts
    else:
        if target not in all_conflicts:
            print(f"ERROR: {target!r} is not a conflicted file.")
            print(f"Conflicts: {', '.join(all_conflicts) or '(none)'}")
            return 1
        targets = [target]

    print(f"# git-resolve: {side} ({len(targets)} file(s))")

    resolved: list[str] = []
    failed: list[tuple[str, str]] = []
    for path in targets:
        co = _git(["checkout", f"--{side}", "--", path])
        if co.returncode != 0:
            failed.append((path, co.stderr.strip() or co.stdout.strip()))
            continue
        add = _git(["add", "--", path])
        if add.returncode != 0:
            failed.append((path, add.stderr.strip() or add.stdout.strip()))
            continue
        resolved.append(path)

    for path in resolved:
        print(f"  ✓ {path}")
    for path, err in failed:
        print(f"  ✗ {path}: {err}")

    remaining = _list_conflicts()
    print(f"\nResolved: {len(resolved)} | Failed: {len(failed)} | Remaining: {len(remaining)}")
    if remaining:
        print("Still conflicted:")
        for p in remaining:
            print(f"  {p}")
        print("Next: ./supertool 'git-conflicts' to inspect, or rerun git-resolve.")
    elif resolved:
        # Detect state to give the right continue command
        gd = _git(["rev-parse", "--git-dir"]).stdout.strip()
        from os.path import exists, join
        if exists(join(gd, "MERGE_HEAD")):
            print("Next: ./supertool 'git-commit:::Merge resolved' (or git merge --continue)")
        elif exists(join(gd, "rebase-merge")) or exists(join(gd, "rebase-apply")):
            print("Next: git rebase --continue")
        elif exists(join(gd, "CHERRY_PICK_HEAD")):
            print("Next: git cherry-pick --continue")
        else:
            print("Next: ./supertool 'git-commit:::MSG' to commit the resolution.")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
