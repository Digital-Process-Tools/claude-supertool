#!/usr/bin/env python3
"""gh-following: gh-following[:N] — list users I follow on GitHub."""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import env_int  # noqa: E402  (the one numeric-knob reader)
import _untrusted  # noqa: E402  (the repo's remote-text convention — #981)
import _digits  # noqa: E402  (the one ASCII-digit test — #1727)


def main(arg: str) -> int:
    # `str.isdigit()` here was an uncaught ValueError: it is True for `²`,
    # where `int()` raises, so `gh-following:²` died before it fetched anything
    # (#1727). A junk limit falls back to the default, as it always did for
    # every other non-numeric argument.
    n = (int(arg) if _digits.is_ascii_int(arg.strip())
         else env_int("SUPERTOOL_DEFAULT_LIMIT", 30, minimum=1))
    result = subprocess.run(
        ["gh", "api", f"user/following?per_page={min(n, 100)}"],
        capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        err = result.stderr.lower()
        if "401" in err or "unauthorized" in err:
            sys.stderr.write("ERROR: gh not authenticated. Run: gh auth login\n")
        else:
            # The relay #981 walked past on its way to the logins below: the
            # writer of this text is the GitHub API, and it lands at column 0
            # in a stderr the core appends to the receipt (#1606).
            sys.stderr.write("ERROR: gh following failed: "
                             f"{_untrusted.flat(result.stderr.strip())}\n")
        return 1
    try:
        users = json.loads(result.stdout)
    except json.JSONDecodeError:
        # A field inside a sentence, where a fence cannot go: flattened, then
        # sliced — the order the relay above and the logins below already use
        # (#970, #1648).
        sys.stderr.write("ERROR: bad JSON: "
                         f"{_untrusted.flat(result.stdout.strip())[:200]}\n")
        return 1
    if not users:
        print("(following 0 users)")
        return 0
    print(f"(following {len(users)} users)")
    # A login is constrained by GitHub today; the render does not borrow that
    # validation as its own line discipline, because `gh` can be pointed at
    # another host with GH_HOST and nothing here can tell (#981).
    print(_untrusted.flat_note("Logins", "GitHub"))
    for u in users[:n]:
        login = _untrusted.flat(str(u.get("login", "?")))
        url = _untrusted.flat(str(u.get("html_url", "")))
        print(f"  - @{login} ({url})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
