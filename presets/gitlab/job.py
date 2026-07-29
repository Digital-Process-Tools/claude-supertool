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


# Lines that state a *reason* for the failure. A stack trace is the consequence;
# the line saying why sits above it (#444), and some failures never produce a
# trace at all (#445). These are matched in addition to — and independently of —
# the configured error_patterns, so a tighter per-job pattern table can narrow
# the surrounding noise without being able to hide the cause.
# Set GL_JOB_CAUSE_MARKERS=0 to disable.
_CAUSE_MARKERS = [
    re.compile(r"^\s*Caused by\b"),
    re.compile(r"[\w\\]+(?:Exception|Error):\s"),
    re.compile(r"SQLSTATE\["),
    re.compile(r"^\s*In \S+\.php line \d+:"),
    re.compile(r"Exit Code:\s*\d+"),
    re.compile(r"Fatal error:"),
    re.compile(r"Segmentation (?:fault|violation)"),
    re.compile(r"Allowed memory size of \d+ bytes exhausted"),
    re.compile(r"\w*CrashedException"),
]

_SECTION_START = re.compile(r"^section_start:\d+:(\S+)")


def _cause_lines(lines: list[str]) -> list[int]:
    """Indices of lines that state a reason for the failure."""
    if os.environ.get("GL_JOB_CAUSE_MARKERS", "1") == "0":
        return []
    return [
        i for i, line in enumerate(lines)
        if any(rx.search(line) for rx in _CAUSE_MARKERS)
    ]


def _last_section(lines: list[str]) -> str | None:
    """Name of the last CI section the runner entered — i.e. the failing step."""
    for line in reversed(lines):
        match = _SECTION_START.match(line)
        if match:
            return match.group(1)
    return None


def _print_unmatched_failure(
    job_id: str, job_status: str, patterns: list[str], lines: list[str], total: int
) -> None:
    """Report a failed job the patterns could not classify — never as silence.

    `## No error patterns matched` on a job GitLab calls *failed* reads as green
    (#445), which is the worst output a failure tool can produce. A failed job
    always has a reason; not finding it is a gap in this tool, so the output
    says exactly that and hands back the raw evidence.
    """
    tail_n = int(os.environ.get("GL_JOB_UNMATCHED_TAIL_LINES", "40"))
    print("\n## FAILED — no error pattern matched")
    print(
        f"Job status is `{job_status}`: something did go wrong. supertool "
        "could not classify it, which means a pattern is missing here — "
        "not that the log is clean. Read the tail below before concluding "
        "anything."
    )
    shown = ", ".join(p.strip() for p in patterns if p.strip())
    if shown:
        print(f"Patterns tried: {shown} (+ built-in cause markers)")
    section = _last_section(lines)
    if section:
        print(f"Last step entered: {section}")
    tail = lines[-tail_n:] if len(lines) > tail_n else lines
    print(f"\n## Log tail (last {len(tail)} lines of {total})")
    start = total - len(tail) + 1
    for i, line in enumerate(tail):
        print(f"  {start + i:>5} | {line}")
    print(
        f"\nNext:  ./supertool 'gl-job:{job_id}:raw'  or  "
        f"'gl-job:{job_id}:grep:PATTERN'  — the whole trace is still there."
    )


_PHPUNIT_BLOCK_START = re.compile(r'^\s*\d+\)\s+\S+::\S+')
_PHPUNIT_BLOCK_SUMMARY = re.compile(
    r'^\s*(FAILURES!|ERRORS!|WARNINGS!|OK \(|OK, but|There (was|were) \d+)'
)


def _phpunit_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """Locate PHPUnit failure blocks as (start, end) inclusive 0-based indices.

    A block runs from its `N) Class::method` header to the last non-blank line
    before the next header or the run summary — typically the trailing
    `/path/File.php:LINE` frames.
    """
    blocks: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if not _PHPUNIT_BLOCK_START.match(line):
            continue
        j = i + 1
        while (
            j < len(lines)
            and not _PHPUNIT_BLOCK_START.match(lines[j])
            and not _PHPUNIT_BLOCK_SUMMARY.match(lines[j])
        ):
            j += 1
        end = j - 1
        while end > i and not lines[end].strip():
            end -= 1
        blocks.append((i, end))
    return blocks


def _expand_phpunit_blocks(
    lines: list[str], matches: set[int], block_max: int, total_max: int
) -> tuple[int, int]:
    """Widen any touched PHPUnit failure to its whole block, in place.

    The assertion diff / rendered artifact sits in the middle of the block, so
    a pattern window centred on `Failed asserting` drops exactly the evidence.
    Blocks longer than block_max keep their head and tail; the elision is then
    reported by the gap marker.

    total_max budgets expansion across all blocks. Past it whole failures are
    dropped rather than gutted — a dropped block keeps whatever its pattern
    windows already selected, so no input returns less than it did before.
    Returns (dropped, touched) for the caller to announce.
    """
    touched = [
        (start, end)
        for start, end in _phpunit_blocks(lines)
        if any(idx in matches for idx in range(start, end + 1))
    ]
    budget = total_max
    dropped = 0
    for start, end in touched:
        size = end - start + 1
        cost = min(size, block_max)
        if cost > budget:
            dropped += 1
            continue
        budget -= cost
        if size <= block_max:
            matches.update(range(start, end + 1))
            continue
        head = block_max // 2
        matches.update(range(start, start + head))
        matches.update(range(end - (block_max - head) + 1, end + 1))
    return dropped, len(touched)


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

    # Cause markers anchor on the line that states *why*, not on the wreckage it
    # produced. They run whatever the configured patterns are, and get their
    # context asymmetrically: a cause is followed by its message body (the
    # indented exception text, the `Exit Code:` line), so the window leans down.
    cause_before = int(os.environ.get("GL_JOB_CAUSE_CONTEXT_BEFORE", "2"))
    for i in _cause_lines(lines):
        for j in range(max(0, i - cause_before), min(len(lines), i + context + 1)):
            matches.add(j)

    if not matches:
        return []

    dropped, touched = _expand_phpunit_blocks(
        lines,
        matches,
        int(os.environ.get("GL_JOB_PHPUNIT_BLOCK_MAX_LINES", "500")),
        int(os.environ.get("GL_JOB_PHPUNIT_TOTAL_MAX_LINES", "2000")),
    )

    result: list[tuple[int, str]] = []
    sorted_matches = sorted(matches)
    prev = -1
    for idx in sorted_matches:
        gap = idx - prev - 1
        if gap > 0:
            plural = "" if gap == 1 else "s"
            result.append((-1, f"... ({gap} line{plural} elided)"))
        result.append((idx + 1, lines[idx]))  # 1-indexed line numbers
        prev = idx

    if dropped:
        plural = "" if dropped == 1 else "s"
        result.append((
            -1,
            f"... ({dropped} of {touched} PHPUnit failure{plural} not shown in full — "
            f"raise GL_JOB_PHPUNIT_TOTAL_MAX_LINES=N)",
        ))

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
    raw_tail: int | None = None
    if raw_mode:
        try:
            if len(sys.argv) > 3 and sys.argv[3]:
                first = int(sys.argv[3])
                # A negative START is the tail form: `raw:-40` is the last 40
                # lines. `raw` is reached as a fallback when `:fail` was
                # unhelpful, and what a caller wants at that point is almost
                # always the end of the log — which previously could not be
                # asked for without first spending a call to learn the total.
                if first < 0:
                    raw_tail = -first
                else:
                    raw_start = max(1, first)
            if len(sys.argv) > 4 and sys.argv[4]:
                raw_end = int(sys.argv[4])
        except ValueError:
            print("ERROR: raw START/END must be integers")
            return 1
        if raw_start is not None and raw_end is not None and raw_end < raw_start:
            # An inverted range used to slice to nothing under a header reading
            # "Raw lines 10-9 of 20" — an empty body that reads as an empty
            # stretch of log rather than as a range the op could not serve.
            print(f"ERROR: raw END ({raw_end}) is before START ({raw_start}); "
                  f"ranges are 1-indexed and inclusive")
            return 1
        if raw_tail is not None and raw_end is not None:
            print("ERROR: the raw tail form takes no END — "
                  f"use raw:-{raw_tail} for the last {raw_tail} lines, "
                  "or raw:START:END for an absolute range")
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
        if total == 0:
            # Distinct from an out-of-range request: this is an absence in the
            # world, not one the op produced. Saying "nothing to show" for both
            # is what cost the round-trip in #487.
            print("\n## Raw — the log is empty (0 lines)")
            return 0
        if raw_tail is not None:
            width = min(raw_tail, total)
            start, end = total - width + 1, total
        else:
            start = raw_start if raw_start is not None else 1
            end = raw_end if raw_end is not None else total
            if start > total:
                # The bound is already printed one line above, so declining
                # here only buys the caller a second call to re-read it. Return
                # the tail of the width that was asked for — and say plainly
                # that these are not the lines requested, because a clamp
                # nobody is told about hands back different data than was
                # asked for, which is the same disease one level down.
                width = (end - start + 1) if raw_end is not None else tail_lines
                width = max(1, min(width, total))
                print(f"\n## Raw — requested {start}-{end} is past end of log "
                      f"({total} lines); showing the last {width} lines instead")
                start, end = total - width + 1, total
            end = min(end, total)
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
            if job_status == "failed":
                _print_unmatched_failure(job_id, job_status, patterns, lines, total)
            else:
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
    elif job_status == "failed":
        # Nothing matched on a job that failed — say so, do not just print a tail
        # and let the reader infer the log was clean (#445).
        _print_unmatched_failure(job_id, job_status, patterns, lines, total)
    else:
        # Job didn't fail — just show tail
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
