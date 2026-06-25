#!/usr/bin/env python3
"""GitLab merge request details via glab CLI.

Fetches MR metadata, pipeline status, reviewer/approval info,
diff stats, and human comments. Dashboard-style output.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

DESCRIPTION_MAX = 2000
COMMENT_MAX = 500
COMMENT_TOTAL_MAX = 2000
TAIL_COMMENTS = 2
NAMESTATUS_DISPLAY_MAX = 50
NAMESTATUS_FETCH_CAP = 500


def _relative_age(iso: str) -> str:
    """Format an ISO timestamp as 'Nd ago', 'Nh ago', or 'Nm ago'.

    Returns '?' on parse failure. Used for MR created/updated lines so the
    agent gets stale-MR signal in one round-trip.
    """
    if not iso:
        return "?"
    try:
        from datetime import datetime, timezone
        # GitLab ISO format: 2026-05-08T10:00:00.000Z
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except (ValueError, ImportError):
        return "?"


def _glab_api(endpoint: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a glab api call."""
    return subprocess.run(
        ["glab", "api", endpoint],
        capture_output=True, text=True, timeout=timeout,
    )


def _local_branch_check(source: str) -> str:
    """Return a one-line local-branch-vs-MR-source check.

    Empty string when not in a git repo or detached HEAD. Used after the
    'Branch:' line to flag when the user is editing on the wrong branch.
    """
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


_MERGE_TREE_NOISE_PREFIXES = (
    "Auto-merging ",
    "CONFLICT ",
    "warning:",
    "hint:",
    "error:",
)


def _get_conflicting_files(source: str, target: str) -> list[str]:
    """Find files in conflict between source and target via local git.

    Uses `git merge-tree --name-only --write-tree` (git 2.38+). Returns
    deduplicated list of conflicting paths, or empty list on any failure
    (not a git repo, refs not fetched, old git version, cwd outside the
    repo). Caller falls back to the plain "Conflicts: YES" message in
    that case.
    """
    try:
        result = subprocess.run(
            ["git", "merge-tree", "--name-only", "--write-tree",
             f"origin/{target}", f"origin/{source}"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    # Exit code 0 = clean merge; >0 = conflicts, file list + git status messages on stdout.
    if result.returncode == 0:
        return []
    files: list[str] = []
    seen: set[str] = set()
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line or line in seen:
            continue
        if re.fullmatch(r"[0-9a-f]{40}", line):
            # First line of merge-tree output is the merged tree hash.
            continue
        if any(line.startswith(p) for p in _MERGE_TREE_NOISE_PREFIXES):
            # Git's status messages get mixed into stdout; drop them.
            continue
        files.append(line)
        seen.add(line)
    return files


_MERGE_TREE_HEADER_RE = re.compile(
    r"^(changed in both|added in (?:local|remote)|removed in (?:local|remote))\b"
)
_MERGE_TREE_PATH_RE = re.compile(
    r"^  (?:base|our|their)\s+\d+\s+[0-9a-f]+\s+(.+)$"
)
HUNK_LINES_PER_FILE = 40


def _get_conflict_hunks(source: str, target: str) -> dict[str, str]:
    """Return per-file conflict diff for hunk preview.

    Uses the older `git merge-tree BASE TARGET SOURCE` syntax which
    produces unified-diff-style output with `<<<<<<< / ======= / >>>>>>>`
    conflict markers. Each per-file block is split off the section
    headers ("changed in both", "added in local", etc.). Returns dict
    mapping file path -> diff text. Empty dict on any failure.
    """
    try:
        base_result = subprocess.run(
            ["git", "merge-base", f"origin/{target}", f"origin/{source}"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    if base_result.returncode != 0 or not base_result.stdout.strip():
        return {}
    base = base_result.stdout.strip()

    try:
        result = subprocess.run(
            ["git", "merge-tree", base,
             f"origin/{target}", f"origin/{source}"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    if not result.stdout:
        return {}

    blocks: dict[str, list[str]] = {}
    current_path: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        if current_path:
            blocks.setdefault(current_path, []).extend(current_lines)

    for line in result.stdout.splitlines():
        if _MERGE_TREE_HEADER_RE.match(line):
            _flush()
            current_path = None
            current_lines = []
            continue
        path_match = _MERGE_TREE_PATH_RE.match(line)
        if path_match:
            # Header is followed by 1-3 path lines (base/our/their) — same path.
            if current_path is None:
                current_path = path_match.group(1)
            continue
        if current_path is not None:
            current_lines.append(line)
    _flush()

    return {p: "\n".join(lines).strip() for p, lines in blocks.items() if any(lines)}


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify glab errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "404" in s or "not found" in s or "could not resolve" in s:
        return f"ERROR: {resource} #{identifier} not found in this repo. Check the number or verify you're in the right repo."
    if "401" in s or "unauthorized" in s or "glpat_" in s or "authenticate" in s or "bad token" in s or "token expired" in s:
        return "ERROR: glab not authenticated. Run: glab auth login"
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."
    return f"ERROR: glab failed for {resource} #{identifier}: {stderr.strip()}"


def _render_note(note: dict) -> str:
    """Format one MR note for printing. Body capped at COMMENT_MAX chars."""
    author = (note.get("author") or {}).get("username", "?")
    body = (note.get("body") or "")[:COMMENT_MAX]
    created = (note.get("created_at") or "")[:10]
    return f"\n**{author}** ({created}):\n{body}\n"


def _fmt_kb(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes}B"
    return f"{nbytes / 1024:.1f}KB"


def _budgeted_comments(notes: list, budget: int, tail: int) -> tuple[list[str], int, int]:
    """Pick rendered notes fitting a total-char budget, keeping the last `tail` for recency.

    Returns (rendered_lines, hidden_count, hidden_bytes). Notes are assumed
    sorted ascending (oldest first) — same order as the GitLab API.
    """
    rendered_all = [_render_note(n) for n in notes]
    if not rendered_all:
        return [], 0, 0
    tail_keep = min(tail, len(rendered_all))
    if tail_keep >= len(rendered_all):
        return rendered_all, 0, 0
    tail_slice = rendered_all[-tail_keep:] if tail_keep else []
    head_pool = rendered_all[:-tail_keep] if tail_keep else rendered_all
    tail_size = sum(len(r) for r in tail_slice)
    remaining = max(0, budget - tail_size)
    head_kept: list[str] = []
    used = 0
    for r in head_pool:
        if used + len(r) > remaining:
            break
        head_kept.append(r)
        used += len(r)
    hidden = head_pool[len(head_kept):]
    hidden_bytes = sum(len(r.encode("utf-8")) for r in hidden)
    return head_kept + (["__GAP__"] if hidden else []) + tail_slice, len(hidden), hidden_bytes


def _name_status_flag(f: dict) -> str:
    """Map a GitLab diff entry to a one-char change type (A/D/R/M)."""
    if f.get("new_file"):
        return "A"
    if f.get("deleted_file"):
        return "D"
    if f.get("renamed_file"):
        return "R"
    return "M"


def _get_name_status(iid: str | int, fetch_all: bool) -> list[tuple[str, str]]:
    """Return per-file (flag, path) for an MR via the paginated diffs endpoint.

    Default fetches only the first page (100 files) — enough for the display
    cap. With fetch_all (gl-mr:N:full) it paginates up to NAMESTATUS_FETCH_CAP
    files. Returns [] on any API/parse failure so the caller silently omits
    the block rather than erroring.
    """
    entries: list[tuple[str, str]] = []
    page = 1
    while True:
        try:
            r = _glab_api(
                f"projects/:id/merge_requests/{iid}/diffs?per_page=100&page={page}"
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            break
        if r.returncode != 0:
            break
        try:
            diffs = json.loads(r.stdout)
        except json.JSONDecodeError:
            break
        if not isinstance(diffs, list) or not diffs:
            break
        for f in diffs:
            flag = _name_status_flag(f)
            new_path = f.get("new_path") or ""
            old_path = f.get("old_path") or ""
            if flag == "R" and old_path and new_path and old_path != new_path:
                path = f"{old_path} → {new_path}"
            else:
                path = new_path or old_path or "?"
            entries.append((flag, path))
        if not fetch_all or len(diffs) < 100 or len(entries) >= NAMESTATUS_FETCH_CAP:
            break
        page += 1
    return entries


def _coerce_count(changes: object) -> int | None:
    """Return the leading integer of GitLab's changes_count.

    changes_count comes back as a string — "18" on normal MRs, "1000+" when
    capped. Returns the leading int (1000 for "1000+"), or None when there are
    no leading digits, so callers can fall back to the fetched-entry count.
    """
    m = re.match(r"\d+", str(changes))
    return int(m.group()) if m else None


def _render_name_status(
    entries: list[tuple[str, str]], changes: object, full: bool, iid: str | int
) -> list[str]:
    """Build the '## Files' block lines from name-status entries.

    Returns [] when there are no entries (caller omits the block). The total
    file count drives the "+N more" overflow line: it comes from changes_count
    (authoritative, survives the display cap and single-page fetch) and falls
    back to the fetched count when changes_count is missing or smaller.
    """
    if not entries:
        return []
    shown = entries if full else entries[:NAMESTATUS_DISPLAY_MAX]
    total = _coerce_count(changes)
    if total is None or total < len(entries):
        total = len(entries)
    lines = [f"\n## Files ({changes})"]
    lines.extend(f" {flag}  {path}" for flag, path in shown)
    hidden = total - len(shown)
    if hidden > 0:
        if full:
            lines.append(f" … +{hidden} more (output capped at {NAMESTATUS_FETCH_CAP} files)")
        else:
            lines.append(f" … +{hidden} more (use gl-mr:{iid}:full)")
    return lines


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: mr.py NUMBER [status|full]")
        return 1

    arg = sys.argv[1]
    flags = sys.argv[2:]
    slim = "status" in flags
    full = "full" in flags

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

    def _latest_pipeline(iid: str | int) -> dict:
        """Fetch freshest pipeline for the MR — glab mr view can return stale head_pipeline."""
        try:
            r = _glab_api(f"projects/:id/merge_requests/{iid}/pipelines?per_page=1")
            if r.returncode == 0:
                pipes = json.loads(r.stdout)
                if isinstance(pipes, list) and pipes:
                    return pipes[0]
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        return {}

    def _pipe_meta(pipeline: dict) -> str:
        """One-liner: SHA + when + source + user + duration/elapsed + coverage."""
        bits = []
        sha = (pipeline.get("sha") or "")[:8]
        if sha:
            bits.append(sha)
        # Source (push, merge_request_event, schedule, web, api, trigger, ...)
        source = pipeline.get("source")
        if source and source not in ("push",):
            bits.append(source)
        # Who triggered
        user = (pipeline.get("user") or {}).get("username")
        if user:
            bits.append(f"by {user}")
        # Time: running pipelines show elapsed; others show finished/updated
        status = pipeline.get("status", "")
        if status == "running":
            started = pipeline.get("started_at") or pipeline.get("created_at")
            if started:
                from datetime import datetime, timezone
                try:
                    dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - dt).total_seconds()
                    if elapsed > 60:
                        bits.append(f"running {int(elapsed // 60)}m")
                    else:
                        bits.append(f"running {int(elapsed)}s")
                except (ValueError, TypeError):
                    pass
        else:
            updated = (pipeline.get("updated_at") or pipeline.get("created_at") or "")[:19].replace("T", " ")
            if updated:
                bits.append(updated)
        duration = pipeline.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            mins, secs = divmod(int(duration), 60)
            bits.append(f"{mins}m{secs:02d}s" if mins else f"{secs}s")
        coverage = pipeline.get("coverage")
        if coverage is not None:
            bits.append(f"cov {coverage}%")
        return " | ".join(bits)

    if slim:
        iid = d.get("iid", arg)
        state = d.get("state", "?")
        merge_status = d.get("merge_status", "?")
        has_conflicts = d.get("has_conflicts", False)
        pipeline = _latest_pipeline(iid) or d.get("pipeline") or d.get("head_pipeline") or {}
        pipe_status = pipeline.get("status", "none")
        pipe_id = pipeline.get("id", "")
        merged_at = d.get("merged_at") or "-"
        merge_commit = d.get("merge_commit_sha") or d.get("squash_commit_sha") or ""
        web_url = d.get("web_url", "")
        print(f"!{iid} | state: {state} | merge_status: {merge_status} | conflicts: {'yes' if has_conflicts else 'no'}")
        pipe_str = pipe_status + (f" (#{pipe_id})" if pipe_id else "")
        meta = _pipe_meta(pipeline)
        if meta:
            pipe_str += f" | {meta}"
        print(f"pipeline: {pipe_str}")
        print(f"merged_at: {merged_at}")
        if merge_commit:
            print(f"merge_commit: {merge_commit[:12]}")
        if web_url:
            print(f"url: {web_url}")
        return 0

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
    merge_status = d.get("merge_status") or d.get("detailed_merge_status") or "?"
    merge_commit = d.get("merge_commit_sha") or d.get("squash_commit_sha") or ""
    draft = d.get("draft", False) or d.get("work_in_progress", False)

    # Pipeline — fetch latest from MR pipelines endpoint (head_pipeline can be stale)
    pipeline = _latest_pipeline(iid) or d.get("pipeline") or d.get("head_pipeline") or {}
    pipe_status = pipeline.get("status", "none")
    pipe_id = pipeline.get("id", "")
    pipe_meta = _pipe_meta(pipeline)

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
    local_check = _local_branch_check(source)
    if local_check:
        print(local_check)
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")

    # Assignees (distinct from reviewers on GitLab)
    assignees = d.get("assignees") or []
    assignee_names = [a.get("username", "?") for a in assignees]
    print(f"Assignees: {', '.join(assignee_names) if assignee_names else 'none'}")

    # Reviewers + approvals — always print so absence is signal, not silence
    print(f"Reviewers: {', '.join(reviewer_names) if reviewer_names else 'none'}")

    # Age — created/updated, for stale-MR signal
    created_at = d.get("created_at") or ""
    updated_at = d.get("updated_at") or ""
    if created_at:
        age_str = f"Created: {_relative_age(created_at)}"
        if updated_at and updated_at != created_at:
            age_str += f" | Updated: {_relative_age(updated_at)}"
        print(age_str)

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

    # Unresolved discussion threads — distinct blocker from comments
    try:
        disc_result = _glab_api(
            f"projects/:id/merge_requests/{iid}/discussions?per_page=100"
        )
        if disc_result.returncode == 0:
            discussions = json.loads(disc_result.stdout)
            if isinstance(discussions, list):
                resolvable = [dd for dd in discussions
                              if any(n.get("resolvable") for n in (dd.get("notes") or []))]
                unresolved = [dd for dd in resolvable
                              if not all(n.get("resolved") for n in (dd.get("notes") or []) if n.get("resolvable"))]
                print(f"Unresolved threads: {len(unresolved)} / {len(resolvable)}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # Pipeline + changes
    pipe_str = pipe_status
    if pipe_id:
        pipe_str += f" (#{pipe_id})"
    if pipe_meta:
        pipe_str += f" | {pipe_meta}"
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

    if additions or deletions:
        print(f"Changes: {changes} files, +{additions} -{deletions}")
    elif changes:
        # glab mr view omits diff_stats on large MRs (typically 1000+ files)
        print(f"Changes: {changes} files (line counts unavailable on large MRs)")

    # File-level name-status — the deletion/addition list is the high-signal
    # scan when reviewing an MR. Default-capped; gl-mr:N:full uncaps.
    if changes:
        for line in _render_name_status(_get_name_status(iid, full), changes, full, iid):
            print(line)

    # Conflicts
    conflict_files: list[str] = []
    if has_conflicts:
        conflict_files = _get_conflicting_files(source, target)
        if conflict_files:
            print(f"Conflicts: YES — cannot merge ({len(conflict_files)} file{'s' if len(conflict_files) != 1 else ''})")
        else:
            print("Conflicts: YES — cannot merge")
    else:
        print(f"Merge status: {merge_status}")

    # Merge commit (if merged)
    if merge_commit:
        print(f"Merge commit: {merge_commit[:12]}")

    if web_url:
        print(f"URL: {web_url}")

    # Conflict file list + hunks — only when conflicts exist and we computed them
    if conflict_files:
        plural = "s" if len(conflict_files) != 1 else ""
        print(f"\n## Conflicts ({len(conflict_files)} file{plural})")
        for path in conflict_files:
            print(f"  {path}")

        hunks = _get_conflict_hunks(source, target)
        for path in conflict_files:
            block = hunks.get(path, "")
            if not block:
                continue
            lines = block.splitlines()
            truncated = ""
            if len(lines) > HUNK_LINES_PER_FILE:
                extra = len(lines) - HUNK_LINES_PER_FILE
                lines = lines[:HUNK_LINES_PER_FILE]
                truncated = f"\n  ... ({extra} more lines)"
            print(f"\n### {path}")
            for line in lines:
                print(f"  {line}")
            if truncated:
                print(truncated)

        print("\nTo resolve:")
        print(f"  git checkout {source} && git fetch origin && git merge origin/{target}")
        files_arg = " ".join(conflict_files)
        print(f"  # Resolve <<<<<<< markers in the files above, then:")
        print(f"  git add {files_arg} && git commit && git push")

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
    else:
        print("\n## Description\n_(empty)_")

    # Human comments (notes) — always print header so absence is signal,
    # not silence. Mirrors gh-pr behavior.
    human_notes: list = []
    try:
        notes_result = _glab_api(
            f"projects/:id/merge_requests/{iid}/notes?per_page=50&sort=asc"
        )
        if notes_result.returncode == 0:
            notes = json.loads(notes_result.stdout)
            if isinstance(notes, list):
                human_notes = [n for n in notes if not n.get("system", False)]
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    print(f"\n## Comments ({len(human_notes)})")
    if full:
        for r in (_render_note(n) for n in human_notes):
            print(r, end="")
    else:
        rendered, hidden_count, hidden_bytes = _budgeted_comments(
            human_notes, COMMENT_TOTAL_MAX, TAIL_COMMENTS,
        )
        for r in rendered:
            if r == "__GAP__":
                print(
                    f"\n... {hidden_count} more comment(s) hidden ({_fmt_kb(hidden_bytes)})."
                    f" Use gl-mr:{iid}:full for everything."
                )
                continue
            print(r, end="")

    return 0


if __name__ == "__main__":
    sys.exit(main())
