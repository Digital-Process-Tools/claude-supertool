#!/usr/bin/env python3
"""GitHub Actions workflow run details via gh CLI."""
import json
import subprocess
import sys


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify gh errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "could not resolve" in s or "404" in s or "not found" in s:
        return f"ERROR: {resource} #{identifier} not found. Check the ID or verify you're in the right repo (gh repo view)."
    if "auth" in s or "login" in s or "token" in s:
        return f"ERROR: gh CLI not authenticated. Run: gh auth login"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes and retry."
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check repo access (gh auth status)."
    return f"ERROR: gh failed for {resource} #{identifier}: {stderr.strip()}"


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: run.py RUN_ID")
        return 1

    run_id = sys.argv[1]

    # Fetch run metadata
    try:
        result = subprocess.run(
            ["gh", "run", "view", run_id, "--json",
             "databaseId,name,status,conclusion,event,headBranch,"
             "createdAt,updatedAt,url,jobs"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        print("ERROR: gh not found — install from https://cli.github.com")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: gh timed out")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "Workflow run", run_id))
        return 1

    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: invalid JSON from gh\n{result.stdout[:500]}")
        return 1

    name = d.get("name", "?")
    status = d.get("status", "?")
    conclusion = d.get("conclusion", "")
    event = d.get("event", "?")
    branch = d.get("headBranch", "?")
    web_url = d.get("url", "")
    run_display = conclusion if conclusion else status

    print(f"# Run #{run_id} — {name}")
    print(f"Status: {run_display} | Event: {event} | Branch: {branch}")
    if web_url:
        print(f"URL: {web_url}")

    # Jobs
    jobs = d.get("jobs", [])
    if jobs:
        print(f"\n{'Job':<40} {'Status':<12} {'Conclusion':<12} {'Duration':<10}")
        print("-" * 74)

        failed = []
        for job in jobs:
            j_name = job.get("name", "?")
            j_status = job.get("status", "?")
            j_conclusion = job.get("conclusion") or "-"
            j_id = job.get("databaseId", "?")

            # Calculate duration from steps if available
            steps = job.get("steps", [])
            duration_str = "-"
            if steps:
                completed = sum(
                    1 for s in steps
                    if s.get("conclusion") in ("success", "failure", "skipped")
                )
                duration_str = f"{completed}/{len(steps)} steps"

            marker = ""
            if j_conclusion == "failure":
                marker = " <!"
                failed.append(job)

            print(f"{j_name:<40} {j_status:<12} {j_conclusion:<12} {duration_str:<10}{marker}")

        if failed:
            print(f"\n## Failed jobs ({len(failed)})")
            for job in failed:
                j_name = job.get("name", "?")
                j_id = job.get("databaseId", "?")
                print(f"  - {j_name} (job #{j_id})")
                # Show failed steps
                for step in job.get("steps", []):
                    if step.get("conclusion") == "failure":
                        print(f"    step: {step.get('name', '?')}")
    else:
        print("\nNo jobs found.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
