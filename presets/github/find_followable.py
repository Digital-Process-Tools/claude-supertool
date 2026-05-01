#!/usr/bin/env python3
"""gh-find-followable: gh-find-followable:OWNER/REPO[|N]

Discovers candidate users to follow by pulling:
  - up to N stargazers of OWNER/REPO (default N=100, max 300)
  - all contributors of OWNER/REPO

Output: unique deduped user logins, one per line, alphabetical, with
the anchor source as a comment header. Pipe to a file then run
`gh-batch-follow:FILE` after review.

Filters out type=Organization (we follow people, not orgs).
"""
import json
import os
import subprocess
import sys


def fetch(endpoint: str) -> list[dict]:
    # No --paginate — popular repos have 100k+ stars and would timeout.
    # Caller paginates explicitly via per_page parameter on the endpoint.
    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        sys.stderr.write(f"WARN: gh api {endpoint} failed: {result.stderr.strip()[:200]}\n")
        return []
    out: list[dict] = []
    for chunk in result.stdout.split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            out.extend(parsed)
        elif isinstance(parsed, dict):
            out.append(parsed)
    return out


def parse_args(arg: str) -> tuple[str, int]:
    if not arg:
        sys.stderr.write("ERROR: usage gh-find-followable:OWNER/REPO[|N]\n")
        sys.exit(2)
    parts = arg.split("|")
    repo = parts[0].strip().lstrip("/")
    if "/" not in repo:
        sys.stderr.write(f"ERROR: expected OWNER/REPO, got {repo!r}\n")
        sys.exit(2)
    n = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else int(
        os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "100"))
    return repo, min(n, 300)


def main(arg: str) -> int:
    repo, n = parse_args(arg)
    pages = (n + 99) // 100
    stargazers = fetch(f"repos/{repo}/stargazers?per_page=100")[:n] if pages else []
    contributors = fetch(f"repos/{repo}/contributors?per_page=100")
    seen: set[str] = set()
    rows: list[tuple[str, str]] = []  # (login, source)
    for u in stargazers:
        if u.get("type") != "User":
            continue
        login = u.get("login")
        if not login or login in seen:
            continue
        seen.add(login)
        rows.append((login, "stargazer"))
    for u in contributors:
        if u.get("type") != "User":
            continue
        login = u.get("login")
        if not login or login in seen:
            continue
        seen.add(login)
        rows.append((login, "contributor"))
    rows.sort(key=lambda r: r[0].lower())
    print(f"# {len(rows)} candidates from {repo} (stargazers + contributors, orgs excluded)")
    print(f"# Review this list, delete who you don't want, then:")
    print(f"#   ./supertool 'gh-batch-follow:CANDIDATES_FILE'")
    print()
    for login, source in rows:
        print(f"{login}  # {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
