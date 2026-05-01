#!/usr/bin/env python3
"""gh-find-starable: gh-find-starable:TOPIC[|N]

Discover repos worth starring by topic search. Pulls top N (default 30,
max 100) repos with the given topic, sorted by stars descending.

Output: OWNER/REPO + star count + description, one per line, ready to
review and pipe to gh-batch-star.

Tip: chain multiple topics for a richer pass:
  ./supertool 'gh-find-starable:claude-code' 'gh-find-starable:mcp' \\
              'gh-find-starable:ai-agents'
"""
import json
import os
import subprocess
import sys
import urllib.parse


def parse_args(arg: str) -> tuple[str, int]:
    if not arg:
        sys.stderr.write("ERROR: usage gh-find-starable:TOPIC[|N]\n")
        sys.exit(2)
    parts = arg.split("|")
    topic = parts[0].strip()
    if not topic:
        sys.stderr.write("ERROR: empty topic\n")
        sys.exit(2)
    n = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else int(
        os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "30"))
    return topic, min(n, 100)


def main(arg: str) -> int:
    topic, n = parse_args(arg)
    query = f"topic:{topic}"
    endpoint = f"search/repositories?q={urllib.parse.quote(query)}&sort=stars&order=desc&per_page={n}"
    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        sys.stderr.write(f"ERROR: gh search failed: {result.stderr.strip()[:200]}\n")
        return 1
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(f"ERROR: bad JSON: {result.stdout[:200]}\n")
        return 1
    repos = data.get("items", []) if isinstance(data, dict) else []
    if not repos:
        print(f"# 0 repos for topic {topic!r}")
        return 0
    print(f"# {len(repos)} repos for topic {topic!r} (sorted by stars)")
    print(f"# Review, delete those you don't want, then:")
    print(f"#   ./supertool 'gh-batch-star:CANDIDATES_FILE'")
    print()
    for r in repos:
        full = r.get("full_name", "?")
        stars = r.get("stargazers_count", 0)
        desc = (r.get("description") or "").replace("\n", " ")[:120]
        print(f"{full}  # {stars}★  {desc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
