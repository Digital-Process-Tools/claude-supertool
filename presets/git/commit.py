#!/usr/bin/env python3
"""Git commit — stage PATHS (optional) + commit MSG + verifiable receipt.

Receipt always shows:
  - HEAD before/after SHA
  - files committed + +/- lines
  - hook exit code + first error line if pre/post-commit blocks

Surfaces silent rollbacks: if HEAD is unchanged after the call,
that's printed loudly. Replaces the add/commit/log-1 cycle.

Special MSG values:
  --no-edit   Use prepared commit message (MERGE_MSG / CHERRY_PICK_HEAD).
              Only valid when a merge or cherry-pick is in progress.
"""
import os
import subprocess
import sys

# triple-colon separator handled by supertool; we receive plain argv here.


def _git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _head_sha() -> str:
    r = _git(["rev-parse", "--short", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else ""


def _first_error_line(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if "error" in low or "fatal" in low or "aborted" in low or "failed" in low or "❌" in s:
            return s
    # Fallback to last non-empty line
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: commit.py MSG [PATH ...]")
        return 1

    msg = sys.argv[1]
    paths = sys.argv[2:]
    no_edit = msg.strip() == "--no-edit"

    if not no_edit and not msg.strip():
        print("ERROR: commit message is empty.")
        return 1

    if _git(["rev-parse", "--git-dir"]).returncode != 0:
        print("ERROR: not inside a git repository.")
        return 1

    head_before = _head_sha()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    if no_edit:
        gd = _git(["rev-parse", "--git-dir"]).stdout.strip()
        in_merge = bool(gd) and (
            os.path.exists(os.path.join(gd, "MERGE_HEAD"))
            or os.path.exists(os.path.join(gd, "CHERRY_PICK_HEAD"))
        )
        if not in_merge:
            print("ERROR: --no-edit requires a merge or cherry-pick in progress "
                  "(no MERGE_HEAD/CHERRY_PICK_HEAD found).")
            return 1

    print(f"# git-commit on {branch}")
    print(f"HEAD before: {head_before}")

    # Stage PATHS if given
    if paths:
        add = _git(["add", "--"] + paths)
        if add.returncode != 0:
            print(f"ERROR: git add failed: {add.stderr.strip() or add.stdout.strip()}")
            return 1
        print(f"Staged: {len(paths)} path(s)")

    # Pre-commit staged check
    staged = _git(["diff", "--cached", "--name-only"])
    if staged.returncode != 0 or not staged.stdout.strip():
        print("ERROR: nothing staged. Use `git-commit:::MSG:::PATHS` or stage manually first.")
        return 1
    staged_files = [l for l in staged.stdout.splitlines() if l.strip()]

    # Commit
    if no_edit:
        result = _git(["commit", "--no-edit"])
    else:
        result = _git(["commit", "-m", msg])
    head_after = _head_sha()

    if result.returncode == 0 and head_after and head_after != head_before:
        new_sha = head_after
        print(f"HEAD after:  {new_sha} ✓")
        # Files + line stats from new commit
        stat = _git(["show", "--shortstat", "--format=", new_sha])
        if stat.returncode == 0 and stat.stdout.strip():
            print(stat.stdout.strip().splitlines()[-1].strip())
        print(f"Files committed: {len(staged_files)}")
        for f in staged_files[:20]:
            print(f"  {f}")
        if len(staged_files) > 20:
            print(f"  … {len(staged_files) - 20} more")
        # Next-step hint
        upstream_res = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
        if upstream_res.returncode == 0 and upstream_res.stdout.strip():
            print("Next: git push (or ./supertool 'mr:.max/mr.md|TIME|LABELS' for push+MR)")
        else:
            print("Next: git push -u origin HEAD (no upstream set)")
        return 0

    # Failure path — could be hook block, validation, or silent rollback
    print(f"HEAD after:  {head_after or '?'} ✗")
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    err = _first_error_line(combined)

    if head_after and head_before and head_after == head_before:
        print("Status: COMMIT NOT APPLIED (HEAD unchanged)")
    else:
        print(f"Status: commit returned exit {result.returncode}")

    if err:
        print(f"First error: {err}")
    print("\n--- git output ---")
    print(combined.strip() or "(no output)")
    print("\nBypass hooks (only if intentional): git commit --no-verify -m '...'")
    return result.returncode or 1


if __name__ == "__main__":
    sys.exit(main())
