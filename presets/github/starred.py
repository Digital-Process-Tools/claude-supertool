#!/usr/bin/env python3
"""gh-starred: gh-starred[:N] — list repos I have starred."""
import json
import os
import subprocess
import sys


def main(arg: str) -> int:
    n = int(arg) if arg.strip().isdigit() else int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "30"))
    result = subprocess.run(
        ["gh", "api", f"user/starred?per_page={min(n, 100)}"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        err = result.stderr.lower()
        if "401" in err or "unauthorized" in err:
            sys.stderr.write("ERROR: gh not authenticated. Run: gh auth login\n")
        else:
            sys.stderr.write(f"ERROR: gh starred failed: {result.stderr.strip()}\n")
        return 1
    try:
        repos = json.loads(result.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(f"ERROR: bad JSON: {result.stdout[:200]}\n")
        return 1
    if not repos:
        print("(0 starred repos)")
        return 0
    print(f"(starred {len(repos)} repos)")
    for r in repos[:n]:
        full = r.get("full_name", "?")
        url = r.get("html_url", "")
        desc = (r.get("description") or "")[:120]
        print(f"  - {full} → {url}")
        if desc:
            print(f"      {desc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
