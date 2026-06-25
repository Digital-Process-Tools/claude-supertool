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
from __future__ import annotations

import json
import os
import re
import subprocess
import sys


def _local_branch_check(source: str) -> str:
    """Return a one-line local-branch-vs-source check for output.

    Empty string when not in a git repo, detached HEAD, or source is empty.
    Used after the 'Branch:' line to flag editing on the wrong branch.
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
    """Classify glab errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "404" in s or "not found" in s or "could not resolve" in s:
        return f"ERROR: {resource} #{identifier} not found. Check the ID. Use gl-pipeline to list jobs first, then gl-job with the job ID."
    if "401" in s or "unauthorized" in s or "glpat_" in s or "authenticate" in s or "bad token" in s or "token expired" in s:
        return "ERROR: glab not authenticated. Run: glab auth login"
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."
    return f"ERROR: glab failed for {resource} #{identifier}: {stderr.strip()}"


def _get_config() -> dict:
    """Read config from SUPERTOOL_ env vars."""
    return {
        "lines": int(os.environ.get("SUPERTOOL_LINES", "80")),
        "error_patterns": os.environ.get(
            "SUPERTOOL_ERROR_PATTERNS",
            # ERROR/FAIL: generic. 🪪: phpstan identifier marker (every phpstan error).
            # notSubtype/argument.type/return.type: phpstan identifiers as text fallback.
            "ERROR,FAILURES!,Fatal,Failed asserting,🪪,notSubtype,argument.type,return.type"
        ).split(","),
        "error_context": int(os.environ.get("SUPERTOOL_ERROR_CONTEXT", "8")),
        "job_patterns": _parse_job_patterns(os.environ.get("SUPERTOOL_JOB_PATTERNS", "")),
    }


def _parse_job_patterns(raw: str) -> list[dict]:
    """Parse the optional per-job-name pattern table (JSON list).

    Each entry: {"job": <name-regex>, "patterns": [<str>...], "resolution": <op>?}.
    Returns [] on empty or malformed config — the flat error_patterns still apply.
    """
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def _select_job_patterns(
    job_name: str, job_patterns: list[dict], default_patterns: list[str]
) -> tuple[list[str], str | None]:
    """Pick patterns + resolution op for this job by name.

    First entry whose "job" regex matches job_name wins. No match → the flat
    default_patterns with no resolution (backward compatible).
    """
    for entry in job_patterns:
        name_re = entry.get("job", "")
        if not name_re:
            continue
        try:
            matched = re.search(name_re, job_name) is not None
        except re.error:
            matched = name_re in job_name
        if matched:
            patterns = entry.get("patterns") or default_patterns
            return patterns, entry.get("resolution")
    return default_patterns, None


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
        print("ERROR: usage: job.py JOB_ID [raw [START [END]]]")
        return 1

    job_id = sys.argv[1]
    raw_mode = len(sys.argv) > 2 and sys.argv[2] == "raw"
    errors_mode = len(sys.argv) > 2 and sys.argv[2] in ("errors", "fail")
    grep_mode = len(sys.argv) > 2 and sys.argv[2] == "grep"
    grep_pattern = sys.argv[3] if grep_mode and len(sys.argv) > 3 else None
    if grep_mode and not grep_pattern:
        print("ERROR: usage: gl-job:JOB_ID:grep:PATTERN")
        return 1
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
            local_check = _local_branch_check(mr_branch)
            if local_check:
                print(local_check)
            if mr_labels:
                print(f"Labels: {mr_labels}")
            print(f"Changes: {mr_changes} files, +{mr_additions} -{mr_deletions}")
            if issue_ref:
                print(f"Issue: {issue_ref}")
            print(f"Pipeline: #{pipeline_id}")
        else:
            print(f"Branch: {ref} | Pipeline: #{pipeline_id}")
            local_check = _local_branch_check(ref)
            if local_check:
                print(local_check)

    if web_url:
        print(f"URL: {web_url}")
    print(f"Log: {total} lines total")

    # 3. Raw mode — dump (sliced) trace, skip filters
    if raw_mode:
        start = raw_start if raw_start is not None else 1
        end = raw_end if raw_end is not None else total
        end = min(end, total)
        if start > total:
            print(f"\n## Raw — start ({start}) > total ({total}); nothing to show")
            return 0
        # Cap raw dumps that exceed GL_JOB_RAW_MAX_LINES, regardless of whether
        # the user passed an explicit START:END. A user can still defeat the
        # cap by raising the env var, but a 99999-line slice no longer
        # silently dumps 10MB into validator output.
        cap = int(os.environ.get("GL_JOB_RAW_MAX_LINES", "5000"))
        shown = lines[start - 1:end]
        if len(shown) > cap:
            kept = shown[:cap]
            hint = (
                "narrow the slice or raise GL_JOB_RAW_MAX_LINES=N"
                if raw_end is not None
                else "pass START:END to slice further, or set GL_JOB_RAW_MAX_LINES=N"
            )
            print(
                f"\n## Raw lines {start}-{start + cap - 1} of {total} "
                f"[CAPPED at {cap} — {hint}]"
            )
            for i, line in enumerate(kept):
                print(f"  {start + i:>5} | {line}")
            return 0
        print(f"\n## Raw lines {start}-{start + len(shown) - 1} of {total}")
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
        return 0

    # 3b. Grep mode — ad-hoc regex over the trace, context + gap markers.
    # Honest primitive: caller's pattern, no config. Regex with literal
    # fallback on re.error (mirrors supertool's grep). Never silent-empty.
    if grep_mode and grep_pattern is not None:
        try:
            rx = re.compile(grep_pattern)
            shown_pattern = grep_pattern
        except re.error:
            rx = re.compile(re.escape(grep_pattern))
            shown_pattern = f"{grep_pattern} (literal match)"
        ctx = config["error_context"]
        hits: set[int] = set()
        for i, line in enumerate(lines):
            if rx.search(line):
                for j in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
                    hits.add(j)
        if not hits:
            print(f"\n## No lines match /{shown_pattern}/ (searched {total} lines)")
            tail = lines[-tail_lines:] if len(lines) > tail_lines else lines
            print(f"Showing last {len(tail)} lines as fallback:")
            start = total - len(tail) + 1
            for i, line in enumerate(tail):
                print(f"  {start + i:>5} | {line}")
            return 0
        match_count = sum(1 for line in lines if rx.search(line))
        print(f"\n## grep /{shown_pattern}/ — {match_count} matching lines (±{ctx} context)")
        prev = -2
        for idx in sorted(hits):
            if idx > prev + 1:
                print("...")
            print(f"  {idx + 1:>5} | {lines[idx]}")
            prev = idx
        return 0

    # 4. Try error pattern search first. Per-job-name table (if configured)
    # picks tighter patterns + a resolution op; else the flat default applies.
    patterns, resolution = _select_job_patterns(
        job_name, config["job_patterns"], config["error_patterns"]
    )
    resolution_line = (
        f"Resolve:  ./supertool '{resolution.replace('{id}', job_id)}'"
        if resolution else ""
    )
    error_sections = _find_error_sections(lines, patterns, config["error_context"])

    # errors mode — dump ALL matched blocks, no tail cap
    if errors_mode:
        if not error_sections:
            print("\n## No error patterns matched")
            return 0
        matched_count = len([e for e in error_sections if e[0] > 0])
        print(f"\n## All error blocks ({matched_count} lines matched, no tail truncation)")
        for line_num, text in error_sections:
            if line_num == -1:
                print(text)
            else:
                print(f"  {line_num:>5} | {text}")
        if resolution_line:
            print(f"\n{resolution_line}")
        return 0

    if error_sections and job_status == "failed":
        print(f"\n## Error context ({len([e for e in error_sections if e[0] > 0])} lines matched)")
        for line_num, text in error_sections:
            if line_num == -1:
                print(text)  # gap marker
            else:
                print(f"  {line_num:>5} | {text}")

        if resolution_line:
            print(f"\n{resolution_line}")

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
