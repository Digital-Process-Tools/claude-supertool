#!/usr/bin/env python3
"""GitLab pipeline job list via glab CLI."""
import json
import subprocess
import sys


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify glab errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "404" in s or "not found" in s or "could not resolve" in s:
        return f"ERROR: {resource} #{identifier} not found. Check the ID or verify you're in the right repo."
    if "401" in s or "unauthorized" in s or "token" in s:
        return "ERROR: glab not authenticated. Run: glab auth login"
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."
    return f"ERROR: glab failed for {resource} #{identifier}: {stderr.strip()}"


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: pipeline.py PIPELINE_ID")
        return 1

    pipeline_id = sys.argv[1]

    # glab ci view doesn't support --output json directly for pipelines,
    # so we use the API endpoint
    try:
        result = subprocess.run(
            ["glab", "api", f"projects/:id/pipelines/{pipeline_id}/jobs",
             "--paginate"],
            capture_output=True, text=True, timeout=15,
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
        jobs = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: invalid JSON from glab\n{result.stdout[:500]}")
        return 1

    if not isinstance(jobs, list):
        print("ERROR: unexpected response format")
        return 1

    # Get pipeline status from first job's pipeline field
    pipe_status = "unknown"
    if jobs:
        pipe = jobs[0].get("pipeline", {})
        pipe_status = pipe.get("status", "unknown")

    print(f"# Pipeline #{pipeline_id} — {pipe_status}")
    print(f"{'Job':<40} {'Stage':<20} {'Status':<12} {'Duration':<10}")
    print("-" * 82)

    # Sort by stage then name
    jobs.sort(key=lambda j: (j.get("stage", ""), j.get("name", "")))

    failed = []
    for job in jobs:
        name = job.get("name", "?")
        stage = job.get("stage", "?")
        status = job.get("status", "?")
        duration = job.get("duration")
        duration_str = f"{duration:.0f}s" if duration else "-"

        marker = ""
        if status == "failed":
            marker = " <!"
            failed.append(job)
        elif status == "success":
            marker = ""
        elif status == "running":
            marker = " ..."

        print(f"{name:<40} {stage:<20} {status:<12} {duration_str:<10}{marker}")

    if failed:
        print(f"\n## Failed jobs ({len(failed)})")
        for job in failed:
            name = job.get("name", "?")
            job_id = job.get("id", "?")
            web_url = job.get("web_url", "")
            print(f"  - {name} (job #{job_id})")
            if web_url:
                print(f"    {web_url}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
