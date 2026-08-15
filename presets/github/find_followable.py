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
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _untrusted  # noqa: E402  (the repo's remote-text convention — #981)
import _digits  # noqa: E402  (the one ASCII-digit test — #1727)
from _env import env_int  # noqa: E402  (the one numeric-knob reader — #654)


def fetch(endpoint: str) -> list[dict]:
    # No --paginate — popular repos have 100k+ stars and would timeout.
    # Caller paginates explicitly via per_page parameter on the endpoint.
    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        # Flatten first, slice second — a cut made first leaves whatever the
        # separator started (#970). The writer is the GitHub API (#1606).
        detail = _untrusted.flat(result.stderr.strip())[:200]
        sys.stderr.write(f"WARN: gh api {endpoint} failed: {detail}\n")
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
    # ASCII digits, not `str.isdigit()`: the latter is True for `²`, where
    # `int()` raises and this line died before anything was fetched (#1727).
    #
    # `env_int` rather than a bare `int(os.environ.get())` — see the twin of
    # this line in `find_starable.py`. Both were invisible to #654's register
    # because it matched a regex per line and the call was wrapped across two.
    n = (int(parts[1])
         if len(parts) > 1 and _digits.is_ascii_int(parts[1].strip())
         else env_int("SUPERTOOL_DEFAULT_LIMIT", 100, minimum=1))
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
    # A `#` comment, not a bare banner. `gh-batch-follow` skips comment lines
    # and follows every other one — it does not even require an `OWNER/REPO`
    # shape — so an uncommented disclosure is an account it tries to follow.
    print(f"# {_untrusted.flat_note('Logins', 'GitHub')}")
    print(f"# Review this list, delete who you don't want, then:")
    print(f"#   ./supertool 'gh-batch-follow:CANDIDATES_FILE'")
    print()
    for login, source in rows:
        # One candidate, one line: a login carrying a separator would otherwise
        # add a second target to the file this list becomes (#981).
        print(f"{_untrusted.flat(login)}  # {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
