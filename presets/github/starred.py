#!/usr/bin/env python3
"""gh-starred: gh-starred[:N] — list repos I have starred."""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)
from _env import env_int  # noqa: E402  (the one numeric-knob reader)
import _untrusted  # noqa: E402  (the repo's remote-text convention — #981)


def main(arg: str) -> int:
    use_utf8_stdout()
    n = int(arg) if arg.strip().isdigit() else env_int("SUPERTOOL_DEFAULT_LIMIT", 30, minimum=1)
    result = subprocess.run(
        ["gh", "api", f"user/starred?per_page={min(n, 100)}"],
        capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        err = result.stderr.lower()
        if "401" in err or "unauthorized" in err:
            sys.stderr.write("ERROR: gh not authenticated. Run: gh auth login\n")
        else:
            # The relay #981 walked past on its way to the fields below: the
            # writer of this text is the GitHub API, and it lands at column 0
            # in a stderr the core appends to the receipt (#1606).
            sys.stderr.write("ERROR: gh starred failed: "
                             f"{_untrusted.flat(result.stderr.strip())}\n")
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
    # These are other people's words about other people's repositories, and this
    # op exists to be read quickly. One disclosure at the top, and `flat()` per
    # field so nothing below it can reach column 0 (#981).
    print(_untrusted.flat_note("Repository names and descriptions", "GitHub"))
    for r in repos[:n]:
        full = _untrusted.flat(str(r.get("full_name", "?")))
        url = _untrusted.flat(str(r.get("html_url", "")))
        # Flatten, then slice: a cut made first leaves whatever the separator
        # started, and `flat()` spells U+2028 as eight characters (#970).
        desc = _untrusted.flat(str(r.get("description") or ""))[:120]
        print(f"  - {full} → {url}")
        if desc:
            print(f"      {desc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
