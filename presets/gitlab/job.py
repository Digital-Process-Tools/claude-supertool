#!/usr/bin/env python3
"""GitLab CI job log via glab CLI.

Shows job metadata + smart log output:
1. Searches for error patterns and shows context around them
2. Falls back to last N lines if no patterns found

Config via SUPERTOOL_ env vars (set from .supertool.json):
  SUPERTOOL_LINES           — tail lines (default 80)
  SUPERTOOL_ERROR_PATTERNS  — comma-separated patterns to search (default: ERROR,FAIL,Fatal,------)
  SUPERTOOL_ERROR_CONTEXT   — lines of context around each error match (default 5)
"""
import json
import os
import re
import subprocess
import sys


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify glab errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "404" in s or "not found" in s or "could not resolve" in s:
        return f"ERROR: {resource} #{identifier} not found. Check the ID. Use gl-pipeline to list jobs first, then gl-job with the job ID."
    if "401" in s or "unauthorized" in s or "token" in s:
        return "ERROR: glab not authenticated. Run: glab auth login"
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."
    return f"ERROR: glab failed for {resource} #{identifier}: {stderr.strip()}"


def _get_config() -> dict:
    """Read config from SUPERTOOL_ env vars."""
    return {
        "lines": int(os.environ.get("SUPERTOOL_LINES", "80")),
        "error_patterns": os.environ.get(
            "SUPERTOOL_ERROR_PATTERNS", "ERROR,FAILURES!,Fatal,Failed asserting"
        ).split(","),
        "error_context": int(os.environ.get("SUPERTOOL_ERROR_CONTEXT", "5")),
    }


def _find_error_sections(lines: list[str], patterns: list[str], context: int) -> list[tuple[int, str]]:
    """Find lines matching error patterns and return them with context.

    Returns list of (line_number, line_text) tuples, deduplicated and sorted.
    """
    matches: set[int] = set()
    for i, line in enumerate(lines):
        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern:
                continue
            if pattern in line:
                # Add the match and surrounding context
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
            result.append((-1, "..."))  # gap marker
        result.append((idx + 1, lines[idx]))  # 1-indexed line numbers
        prev = idx

    return result


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: job.py JOB_ID")
        return 1

    job_id = sys.argv[1]
    config = _get_config()
    tail_lines = config["lines"]

    # 1. Get job metadata
    try:
        meta_result = subprocess.run(
            ["glab", "api", f"projects/:id/jobs/{job_id}"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        print("ERROR: glab not found — install from https://gitlab.com/gitlab-org/cli")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: glab timed out (metadata)")
        return 1

    job_name = "?"
    job_status = "?"
    job_stage = "?"
    job_duration = None
    web_url = ""
    ref = ""
    pipeline_id = ""
    if meta_result.returncode == 0:
        try:
            meta = json.loads(meta_result.stdout)
            job_name = meta.get("name", "?")
            job_status = meta.get("status", "?")
            job_stage = meta.get("stage", "?")
            job_duration = meta.get("duration")
            web_url = meta.get("web_url", "")
            ref = meta.get("ref", "")
            pipeline_id = str((meta.get("pipeline") or {}).get("id", ""))
        except json.JSONDecodeError:
            pass

    # 2. Get job trace (log)
    try:
        result = subprocess.run(
            ["glab", "api", f"projects/:id/jobs/{job_id}/trace"],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        print("ERROR: glab timed out (trace)")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "Job log", job_id))
        return 1

    # Clean ANSI escape codes
    log = result.stdout
    log = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', log)
    log = re.sub(r'\x1b\]8;[^;]*;[^\x1b]*\x1b\\', '', log)

    lines = log.splitlines()
    total = len(lines)

    # Header
    duration_str = f"{job_duration:.0f}s" if job_duration else "?"
    print(f"# Job #{job_id} — {job_name}")
    print(f"Stage: {job_stage} | Status: {job_status} | Duration: {duration_str}")

    # Parse ref to show branch or MR (with details)
    if ref:
        mr_match = re.match(r'refs/merge-requests/(\d+)/head', ref)
        if mr_match:
            mr_iid = mr_match.group(1)
            mr_data = {}
            try:
                mr_result = subprocess.run(
                    ["glab", "api", f"projects/:id/merge_requests/{mr_iid}"],
                    capture_output=True, text=True, timeout=5,
                )
                if mr_result.returncode == 0:
                    mr_data = json.loads(mr_result.stdout)
            except (subprocess.TimeoutExpired, json.JSONDecodeError):
                pass

            mr_title = mr_data.get("title", "")
            mr_branch = mr_data.get("source_branch", "")
            mr_target = mr_data.get("target_branch", "")
            mr_author = (mr_data.get("author") or {}).get("username", "")
            mr_labels = ", ".join(mr_data.get("labels", [])) or ""
            mr_state = mr_data.get("state", "")
            diff_stats = mr_data.get("diff_stats") or {}
            mr_changes = mr_data.get("changes_count", "?")
            mr_additions = diff_stats.get("additions", "?")
            mr_deletions = diff_stats.get("deletions", "?")

            # Extract related issue from description (#NUMBER pattern)
            mr_desc = mr_data.get("description") or ""
            issue_match = re.search(r'#(\d{4,})', mr_desc)
            issue_ref = f"#{issue_match.group(1)}" if issue_match else ""

            print(f"\n## MR !{mr_iid} — {mr_title}")
            print(f"State: {mr_state} | Author: {mr_author}")
            print(f"Branch: {mr_branch} -> {mr_target}")
            if mr_labels:
                print(f"Labels: {mr_labels}")
            print(f"Changes: {mr_changes} files, +{mr_additions} -{mr_deletions}")
            if issue_ref:
                print(f"Issue: {issue_ref}")
            print(f"Pipeline: #{pipeline_id}")
        else:
            print(f"Branch: {ref} | Pipeline: #{pipeline_id}")

    if web_url:
        print(f"URL: {web_url}")
    print(f"Log: {total} lines total")

    # 3. Try error pattern search first
    error_sections = _find_error_sections(
        lines, config["error_patterns"], config["error_context"]
    )

    if error_sections and job_status == "failed":
        print(f"\n## Error context ({len([e for e in error_sections if e[0] > 0])} lines matched)")
        for line_num, text in error_sections:
            if line_num == -1:
                print(text)  # gap marker
            else:
                print(f"  {line_num:>5} | {text}")

        # Also show tail for full context
        print(f"\n## Tail (last {tail_lines} lines)")
        shown = lines[-tail_lines:] if len(lines) > tail_lines else lines
        start = total - len(shown) + 1
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
    else:
        # No error patterns found or job didn't fail — just show tail
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
