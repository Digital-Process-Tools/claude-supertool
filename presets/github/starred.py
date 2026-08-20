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
import _auth_probe  # noqa: E402  (does this stderr *state* that the credential is unusable? - #1846)
import _digits  # noqa: E402  (the one ASCII-digit test — #1727)


def main(arg: str) -> int:
    use_utf8_stdout()
    # `str.isdigit()` here was an uncaught ValueError — True for `²`, where
    # `int()` raises, so `gh-starred:²` died before it fetched anything (#1727).
    n = (int(arg) if _digits.is_ascii_int(arg.strip())
         else env_int("SUPERTOOL_DEFAULT_LIMIT", 30, minimum=1))
    result = subprocess.run(
        ["gh", "api", f"user/starred?per_page={min(n, 100)}"],
        capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        err = result.stderr.lower()
        # A status, never a number (#1846): a throttle carries `401` inside its
        # user id, and must reach the arm below that quotes what actually failed.
        if _auth_probe.says_not_authenticated(err):
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
        # A field inside a sentence, where a fence cannot go: flattened, then
        # sliced — the order the relay above and the descriptions below already
        # use (#970, #1648).
        sys.stderr.write("ERROR: bad JSON: "
                         f"{_untrusted.flat(result.stdout.strip())[:200]}\n")
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
