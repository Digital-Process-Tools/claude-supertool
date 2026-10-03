#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import env_int  
import _untrusted  
import _auth_probe  
import _digits  


def main(arg: str) -> int:




    n = (int(arg) if _digits.is_ascii_int(arg.strip())
         else env_int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT"), "SUPERTOOL_DEFAULT_LIMIT", 30, minimum=1))
    result = subprocess.run(
        ["gh", "api", f"user/following?per_page={min(n, 100)}"],
        capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        err = result.stderr.lower()


        if _auth_probe.says_not_authenticated(err):
            sys.stderr.write("ERROR: gh not authenticated. Run: gh auth login\n")
        else:



            sys.stderr.write("ERROR: gh following failed: "
                             f"{_untrusted.flat(result.stderr.strip())}\n")
        return 1
    try:
        users = json.loads(result.stdout)
    except json.JSONDecodeError:



        sys.stderr.write("ERROR: bad JSON: "
                         f"{_untrusted.flat(result.stdout.strip())[:200]}\n")
        return 1
    if not users:
        print("(following 0 users)")
        return 0
    print(f"(following {len(users)} users)")



    print(_untrusted.flat_note("Logins", "GitHub"))
    for u in users[:n]:
        login = _untrusted.flat(str(u.get("login", "?")))
        url = _untrusted.flat(str(u.get("html_url", "")))
        print(f"  - @{login} ({url})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
