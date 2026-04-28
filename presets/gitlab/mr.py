#!/usr/bin/env python3
"""GitLab merge request details via glab CLI.

Fetches MR metadata, pipeline status, reviewer/approval info,
diff stats, and human comments. Dashboard-style output.
"""
import json
import re
import subprocess
import sys

DESCRIPTION_MAX = 2000
COMMENT_MAX = 500


def _glab_api(endpoint: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a glab api call."""
    return subprocess.run(
        ["glab", "api", endpoint],
        capture_output=True, text=True, timeout=timeout,
    )


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify glab errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "404" in s or "not found" in s or "could not resolve" in s:
        return f"ERROR: {resource} #{identifier} not found in this repo. Check the number or verify you're in the right repo."
    if "401" in s or "unauthorized" in s or "token" in s:
        return "ERROR: glab not authenticated. Run: glab auth login"
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."
    return f"ERROR: glab failed for {resource} #{identifier}: {stderr.strip()}"


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: mr.py NUMBER")
        return 1

    arg = sys.argv[1]

    # If not all digits, treat as branch name and resolve to MR number
    if not arg.isdigit():
        try:
            branch_result = _glab_api(
                f"projects/:id/merge_requests?source_branch={arg}&state=opened&per_page=1"
            )
            if branch_result.returncode == 0:
                mrs = json.loads(branch_result.stdout)
                if isinstance(mrs, list) and mrs:
                    arg = str(mrs[0].get("iid", arg))
                else:
                    # Try all states if no open MR found
                    branch_result2 = _glab_api(
                        f"projects/:id/merge_requests?source_branch={arg}&per_page=1"
                    )
                    if branch_result2.returncode == 0:
                        mrs2 = json.loads(branch_result2.stdout)
                        if isinstance(mrs2, list) and mrs2:
                            arg = str(mrs2[0].get("iid", arg))
                        else:
                            print(f"ERROR: no MR found for branch {arg!r}")
                            return 1
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"ERROR: branch lookup failed: {e}")
            return 1

    try:
        result = subprocess.run(
            ["glab", "mr", "view", arg, "--output", "json"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        print("ERROR: glab not found — install from https://gitlab.com/gitlab-org/cli")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: glab timed out")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "MR", arg))
        return 1

    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: invalid JSON from glab\n{result.stdout[:500]}")
        return 1

    title = d.get("title", "?")
    state = d.get("state", "?")
    iid = d.get("iid", arg)
    source = d.get("source_branch", "?")
    target = d.get("target_branch", "?")
    author = (d.get("author") or {}).get("username", "?")
    web_url = d.get("web_url", "")
    labels = ", ".join(d.get("labels", [])) or "none"
    milestone = (d.get("milestone") or {}).get("title", "none")
    has_conflicts = d.get("has_conflicts", False)
    merge_status = d.get("merge_status", "?")
    merge_commit = d.get("merge_commit_sha") or d.get("squash_commit_sha") or ""
    draft = d.get("draft", False) or d.get("work_in_progress", False)

    # Pipeline
    pipeline = d.get("pipeline") or d.get("head_pipeline") or {}
    pipe_status = pipeline.get("status", "none")
    pipe_id = pipeline.get("id", "")

    # Diff stats
    changes = d.get("changes_count") or 0
    diff_stats = d.get("diff_stats") or {}
    additions = diff_stats.get("additions", 0)
    deletions = diff_stats.get("deletions", 0)

    # Reviewers
    reviewers = d.get("reviewers") or []
    reviewer_names = [r.get("username", "?") for r in reviewers]

    # Header
    draft_marker = " [DRAFT]" if draft else ""
    print(f"# !{iid} {title}{draft_marker}")
    print(f"State: {state} | Author: {author}")
    print(f"Branch: {source} -> {target}")
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")

    # Reviewers + approvals
    if reviewer_names:
        print(f"Reviewers: {', '.join(reviewer_names)}")

    # Fetch approvals via API (glab mr view doesn't include this)
    try:
        approvals_result = _glab_api(f"projects/:id/merge_requests/{iid}/approvals")
        if approvals_result.returncode == 0:
            approvals = json.loads(approvals_result.stdout)
            approved_by = approvals.get("approved_by", [])
            if approved_by:
                approver_names = [
                    (a.get("user") or {}).get("username", "?")
                    for a in approved_by
                ]
                print(f"Approved by: {', '.join(approver_names)}")
            else:
                print("Approved by: none")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # Pipeline + changes
    pipe_str = pipe_status
    if pipe_id:
        pipe_str += f" (#{pipe_id})"
    print(f"Pipeline: {pipe_str}")

    # Failed jobs (only when pipeline failed)
    if pipe_status == "failed" and pipe_id:
        try:
            jobs_result = _glab_api(
                f"projects/:id/pipelines/{pipe_id}/jobs?per_page=100&scope=failed"
            )
            if jobs_result.returncode == 0:
                jobs = json.loads(jobs_result.stdout)
                if isinstance(jobs, list) and jobs:
                    print(f"Failed jobs ({len(jobs)}):")
                    for job in jobs:
                        jid = job.get("id", "?")
                        jname = job.get("name", "?")
                        jstage = job.get("stage", "?")
                        print(f"  #{jid} | {jname} | {jstage}")
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

    print(f"Changes: {changes} files, +{additions} -{deletions}")

    # Conflicts
    if has_conflicts:
        print("Conflicts: YES — cannot merge")
    else:
        print(f"Merge status: {merge_status}")

    # Merge commit (if merged)
    if merge_commit:
        print(f"Merge commit: {merge_commit[:12]}")

    if web_url:
        print(f"URL: {web_url}")

    # Linked issue — extract from description or closing_issues
    description_raw = d.get("description") or ""
    issue_match = re.search(r'#(\d{4,})', description_raw)
    if issue_match:
        issue_iid = issue_match.group(1)
        try:
            issue_result = _glab_api(f"projects/:id/issues/{issue_iid}")
            if issue_result.returncode == 0:
                issue_data = json.loads(issue_result.stdout)
                issue_title = issue_data.get("title", "?")
                issue_state = issue_data.get("state", "?")
                issue_labels = ", ".join(issue_data.get("labels", [])) or "none"
                issue_assignees = ", ".join(
                    a.get("username", "?") for a in issue_data.get("assignees", [])
                ) or "none"
                print(f"\n## Issue #{issue_iid} — {issue_title}")
                print(f"State: {issue_state} | Labels: {issue_labels} | Assignees: {issue_assignees}")
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            print(f"\nIssue: #{issue_iid}")

    # Description
    description = description_raw[:DESCRIPTION_MAX]
    if description:
        print(f"\n## Description\n{description}")

    # Human comments (notes)
    try:
        notes_result = _glab_api(
            f"projects/:id/merge_requests/{iid}/notes?per_page=50&sort=asc"
        )
        if notes_result.returncode == 0:
            notes = json.loads(notes_result.stdout)
            if isinstance(notes, list):
                human_notes = [n for n in notes if not n.get("system", False)]
                if human_notes:
                    print(f"\n## Comments ({len(human_notes)})")
                    for note in human_notes[-10:]:
                        note_author = (note.get("author") or {}).get("username", "?")
                        body = (note.get("body") or "")[:COMMENT_MAX]
                        created = (note.get("created_at") or "")[:10]
                        print(f"\n**{note_author}** ({created}):")
                        print(body)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
