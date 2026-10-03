#!/usr/bin/env python3












from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _untrusted  
import _digits  
from _env import env_int  


def fetch(endpoint: str) -> list[dict]:


    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:


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
    rows: list[tuple[str, str]] = []  
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



    print(f"# {_untrusted.flat_note('Logins', 'GitHub')}")
    print(f"# Review this list, delete who you don't want, then:")
    print(f"#   ./supertool 'gh-batch-follow:CANDIDATES_FILE'")
    print()
    for login, source in rows:


        print(f"{_untrusted.flat(login)}  # {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
