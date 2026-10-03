#!/usr/bin/env python3













from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  
import job as gitlab_job  
import _repo_target  
import _secrets  
import _untrusted  
import _auth_probe  
import _status_probe  


_ACTIVE_STATUSES = {"running", "pending"}


_NOISE_STATUSES = {"manual", "created", "skipped"}

_FILTERS = {"full", "active", "failed", "traces"}


def _parse_paginated_json(raw: str) -> list[dict]:








    decoder = json.JSONDecoder()
    merged: list[dict] = []
    idx = 0
    length = len(raw)
    while idx < length:

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

    s = stderr.lower()
    if _status_probe.says_not_found(s):
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. "
                f"{_repo_target.gl_not_found_hint()}")






    if (_auth_probe.says_not_authenticated(s, _auth_probe.GITLAB_MARKERS)
            or _secrets.mentions_gitlab_token(s)):
        return "ERROR: glab not authenticated. Run: glab auth login"
    if _status_probe.says_forbidden(s):
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."

    return (f"ERROR: glab failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def _print_table(jobs: list[dict]) -> None:

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
        print(f"ERROR: unknown filter {mode!r} — use 'full', 'active', 'failed', 'traces', or omit for the full board")
        return 1



    try:
        result = subprocess.run(
            ["glab", "api",
             _repo_target.gl_api_path(
                 f"projects/:id/pipelines/{pipeline_id}/jobs"),
             "--paginate"],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("ERROR: glab not found — install the GitLab CLI")
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


    pipe_status = "unknown"
    if jobs:
        pipe = jobs[0].get("pipeline", {})
        pipe_status = pipe.get("status", "unknown")


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




        if not failed:
            print("No failed jobs.")
            return 0








        ids = [_untrusted.flat(str(j.get("id"))) for j in failed if j.get("id") is not None]
        return gitlab_job.write_traces(ids)

    if mode == "active":
        active = [j for j in jobs if j.get("status") in _ACTIVE_STATUSES]
        if not active:
            print("No running or pending jobs.")
            return 0
        _print_table(active)
        return 0



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
