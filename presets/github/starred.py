#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  
from _env import env_int  
import _untrusted  
import _auth_probe  
import _digits  


def main(arg: str) -> int:
    use_utf8_stdout()


    n = (int(arg) if _digits.is_ascii_int(arg.strip())
         else env_int("SUPERTOOL_DEFAULT_LIMIT", 30, minimum=1))
    result = subprocess.run(
        ["gh", "api", f"user/starred?per_page={min(n, 100)}"],
        capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        err = result.stderr.lower()


        if _auth_probe.says_not_authenticated(err):
            sys.stderr.write("ERROR: gh not authenticated. Run: gh auth login\n")
        else:



            sys.stderr.write("ERROR: gh starred failed: "
                             f"{_untrusted.flat(result.stderr.strip())}\n")
        return 1
    try:
        repos = json.loads(result.stdout)
    except json.JSONDecodeError:



        sys.stderr.write("ERROR: bad JSON: "
                         f"{_untrusted.flat(result.stdout.strip())[:200]}\n")
        return 1
    if not repos:
        print("(0 starred repos)")
        return 0
    print(f"(starred {len(repos)} repos)")



    print(_untrusted.flat_note("Repository names and descriptions", "GitHub"))
    for r in repos[:n]:
        full = _untrusted.flat(str(r.get("full_name", "?")))
        url = _untrusted.flat(str(r.get("html_url", "")))


        desc = _untrusted.flat(str(r.get("description") or ""))[:120]
        print(f"  - {full} → {url}")
        if desc:
            print(f"      {desc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
