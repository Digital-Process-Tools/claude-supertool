#!/usr/bin/env python3
"""GitHub Actions job log via gh CLI.

Shows job metadata + smart log output:
1. Searches for error patterns and shows context around them
2. Falls back to last N lines if no patterns found

Config via SUPERTOOL_ env vars (set from .supertool.json):
  SUPERTOOL_LINES           — tail lines (default 80)
  SUPERTOOL_ERROR_PATTERNS  — comma-separated patterns to search
  SUPERTOOL_ERROR_CONTEXT   — lines of context around each error match (default 5)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys


def _local_branch_check(source: str) -> str:
    """Return a one-line local-branch-vs-source check for output.

    Empty string when not in a git repo, detached HEAD, or source is empty.
    """
    if not source:
        return ""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return ""
        local = r.stdout.strip()
        if not local or local == "HEAD":
            return ""
        if local == source:
            return f"You are on: {local} ✓"
        return f"You are on: {local} ⚠ MISMATCH — switch with: ./supertool 'git-checkout:{source}'"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify gh errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "could not resolve" in s or "404" in s or "not found" in s:
        return f"ERROR: {resource} #{identifier} not found. Check the ID. Use gh-run to list jobs first, then gh-job with the job ID."
    if "auth" in s or "login" in s or "token" in s:
        return f"ERROR: gh CLI not authenticated. Run: gh auth login"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes and retry."
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check repo access (gh auth status)."
    return f"ERROR: gh failed for {resource} #{identifier}: {stderr.strip()}"


def _get_config() -> dict:
    """Read config from SUPERTOOL_ env vars."""
    return {
        "lines": int(os.environ.get("SUPERTOOL_LINES", "80")),
        "error_patterns": os.environ.get(
            "SUPERTOOL_ERROR_PATTERNS", "ERROR,FAILED,Error:,Failed,fatal:,##[error]"
        ).split(","),
        "error_context": int(os.environ.get("SUPERTOOL_ERROR_CONTEXT", "5")),
    }


def _find_error_sections(lines: list[str], patterns: list[str], context: int) -> list[tuple[int, str]]:
    """Find lines matching error patterns and return them with context."""
    matches: set[int] = set()
    for i, line in enumerate(lines):
        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern:
                continue
            if pattern in line:
                for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                    matches.add(j)
                break

    if not matches:
        return []

    result: list[tuple[int, str]] = []
    sorted_matches = sorted(matches)
    prev = -2
    for idx in sorted_matches:
        if idx > prev + 1:
            result.append((-1, "..."))
        result.append((idx + 1, lines[idx]))
        prev = idx

    return result


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: job.py JOB_ID [raw [START [END]]]")
        return 1

    job_id = sys.argv[1]
    raw_mode = len(sys.argv) > 2 and sys.argv[2] == "raw"
    raw_start: int | None = None
    raw_end: int | None = None
    if raw_mode:
        try:
            if len(sys.argv) > 3 and sys.argv[3]:
                raw_start = max(1, int(sys.argv[3]))
            if len(sys.argv) > 4 and sys.argv[4]:
                raw_end = int(sys.argv[4])
        except ValueError:
            print("ERROR: raw START/END must be integers")
            return 1
    config = _get_config()
    tail_lines = config["lines"]

    # 1. Get job metadata — gh doesn't have a direct "get job by ID" command,
    # but we can get the log which includes the run context
    # First, try to get run info from the job
    job_name = "?"
    job_status = "?"
    job_conclusion = "?"
    run_id = ""
    pr_title = ""
    pr_number = ""
    pr_branch = ""
    pr_author = ""

    try:
        # gh api to get job details
        meta_result = subprocess.run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}"],
            capture_output=True, text=True, timeout=10,
        )
        if meta_result.returncode == 0:
            meta = json.loads(meta_result.stdout)
            job_name = meta.get("name", "?")
            job_status = meta.get("status", "?")
            job_conclusion = meta.get("conclusion") or "in_progress"
            run_id = str(meta.get("run_id", ""))
            run_url = meta.get("run_url", "")

            # Get the run to find the PR
            if run_id:
                run_result = subprocess.run(
                    ["gh", "run", "view", run_id, "--json",
                     "headBranch,event,pullRequests"],
                    capture_output=True, text=True, timeout=5,
                )
                if run_result.returncode == 0:
                    run_data = json.loads(run_result.stdout)
                    pr_branch = run_data.get("headBranch", "")
                    prs = run_data.get("pullRequests", [])
                    if prs:
                        pr_number = str(prs[0].get("number", ""))
                        # Get PR details
                        if pr_number:
                            pr_result = subprocess.run(
                                ["gh", "pr", "view", pr_number, "--json",
                                 "title,author,headRefName,baseRefName,labels"],
                                capture_output=True, text=True, timeout=5,
                            )
                            if pr_result.returncode == 0:
                                pr_data = json.loads(pr_result.stdout)
                                pr_title = pr_data.get("title", "")
                                pr_author = (pr_data.get("author") or {}).get("login", "")
                                pr_branch = pr_data.get("headRefName", pr_branch)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # 2. Get job log
    try:
        log_result = subprocess.run(
            ["gh", "api",
             f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}/logs"],
            capture_output=True, text=True, timeout=20,
        )
    except FileNotFoundError:
        print("ERROR: gh not found — install from https://cli.github.com")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: gh timed out (log)")
        return 1

    if log_result.returncode != 0:
        print(_format_error(log_result.stderr, "Job log", job_id))
        return 1

    # Clean timestamps and ANSI codes from log
    log = log_result.stdout
    log = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', log)
    # GitHub Actions prepends timestamps like "2024-01-15T10:30:00.1234567Z "
    log = re.sub(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ', '', log, flags=re.MULTILINE)

    lines = log.splitlines()
    total = len(lines)

    # Header
    display_status = job_conclusion if job_conclusion != "in_progress" else job_status
    print(f"# Job #{job_id} — {job_name}")
    print(f"Status: {display_status}")

    if pr_number:
        print(f"\n## PR #{pr_number} — {pr_title}")
        if pr_author:
            print(f"Author: {pr_author}")
        if pr_branch:
            print(f"Branch: {pr_branch}")
            local_check = _local_branch_check(pr_branch)
            if local_check:
                print(local_check)

    if run_id:
        print(f"Run: #{run_id}")

    print(f"Log: {total} lines total")

    # 3. Raw mode — dump (sliced) trace, skip filters
    if raw_mode:
        start = raw_start if raw_start is not None else 1
        end = raw_end if raw_end is not None else total
        end = min(end, total)
        if start > total:
            print(f"\n## Raw — start ({start}) > total ({total}); nothing to show")
            return 0
        shown = lines[start - 1:end]
        print(f"\n## Raw lines {start}-{start + len(shown) - 1} of {total}")
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
        return 0

    # 4. Error pattern search
    error_sections = _find_error_sections(
        lines, config["error_patterns"], config["error_context"]
    )

    if error_sections and display_status == "failure":
        print(f"\n## Error context ({len([e for e in error_sections if e[0] > 0])} lines matched)")
        for line_num, text in error_sections:
            if line_num == -1:
                print(text)
            else:
                print(f"  {line_num:>5} | {text}")

        print(f"\n## Tail (last {tail_lines} lines)")
        shown = lines[-tail_lines:] if len(lines) > tail_lines else lines
        start = total - len(shown) + 1
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
    else:
        shown = lines[-tail_lines:] if len(lines) > tail_lines else lines
        skipped = total - len(shown)
        if skipped > 0:
            print(f"({skipped} lines skipped)")
        print()
        start = total - len(shown) + 1
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
