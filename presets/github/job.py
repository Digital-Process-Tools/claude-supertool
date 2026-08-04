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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)
from _env import env_int  # noqa: E402  (the one numeric-knob reader)


def _api_repo_path(suffix: str) -> str:
    """A `gh api` repo path — the target's, or gh's own cwd placeholders.

    `gh api repos/{owner}/{repo}/…` expands those two literal placeholders from
    the cwd's remote. That expansion is precisely what a repo target has to
    override, so the placeholders are replaced rather than accompanied — there
    is no `--repo` on `gh api` to add beside them (#673).
    """
    return _repo_target.api_path(suffix)


def _local_branch_check(source: str) -> str:
    """Return a one-line local-branch-vs-source check for output.

    Empty string when not in a git repo, detached HEAD, or source is empty.
    """
    if not source:
        return ""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3, encoding="utf-8", errors="replace",
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


def _gh_error_kind(stderr: str) -> str:
    """Bucket a gh failure. Probe order matches the original _format_error
    chain exactly, so the message any given stderr produces is unchanged."""
    s = stderr.lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return "repo"
    if "could not resolve" in s or "404" in s or "not found" in s:
        return "notfound"
    if "401" in s or "unauthorized" in s or "not logged in" in s or "token" in s:
        return "auth"
    if "rate limit" in s or "429" in s:
        return "ratelimit"
    if "403" in s or "forbidden" in s:
        return "forbidden"
    return "other"


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify gh errors into actionable messages for LLMs."""
    kind = _gh_error_kind(stderr)
    if kind == "repo":
        return _repo_target.no_repo_error("gh-job:12345:fail")
    if kind == "notfound":
        return f"ERROR: {resource} #{identifier} not found. Check the ID. Use gh-run to list jobs first, then gh-job with the job ID."
    if kind == "auth":
        return f"ERROR: gh CLI not authenticated. Run: gh auth login (verify with: gh auth status)"
    if kind == "ratelimit":
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes and retry."
    if kind == "forbidden":
        return f"ERROR: permission denied for {resource} #{identifier}. Check repo access (gh auth status)."
    return f"ERROR: gh failed for {resource} #{identifier}: {stderr.strip()}"


def _missing_log_message(
    job_id: str, meta: dict | None, meta_absent: bool, meta_error: str
) -> str:
    """Explain a 404 from the logs endpoint by the job's state, not the ID.

    GitHub writes a job's log **on completion**, so `404 BlobNotFound` from
    `actions/jobs/<id>/logs` has four causes that call for four different
    next actions, and the op used to render all four as "Check the ID" — the
    one thing that was demonstrably right in the incident that filed #723.
    Verified live on this repo: a queued job and an in_progress job both
    return `gh: HTTP 404` with a `<Code>BlobNotFound</Code>` body, while the
    *job* endpoint returns the full object for the same ID.

    That job endpoint is what separates them, and `main` already calls it
    before it fetches the log — so this costs **no extra request on any
    path**, including the happy one where the log exists and nobody cares
    about the job's status. Fetching metadata defensively *after* the failure
    would have been the cheap-looking option; there was nothing to buy.

    The cancelled row is the one that saves real time: it is the only state
    whose right response is to stop looking rather than to retry.

    When the job endpoint itself did not answer, there is nothing to decide
    from. That is the third state and it declines, rather than picking the
    likeliest of four (`docs/validators.md`, "Declining instead of
    guessing"). All four branches stay ERROR and exit 1 — a log that could
    not be read must never soften into an empty log or an ok.
    """
    if meta is None:
        if meta_absent:
            return (
                f"ERROR: Job #{job_id} not found — the job endpoint returned "
                f"404 for this ID too, so no such job exists in this repo. "
                f"Check the ID. Use gh-run to list jobs first, then gh-job "
                f"with the job ID."
            )
        state_path = _api_repo_path("actions/jobs/" + str(job_id))
        return (
            f"ERROR: Job #{job_id} has no log (HTTP 404), and supertool "
            f"could not tell why — the job endpoint did not answer: "
            f"{meta_error}. A wrong ID, a job still running, and a log that "
            f"was never written or has since expired are all still possible; "
            f"this op is not guessing between them. Read the job state "
            f"directly with: gh api {state_path}"
        )
    name = meta.get("name") or "?"
    status = meta.get("status") or "?"
    conclusion = meta.get("conclusion") or ""
    completed_at = meta.get("completed_at") or "?"
    label = f"Job #{job_id} ({name})"
    if status != "completed":
        return (
            f"ERROR: {label} has no log — its status is `{status}`, so the "
            f"log is not written yet. GitHub writes a job's log when the job "
            f"completes; the ID is correct and there is nothing to fix. "
            f"Retry once it finishes: ./supertool 'gh-job:{job_id}'"
        )
    if conclusion in ("cancelled", "skipped"):
        return (
            f"ERROR: {label} has no log — the job was `{conclusion}` "
            f"(completed_at {completed_at}). GitHub only writes a log for a "
            f"job that ran to completion, so no log was ever written for this "
            f"one and none ever will be. Stop waiting — the ID is correct. "
            f"Sibling jobs on the same run may still have logs."
        )
    return (
        f"ERROR: {label} completed `{conclusion}` at {completed_at}, but its "
        f"log is unavailable — expired or purged (GitHub keeps job logs for a "
        f"limited retention window). The ID is correct; the log is gone, not "
        f"missing from your query."
    )


def _get_config() -> dict:
    """Read config from SUPERTOOL_ env vars."""
    return {
        "lines": env_int("SUPERTOOL_LINES", 80, minimum=1),
        "error_patterns": os.environ.get(
            "SUPERTOOL_ERROR_PATTERNS", "ERROR,FAILED,Error:,Failed,fatal:,##[error]"
        ).split(","),
        "error_context": env_int("SUPERTOOL_ERROR_CONTEXT", 5, minimum=0),
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


def _print_unmatched_failure(
    job_id: str, status_label: str, patterns: list[str], lines: list[str], total: int
) -> None:
    """Report a failed job the patterns could not classify — never as silence.

    `## No error patterns matched` on a job GitHub calls *failure* reads as
    green (#453, mirrors #445/#452 on the GitLab side), which is the worst
    output a failure tool can produce. A failed job always has a reason; not
    finding it is a gap in this tool, so the output says exactly that and
    hands back the raw evidence.

    Unlike gl-job, this does not port a "last section entered" hint: GitHub
    Actions always runs its `if: always()` steps (the junit-summary step,
    "Post job cleanup") after a failure, so the last `##[group]` in the log is
    routinely a step that has nothing to do with the failure — naming it would
    mislead rather than help.
    """
    tail_n = env_int("GH_JOB_UNMATCHED_TAIL_LINES", 40, minimum=1)
    print("\n## FAILED — no error pattern matched")
    print(
        f"Job status is `{status_label}`: something did go wrong. supertool "
        "could not classify it, which means a pattern is missing here — "
        "not that the log is clean. Read the tail below before concluding "
        "anything."
    )
    shown = ", ".join(p.strip() for p in patterns if p.strip())
    if shown:
        print(f"Patterns tried: {shown}")
    tail = lines[-tail_n:] if len(lines) > tail_n else lines
    print(f"\n## Log tail (last {len(tail)} lines of {total})")
    start = total - len(tail) + 1
    for i, line in enumerate(tail):
        print(f"  {start + i:>5} | {line}")
    print(
        f"\nNext:  ./supertool 'gh-job:{job_id}:raw'  or  "
        f"'gh-job:{job_id}:grep:PATTERN'  — the whole trace is still there."
    )


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


def _emit_grep_hits(
    lines: list[str],
    hit_indexes: list[int],
    rx: "re.Pattern[str]",
    match_count: int,
    budget: int,
    knob: str,
    shown_pattern: str,
    ctx: int,
) -> None:
    """Print grep hits under a byte budget, and say so when the budget bit (#622).

    The op used to print every hit, unbounded. That reads as safe — nothing is
    dropped — but a `:grep:` over a CI trace where each match is a whole
    assertion failure with rendered HTML emits hundreds of KB into a consumer
    that cuts at a few tens, so the tail vanished with no marker anywhere. A
    pipeline triage read the surviving head as the whole list and judged the
    blast radius small; it was not. Unbounded-then-cut-downstream is the same
    silence as a limit that does not announce itself, one layer over.

    So the bound moves here, where the true total is already known, and the
    three states stay distinguishable:

      - everything fit: nothing extra is printed, and that silence is the
        positive claim that the list is whole;
      - the budget bit: the shortfall is stated in exact numbers, because
        `match_count` was computed over the whole trace before printing began.
        This is not the streaming case where only "there was more" is knowable
        — do not weaken it to one;
      - and the note names *size* as what cut, plus the knob that governs it.
        Saying "limit N" here would be a confidently wrong disclosure: this op
        has no match limit, and the cut fires far earlier than any count would
        suggest precisely because the lines are enormous. Wrong is worse than
        silent.

    The note is a single bounded line (#605) — one line per dropped match would
    re-spend the budget the bound just saved.
    """
    # Plan first, print second. The note has to go in the HEADER as well as
    # the footer, and the header is written before the body — so what fits
    # must be known before the first byte goes out. A footer-only disclosure
    # is read by nobody in exactly the case it exists for: the reader who is
    # being cut off is cut off before reaching it.
    planned: list[str] = []
    emitted = 0
    shown_matches = 0
    prev = -2
    cut = False
    for idx in hit_indexes:
        chunk = "...\n" if idx > prev + 1 else ""
        chunk += f"  {idx + 1:>5} | {lines[idx]}\n"
        size = len(chunk.encode("utf-8", "replace"))
        # The first hit always goes out whole, however fat: a bound that can
        # return zero matches on a pattern that matched is an absence the op
        # invented, which is the disease itself.
        if emitted and emitted + size > budget:
            cut = True
            break
        planned.append(chunk)
        emitted += size
        if rx.search(lines[idx]):
            shown_matches += 1
        prev = idx
    header = (f"\n## grep /{shown_pattern}/ — {match_count} matching lines "
              f"(±{ctx} context)")
    if cut:
        header += (f" [CAPPED: {shown_matches} shown, output limited to {budget} "
                   f"bytes by size — raise {knob}=N]")
    print(header)
    sys.stdout.write("".join(planned))
    if cut:
        print(
            f"... ({shown_matches} of {match_count} matching lines shown — output "
            f"capped at {budget} bytes by size, not by a match count limit; "
            f"raise {knob}=N or narrow the pattern)"
        )


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: job.py JOB_ID [raw [START [END]]]")
        return 1

    job_id = sys.argv[1]
    raw_mode = len(sys.argv) > 2 and sys.argv[2] == "raw"
    grep_mode = len(sys.argv) > 2 and sys.argv[2] == "grep"
    errors_mode = len(sys.argv) > 2 and sys.argv[2] in ("errors", "fail")
    grep_pattern = sys.argv[3] if grep_mode and len(sys.argv) > 3 else None
    if grep_mode and not grep_pattern:
        print("ERROR: usage: gh-job:JOB_ID:grep:PATTERN")
        return 1
    raw_start: int | None = None
    raw_end: int | None = None
    raw_tail: int | None = None
    if raw_mode:
        try:
            if len(sys.argv) > 3 and sys.argv[3]:
                first = int(sys.argv[3])
                # Tail form — see the gl-job twin of this block (#487).
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

    # 1. Get job metadata — gh doesn't have a direct "get job by ID" command,
    # but we can get the log which includes the run context
    # First, try to get run info from the job
    job_name = "?"
    job_status = "?"
    job_conclusion = "?"
    job_meta: dict | None = None
    meta_absent = False
    meta_error = ""
    run_id = ""
    pr_title = ""
    pr_number = ""
    pr_branch = ""
    pr_author = ""

    try:
        # gh api to get job details
        meta_result = subprocess.run(
            ["gh", "api", _api_repo_path(f"actions/jobs/{job_id}")],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
        if meta_result.returncode != 0:
            # A 404 *here* is the one case where "check the ID" is the truth:
            # there is no such job. Every other failure means the job's state
            # is unknowable, and #723 is precisely about not converting that
            # into a confident answer further down.
            if _gh_error_kind(meta_result.stderr) == "notfound":
                meta_absent = True
            else:
                meta_error = (meta_result.stderr.strip()
                              or f"gh exited {meta_result.returncode}")
        if meta_result.returncode == 0:
            meta = json.loads(meta_result.stdout)
            job_meta = meta
            job_name = meta.get("name", "?")
            job_status = meta.get("status", "?")
            job_conclusion = meta.get("conclusion") or "in_progress"
            run_id = str(meta.get("run_id", ""))
            run_url = meta.get("run_url", "")

            # Get the run to find the PR
            if run_id:
                run_result = subprocess.run(
                    ["gh", "run", "view", run_id, *_repo_target.gh_args(), "--json",
                     "headBranch,event,pullRequests"],
                    capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
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
                                ["gh", "pr", "view", pr_number, *_repo_target.gh_args(), "--json",
                                 "title,author,headRefName,baseRefName,labels"],
                                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
                            )
                            if pr_result.returncode == 0:
                                pr_data = json.loads(pr_result.stdout)
                                pr_title = pr_data.get("title", "")
                                pr_author = (pr_data.get("author") or {}).get("login", "")
                                pr_branch = pr_data.get("headRefName", pr_branch)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        meta_error = meta_error or f"{type(exc).__name__}: {exc}"

    # 2. Get job log
    try:
        log_result = subprocess.run(
            ["gh", "api", _api_repo_path(f"actions/jobs/{job_id}/logs")],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("ERROR: gh not found — install from https://cli.github.com")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: gh timed out (log)")
        return 1

    if log_result.returncode != 0:
        if _gh_error_kind(log_result.stderr) == "notfound":
            print(_missing_log_message(job_id, job_meta, meta_absent, meta_error))
        else:
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

    if total == 0 and not raw_mode:
        # Empty and absent are two different lies this surface tells (#723):
        # `gh run view --log` returns nothing at all for jobs whose log the
        # API serves in full. Absence exits 1 above with a stated cause; a log
        # that was fetched successfully and is genuinely 0 bytes says exactly
        # that, in its own words, and never borrows the vocabulary of a log
        # that could not be read. Without this the run fell through to the
        # pattern search and printed a banner over nothing at all.
        print()
        print("## The log is empty — the fetch succeeded and returned 0 bytes")
        print(f"This is not a missing log: gh returned one, and it has no "
              f"content. Job state: status `{job_status}`, conclusion "
              f"`{job_conclusion}`.")
        logs_path = _api_repo_path("actions/jobs/" + str(job_id) + "/logs")
        print(f"Cross-check the raw bytes with: gh api {logs_path}")
        return 0

    # 3. Raw mode — dump (sliced) trace, skip filters
    if raw_mode:
        if total == 0:
            print("\n## Raw — the log is empty (0 lines)")
            return 0
        if raw_tail is not None:
            width = min(raw_tail, total)
            start, end = total - width + 1, total
        else:
            start = raw_start if raw_start is not None else 1
            end = raw_end if raw_end is not None else total
            if start > total:
                # Same trade as gl-job (#487): return the tail of the width
                # asked for, and say it is not the range that was requested.
                width = (end - start + 1) if raw_end is not None else tail_lines
                width = max(1, min(width, total))
                print(f"\n## Raw — requested {start}-{end} is past end of log "
                      f"({total} lines); showing the last {width} lines instead")
                start, end = total - width + 1, total
            end = min(end, total)
        shown = lines[start - 1:end]
        print(f"\n## Raw lines {start}-{start + len(shown) - 1} of {total}")
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
        return 0

    # 3b. Grep mode — ad-hoc regex over the log, context + gap markers.
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
        _emit_grep_hits(lines, sorted(hits), rx, match_count,
                        env_int("GH_JOB_GREP_MAX_BYTES", 65536, minimum=1),
                        "GH_JOB_GREP_MAX_BYTES", shown_pattern, ctx)
        return 0

    # 4. Error pattern search. Per-job-name table (if configured) picks tighter
    # patterns + a resolution op; else the flat default applies.
    patterns, resolution = _select_job_patterns(
        job_name, config["job_patterns"], config["error_patterns"]
    )
    resolution_line = (
        f"Resolve:  ./supertool '{resolution.replace('{id}', job_id)}'"
        if resolution else ""
    )
    error_sections = _find_error_sections(lines, patterns, config["error_context"])

    # fail/errors mode — dump ALL matched blocks, no tail cap
    if errors_mode:
        if not error_sections:
            if display_status == "failure":
                _print_unmatched_failure(job_id, display_status, patterns, lines, total)
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

    if error_sections and display_status == "failure":
        print(f"\n## Error context ({len([e for e in error_sections if e[0] > 0])} lines matched)")
        for line_num, text in error_sections:
            if line_num == -1:
                print(text)
            else:
                print(f"  {line_num:>5} | {text}")

        if resolution_line:
            print(f"\n{resolution_line}")

        print(f"\n## Tail (last {tail_lines} lines)")
        shown = lines[-tail_lines:] if len(lines) > tail_lines else lines
        start = total - len(shown) + 1
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
    elif display_status == "failure":
        # Nothing matched on a job that failed — say so, do not just print a
        # tail and let the reader infer the log was clean (#453/#445).
        _print_unmatched_failure(job_id, display_status, patterns, lines, total)
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
