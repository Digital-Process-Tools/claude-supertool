#!/usr/bin/env python3












from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  
import _untrusted  
import _digits  
from _env import env_int  


def parse_args(arg: str) -> tuple[str, int]:
    if not arg:
        sys.stderr.write("ERROR: usage gh-find-starable:TOPIC[|N]\n")
        sys.exit(2)
    parts = arg.split("|")
    topic = parts[0].strip()
    if not topic:
        sys.stderr.write("ERROR: empty topic\n")
        sys.exit(2)








    n = (int(parts[1])
         if len(parts) > 1 and _digits.is_ascii_int(parts[1].strip())
         else env_int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT"), "SUPERTOOL_DEFAULT_LIMIT", 30, minimum=1))
    return topic, min(n, 100)


def main(arg: str) -> int:
    use_utf8_stdout()
    topic, n = parse_args(arg)
    query = f"topic:{topic}"
    endpoint = f"search/repositories?q={urllib.parse.quote(query)}&sort=stars&order=desc&per_page={n}"
    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:



        detail = _untrusted.flat(result.stderr.strip())[:200]
        sys.stderr.write(f"ERROR: gh search failed: {detail}\n")
        return 1
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:


        sys.stderr.write("ERROR: bad JSON: "
                         f"{_untrusted.flat(result.stdout.strip())[:200]}\n")
        return 1
    repos = data.get("items", []) if isinstance(data, dict) else []
    if not repos:
        print(f"# 0 repos for topic {topic!r}")
        return 0
    print(f"# {len(repos)} repos for topic {topic!r} (sorted by stars)")



    print(f"# {_untrusted.flat_note('Repository names and descriptions', 'GitHub')}")
    print(f"# Review, delete those you don't want, then:")
    print(f"#   ./supertool 'gh-batch-star:CANDIDATES_FILE'")
    print()
    for r in repos:
        full = _untrusted.flat(str(r.get("full_name", "?")))
        stars = r.get("stargazers_count", 0)




        desc = _untrusted.flat(str(r.get("description") or ""))[:120]
        print(f"{full}  # {stars}★  {desc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
