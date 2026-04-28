#!/usr/bin/env python3
"""Git status dashboard — where am I, what's changed, what's stashed.

Combines branch info, recent commits, working tree state, and stash
list into one structured report.
"""
import subprocess
import sys


def _git(args: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
    """Run a git command."""
    return subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def main() -> int:
    # 1. Branch + tracking
    branch_result = _git(["branch", "-vv", "--no-color"])
    if branch_result.returncode != 0:
        stderr = branch_result.stderr.lower()
        if "not a git repository" in stderr:
            print("ERROR: not inside a git repository.")
        else:
            print(f"ERROR: git failed: {branch_result.stderr.strip()}")
        return 1

    current_branch = ""
    tracking = ""
    for line in branch_result.stdout.splitlines():
        if line.startswith("* "):
            current_branch = line[2:].strip()
            break

    # Cleaner branch + remote info
    branch_name_result = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    branch_name = branch_name_result.stdout.strip() if branch_name_result.returncode == 0 else "?"

    # Ahead/behind
    ahead_behind = ""
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

    print(f"# git-status")
    print(f"Branch: {branch_name}" + (f" ({ahead_behind})" if ahead_behind else ""))

    # 2. Last 5 commits
    log_result = _git(["log", "-5", "--format=%h %ad %an | %s", "--date=short"])
    if log_result.returncode == 0 and log_result.stdout.strip():
        print(f"\n## Last 5 commits")
        for line in log_result.stdout.strip().splitlines():
            print(f"  {line}")

    # 3. Working tree
    status_result = _git(["status", "--porcelain=v1"])
    if status_result.returncode == 0:
        lines = [l for l in status_result.stdout.splitlines() if l.strip()]
        staged = [l for l in lines if l[0] != " " and l[0] != "?"]
        unstaged = [l for l in lines if len(l) > 1 and l[1] != " " and l[0] != "?"]
        untracked = [l for l in lines if l.startswith("??")]

        if not lines:
            print(f"\n## Working tree: clean")
        else:
            print(f"\n## Working tree ({len(lines)} changes)")
            if staged:
                print(f"\n### Staged ({len(staged)})")
                for l in staged[:20]:
                    print(f"  {l}")
                if len(staged) > 20:
                    print(f"  ... ({len(staged) - 20} more)")
            if unstaged:
                print(f"\n### Unstaged ({len(unstaged)})")
                for l in unstaged[:20]:
                    print(f"  {l}")
                if len(unstaged) > 20:
                    print(f"  ... ({len(unstaged) - 20} more)")
            if untracked:
                print(f"\n### Untracked ({len(untracked)})")
                for l in untracked[:10]:
                    print(f"  {l[3:]}")
                if len(untracked) > 10:
                    print(f"  ... ({len(untracked) - 10} more)")

    # 4. Stash
    stash_result = _git(["stash", "list"])
    if stash_result.returncode == 0 and stash_result.stdout.strip():
        stashes = stash_result.stdout.strip().splitlines()
        print(f"\n## Stashes ({len(stashes)})")
        for s in stashes[:5]:
            print(f"  {s}")
        if len(stashes) > 5:
            print(f"  ... ({len(stashes) - 5} more)")

    # 5. MR/PR for current branch (try glab, then gh — skip if neither available)
    import json as _json
    import re as _re

    mr_found = False

    # Try GitLab (glab)
    try:
        glab_result = subprocess.run(
            ["glab", "mr", "view", branch_name, "--output", "json"],
            capture_output=True, text=True, timeout=5,
        )
        if glab_result.returncode == 0:
            mr = _json.loads(glab_result.stdout)
            mr_iid = mr.get("iid", "?")
            mr_title = mr.get("title", "?")
            mr_state = mr.get("state", "?")
            mr_target = mr.get("target_branch", "?")
            pipeline = mr.get("pipeline") or mr.get("head_pipeline") or {}
            pipe_status = pipeline.get("status", "none")

            print(f"\n## MR !{mr_iid} — {mr_title}")
            print(f"State: {mr_state} | Target: {mr_target} | Pipeline: {pipe_status}")

            # Extract linked issue from description
            desc = mr.get("description") or ""
            issue_match = _re.search(r'#(\d{4,})', desc)
            if issue_match:
                print(f"Issue: #{issue_match.group(1)}")
            mr_found = True
    except (FileNotFoundError, subprocess.TimeoutExpired, _json.JSONDecodeError):
        pass

    # Try GitHub (gh) if glab didn't find anything
    if not mr_found:
        try:
            gh_result = subprocess.run(
                ["gh", "pr", "view", branch_name, "--json",
                 "number,title,state,baseRefName,statusCheckRollup,body"],
                capture_output=True, text=True, timeout=5,
            )
            if gh_result.returncode == 0:
                pr = _json.loads(gh_result.stdout)
                pr_num = pr.get("number", "?")
                pr_title = pr.get("title", "?")
                pr_state = pr.get("state", "?")
                pr_target = pr.get("baseRefName", "?")
                checks = pr.get("statusCheckRollup", [])
                check_summary = "none"
                if checks:
                    passed = sum(1 for c in checks if c.get("conclusion") == "SUCCESS")
                    failed = sum(1 for c in checks if c.get("conclusion") == "FAILURE")
                    check_summary = f"{passed} passed, {failed} failed"

                print(f"\n## PR #{pr_num} — {pr_title}")
                print(f"State: {pr_state} | Target: {pr_target} | Checks: {check_summary}")

                # Extract linked issue
                body = pr.get("body") or ""
                issue_match = _re.search(r'(?:closes|fixes|resolves)?\s*#(\d+)', body, _re.IGNORECASE)
                if issue_match:
                    print(f"Issue: #{issue_match.group(1)}")
                mr_found = True
        except (FileNotFoundError, subprocess.TimeoutExpired, _json.JSONDecodeError):
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
