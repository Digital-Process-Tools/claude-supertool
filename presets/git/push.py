#!/usr/bin/env python3
"""Git push — update the current branch's remote + verifiable receipt.

Closes the loop for the common case the `mr` op doesn't cover: pushing a
fix to an MR that already exists. `mr` creates; this updates.

Receipt always shows:
  - branch + upstream (sets upstream on first push)
  - commits pushed (remote SHA before/after) or "already up to date"
  - ahead/behind vs the remote afterwards
  - open MR/PR for the branch + pipeline status (push triggers a run)

Non-fast-forward rejections are surfaced loudly with a hint, never
auto-forced — mirrors git-commit's no-silent-bypass philosophy.
"""
from __future__ import annotations

import os
import sys

# Sibling import: runtime puts this dir on sys.path[0]; the test harness
# loads scripts via importlib (no dir on path), so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _git_common import _first_error_line, _git, query_open_mr  # noqa: E402


def _upstream_ref() -> str:
    """Configured upstream of HEAD (e.g. origin/foo), or empty if none."""
    r = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    return r.stdout.strip() if r.returncode == 0 else ""


def _remote_sha(ref: str) -> str:
    if not ref:
        return ""
    r = _git(["rev-parse", "--short", ref])
    return r.stdout.strip() if r.returncode == 0 else ""


def _open_mr_line(branch: str) -> str:
    """One-line MR/PR summary for the post-push receipt, or empty."""
    mr = query_open_mr(branch)
    if not mr:
        return ""
    if mr["source"] == "gitlab":
        return (f"MR !{mr['iid']} → {mr['target']} | "
                f"pipeline: {mr['pipeline'] or 'triggered'}")
    return f"PR #{mr['iid']} → {mr['target']} | checks triggered"


def main() -> int:
    if _git(["rev-parse", "--git-dir"]).returncode != 0:
        print("ERROR: not inside a git repository.")
        return 1

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if not branch or branch == "HEAD":
        print("ERROR: detached HEAD — checkout a branch before pushing.")
        return 1

    upstream = _upstream_ref()
    has_upstream = bool(upstream)
    remote_before = _remote_sha(upstream) if has_upstream else ""

    print(f"# git-push on {branch}")
    if has_upstream:
        print(f"Upstream: {upstream}" + (f" @ {remote_before}" if remote_before else ""))
    else:
        print("Upstream: none — setting on first push (origin)")

    if has_upstream:
        result = _git(["push"], timeout=120)
    else:
        result = _git(["push", "-u", "origin", "HEAD"], timeout=120)

    combined = (result.stdout or "") + "\n" + (result.stderr or "")

    if result.returncode != 0:
        print("Status: PUSH REJECTED ✗")
        err = _first_error_line(combined)
        if err:
            print(f"First error: {err}")
        if "non-fast-forward" in combined or "rejected" in combined.lower():
            print("Hint: remote has commits you lack — `git pull --rebase` then "
                  "retry, or force only if intentional: "
                  "`git push --force-with-lease`")
        print("\n--- git output ---")
        print(combined.strip() or "(no output)")
        return result.returncode

    # Success — recompute upstream (now set if it was the first push)
    upstream = upstream or _upstream_ref()
    remote_after = _remote_sha(upstream)

    print("Status: pushed ✓")
    if remote_before and remote_after and remote_before != remote_after:
        rng = _git(["rev-list", "--count", f"{remote_before}..{remote_after}"])
        n = rng.stdout.strip() if rng.returncode == 0 else "?"
        print(f"Remote {remote_before} → {remote_after} ({n} commit(s))")
    elif not remote_before and remote_after:
        print(f"Remote now at {remote_after} (branch created)")
    else:
        print("Already up to date — nothing to push")

    # Ahead/behind vs upstream after the push (should be in sync on success)
    ab = _git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
    if ab.returncode == 0:
        parts = ab.stdout.strip().split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
            if ahead or behind:
                print(f"vs upstream: ahead {ahead}, behind {behind}")
            else:
                print("vs upstream: in sync")

    mr_line = _open_mr_line(branch)
    if mr_line:
        print(mr_line)

    return 0


if __name__ == "__main__":
    sys.exit(main())
