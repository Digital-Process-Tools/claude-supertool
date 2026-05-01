#!/usr/bin/env python3
"""gh-following: gh-following[:N] — list users I follow on GitHub."""
import json
import os
import subprocess
import sys


def main(arg: str) -> int:
    n = int(arg) if arg.strip().isdigit() else int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "30"))
    result = subprocess.run(
        ["gh", "api", f"user/following?per_page={min(n, 100)}"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        err = result.stderr.lower()
        if "401" in err or "unauthorized" in err:
            sys.stderr.write("ERROR: gh not authenticated. Run: gh auth login\n")
        else:
            sys.stderr.write(f"ERROR: gh following failed: {result.stderr.strip()}\n")
        return 1
    try:
        users = json.loads(result.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(f"ERROR: bad JSON: {result.stdout[:200]}\n")
        return 1
    if not users:
        print("(following 0 users)")
        return 0
    print(f"(following {len(users)} users)")
    for u in users[:n]:
        print(f"  - @{u.get('login','?')} ({u.get('html_url','')})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
