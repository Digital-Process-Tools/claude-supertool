#!/usr/bin/env python3
"""GitHub pull request details via gh CLI."""
import json
import re
import subprocess
import sys

DESCRIPTION_MAX = 2000
COMMENT_MAX = 500


def _gh(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a gh command and return the result."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify gh errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "could not resolve" in s or "404" in s or "not found" in s:
        return f"ERROR: {resource} #{identifier} not found in this repo. Check the number or verify you're in the right repo (gh repo view)."
    if "auth" in s or "login" in s or "token" in s:
        return f"ERROR: gh CLI not authenticated. Run: gh auth login"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes and retry."
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check repo access (gh auth status)."
    return f"ERROR: gh failed for {resource} #{identifier}: {stderr.strip()}"


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: pr.py NUMBER_OR_BRANCH [status]")
        return 1

    arg = sys.argv[1]
    slim = len(sys.argv) > 2 and sys.argv[2] == "status"

    # If not all digits, treat as branch name
    if not arg.isdigit():
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
                    # Try closed PRs too
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

    # Fetch PR with all needed fields
    try:
        result = _gh([
            "pr", "view", arg, "--json",
            "number,title,state,author,headRefName,baseRefName,labels,"
            "milestone,reviewDecision,reviews,mergeCommit,mergeable,"
            "isDraft,url,body,comments,additions,deletions,changedFiles,"
            "statusCheckRollup"
        ])
    except FileNotFoundError:
        print("ERROR: gh not found — install from https://cli.github.com")
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
        print(f"ERROR: invalid JSON from gh\n{result.stdout[:500]}")
        return 1

    if slim:
        iid = d.get("number", arg)
        state = d.get("state", "?")
        mergeable = d.get("mergeable", "?")
        review_decision = d.get("reviewDecision") or "none"
        checks = d.get("statusCheckRollup", [])
        passed = sum(1 for c in checks if c.get("conclusion") == "SUCCESS")
        failed = sum(1 for c in checks if c.get("conclusion") == "FAILURE")
        pending = sum(1 for c in checks if c.get("status") == "IN_PROGRESS" or c.get("conclusion") is None)
        merge_commit = (d.get("mergeCommit") or {}).get("oid", "")
        web_url = d.get("url", "")
        conflicts = "yes" if mergeable == "CONFLICTING" else "no"
        print(f"#{iid} | state: {state} | mergeable: {mergeable} | conflicts: {conflicts}")
        print(f"checks: {passed} passed, {failed} failed, {pending} pending")
        print(f"review: {review_decision}")
        if merge_commit:
            print(f"merge_commit: {merge_commit[:12]}")
        if web_url:
            print(f"url: {web_url}")
        return 0

    title = d.get("title", "?")
    state = d.get("state", "?")
    iid = d.get("number", arg)
    source = d.get("headRefName", "?")
    target = d.get("baseRefName", "?")
    author = (d.get("author") or {}).get("login", "?")
    web_url = d.get("url", "")
    labels = ", ".join(l.get("name", "?") for l in d.get("labels", [])) or "none"
    milestone = (d.get("milestone") or {}).get("title", "none")
    draft = d.get("isDraft", False)
    mergeable = d.get("mergeable", "?")
    review_decision = d.get("reviewDecision") or "none"
    merge_commit = (d.get("mergeCommit") or {}).get("oid", "")
    additions = d.get("additions", "?")
    deletions = d.get("deletions", "?")
    changed_files = d.get("changedFiles", "?")

    # Header
    draft_marker = " [DRAFT]" if draft else ""
    print(f"# #{iid} {title}{draft_marker}")
    print(f"State: {state} | Author: {author}")
    print(f"Branch: {source} -> {target}")
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")

    # Reviews
    reviews = d.get("reviews", [])
    if reviews:
        reviewers = {}
        for r in reviews:
            login = (r.get("author") or {}).get("login", "?")
            r_state = r.get("state", "?")
            reviewers[login] = r_state  # latest review state per reviewer
        parts = [f"{login} ({state})" for login, state in reviewers.items()]
        print(f"Reviews: {', '.join(parts)}")
    print(f"Review decision: {review_decision}")

    # Checks (CI status)
    checks = d.get("statusCheckRollup", [])
    if checks:
        passed = sum(1 for c in checks if c.get("conclusion") == "SUCCESS")
        failed = sum(1 for c in checks if c.get("conclusion") == "FAILURE")
        pending = sum(1 for c in checks if c.get("status") == "IN_PROGRESS" or c.get("conclusion") is None)
        print(f"Checks: {passed} passed, {failed} failed, {pending} pending")
    else:
        print("Checks: none")

    # Changes
    print(f"Changes: {changed_files} files, +{additions} -{deletions}")

    # Mergeable
    if mergeable == "CONFLICTING":
        print("Conflicts: YES — cannot merge")
    elif mergeable == "MERGEABLE":
        print("Mergeable: yes")
    else:
        print(f"Mergeable: {mergeable}")

    # Merge commit
    if merge_commit:
        print(f"Merge commit: {merge_commit[:12]}")

    if web_url:
        print(f"URL: {web_url}")

    # Linked issue — extract from body (Closes #N, Fixes #N, Resolves #N, or bare #N)
    body_raw = d.get("body") or ""
    issue_match = re.search(r'(?:closes|fixes|resolves)?\s*#(\d+)', body_raw, re.IGNORECASE)
    if issue_match:
        issue_num = issue_match.group(1)
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
            print(f"\nIssue: #{issue_num}")

    # Description
    body = (d.get("body") or "")[:DESCRIPTION_MAX]
    if body:
        print(f"\n## Description\n{body}")

    # Comments
    comments = d.get("comments", [])
    if comments:
        print(f"\n## Comments ({len(comments)})")
        for c in comments[-10:]:
            c_author = (c.get("author") or {}).get("login", "?")
            c_body = (c.get("body") or "")[:COMMENT_MAX]
            c_created = (c.get("createdAt") or "")[:10]
            print(f"\n**{c_author}** ({c_created}):")
            print(c_body)
    else:
        print(f"\n## Comments (0)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
