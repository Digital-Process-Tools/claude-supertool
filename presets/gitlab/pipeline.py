#!/usr/bin/env python3
"""GitLab pipeline job list via glab CLI.

`gl-pipeline:ID` prints every job by stage. When polling a running pipeline
that's ~90 lines of `manual`/`created` bulk you never read, re-paid every turn.
Two filter modes cover the only questions you actually ask mid-pipeline:

    gl-pipeline:ID          full board — manual/created/skipped bulk collapsed
                            to a one-line count
    gl-pipeline:ID:active   only running/pending jobs — "what's still going"
    gl-pipeline:ID:failed   only failed jobs + their job IDs/URLs — "what broke"
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)
import job as gitlab_job  # noqa: E402  (shares the trace writer with gl-job — #626)
import _repo_target  # noqa: E402  (the project this call is about, if not cwd's — #676)
import _secrets  # noqa: E402  (the one GitLab token-prefix list — #1645)
import _untrusted  # noqa: E402  (a job name is CI-config text, and this is a column-aligned table — #965)
import _auth_probe  # noqa: E402  (does this stderr *state* that the credential is unusable? - #1846)
import _status_probe  # noqa: E402  (does this stderr *state* the target is missing or access denied? - #1864)

# Statuses that answer "what's still going on right now".
_ACTIVE_STATUSES = {"running", "pending"}
# Bulk statuses that are never what you're polling for — collapsed to a
# one-line count in the default view instead of one row each.
_NOISE_STATUSES = {"manual", "created", "skipped"}

_FILTERS = {"full", "active", "failed", "traces"}


def _parse_paginated_json(raw: str) -> list[dict]:
    """Parse glab's `--paginate` output — one JSON array per page, concatenated.

    glab emits each page's body back-to-back with no separator, e.g.
    `[{job1}][{job2},{job3}]`. A single `json.loads` chokes on the second `[`.
    We walk the string with `raw_decode`, consuming one document at a time and
    flattening every array's elements into a single list. Robust to the common
    single-page case (one array) and to empty pages (`[]`).
    """
    decoder = json.JSONDecoder()
    merged: list[dict] = []
    idx = 0
    length = len(raw)
    while idx < length:
        # Skip whitespace glab may insert between concatenated documents.
        while idx < length and raw[idx].isspace():
            idx += 1
        if idx >= length:
            break
        doc, end = decoder.raw_decode(raw, idx)
        if not isinstance(doc, list):
            raise ValueError("expected a JSON array per page")
        merged.extend(doc)
        idx = end
    return merged


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify glab errors into actionable messages for LLMs."""
    s = stderr.lower()
    if _status_probe.says_not_found(s):
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. "
                f"{_repo_target.gl_not_found_hint()}")
    # `_secrets.mentions_gitlab_token`, not a literal: this line read `glpat_`
    # until #1645, GitLab mints `glpat-`, and the only test over it used the
    # same wrong spelling. One list, cited to GitLab's docs, in one file.
    # A status, never a number (#1846). go-gitlab echoes the request URL into
    # every error string, so a project, job or pipeline id containing `401`
    # made a 500 or a throttle render as a missing credential.
    if (_auth_probe.says_not_authenticated(s, _auth_probe.GITLAB_MARKERS)
            or _secrets.mentions_gitlab_token(s)):
        return "ERROR: glab not authenticated. Run: glab auth login"
    if _status_probe.says_forbidden(s):
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."
    # The remote host wrote this text — flattened, never relayed raw (#1485).
    return (f"ERROR: glab failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def _print_table(jobs: list[dict]) -> None:
    """Render the job table header + one row per job, with status markers."""
    print(f"{'Job':<40} {'Stage':<20} {'Status':<12} {'Duration':<10}")
    print("-" * 82)
    for job in jobs:
        name = _untrusted.flat(str(job.get("name", "?")))
        stage = _untrusted.flat(str(job.get("stage", "?")))
        status = job.get("status", "?")
        duration = job.get("duration")
        duration_str = f"{duration:.0f}s" if duration else "-"

        marker = ""
        if status == "failed":
            marker = " <!"
        elif status == "running":
            marker = " ..."

        print(f"{name:<40} {stage:<20} {status:<12} {duration_str:<10}{marker}")


def _print_failed_detail(failed: list[dict]) -> None:
    """The failed-job names with job IDs + web URLs — the 'what broke' answer."""
    print(f"\n## Failed jobs ({len(failed)})")
    for job in failed:
        name = _untrusted.flat(str(job.get("name", "?")))
        job_id = job.get("id", "?")
        web_url = job.get("web_url", "")
        print(f"  - {name} (job #{job_id})")
        if web_url:
            print(f"    {web_url}")


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: pipeline.py PIPELINE_ID [active|failed]")
        return 1

    pipeline_id = sys.argv[1]
    mode = sys.argv[2].lower() if len(sys.argv) > 2 and sys.argv[2] else "full"
    if mode not in _FILTERS:
        print(f"ERROR: unknown filter {mode!r} — use 'active', 'failed', or omit for the full board")
        return 1

    # glab ci view doesn't support --output json directly for pipelines,
    # so we use the API endpoint
    try:
        result = subprocess.run(
            ["glab", "api",
             _repo_target.gl_api_path(
                 f"projects/:id/pipelines/{pipeline_id}/jobs"),
             "--paginate"],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("ERROR: glab not found — install from https://gitlab.com/gitlab-org/cli")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: glab timed out")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "Pipeline", pipeline_id))
        return 1

    try:
        jobs = _parse_paginated_json(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: invalid JSON from glab\n{result.stdout[:500]}")
        return 1
    except ValueError:
        print("ERROR: unexpected response format")
        return 1

    # Get pipeline status from first job's pipeline field
    pipe_status = "unknown"
    if jobs:
        pipe = jobs[0].get("pipeline", {})
        pipe_status = pipe.get("status", "unknown")

    # Sort by stage then name
    jobs.sort(key=lambda j: (j.get("stage", ""), j.get("name", "")))
    failed = [j for j in jobs if j.get("status") == "failed"]

    print(f"# Pipeline #{pipeline_id} — {pipe_status}")

    if mode == "failed":
        if not failed:
            print("No failed jobs.")
            return 0
        _print_table(failed)
        _print_failed_detail(failed)
        return 0

    if mode == "traces":
        # #626 — the pipeline is the actual entry point ("pipeline failed"),
        # not a job id, so this hands the same failed-job ids `:failed`
        # already computes to `gl-job`'s trace writer rather than growing a
        # second implementation of it here.
        if not failed:
            print("No failed jobs.")
            return 0
        # A job id from GitLab's own job-listing API, not from the job's own
        # `.gitlab-ci.yml`-controlled stdout -- but `_untrusted.flat` is
        # idempotent and cheap, and this id also builds a filename in
        # `write_traces` (#626), so flattening it here is defence in depth
        # rather than a response to a specific forgery this field can carry.
        ids = [_untrusted.flat(str(j.get("id"))) for j in failed if j.get("id") is not None]
        return gitlab_job.write_traces(ids)

    if mode == "active":
        active = [j for j in jobs if j.get("status") in _ACTIVE_STATUSES]
        if not active:
            print("No running or pending jobs.")
            return 0
        _print_table(active)
        return 0

    # Full board — collapse the manual/created/skipped bulk to a count line so
    # the running/failed/done jobs you actually care about aren't buried.
    shown = [j for j in jobs if j.get("status") not in _NOISE_STATUSES]
    hidden = [j for j in jobs if j.get("status") in _NOISE_STATUSES]

    _print_table(shown)

    if hidden:
        counts = Counter(str(j.get("status", "?")) for j in hidden)
        summary = ", ".join(f"+{n} {status}" for status, n in sorted(counts.items()))
        print(
            f"{summary} (hidden — "
            f"gl-pipeline:{pipeline_id}:active / :failed to filter)"
        )

    if failed:
        _print_failed_detail(failed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
