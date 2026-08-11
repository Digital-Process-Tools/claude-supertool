#!/usr/bin/env python3
"""gh-batch-follow: gh-batch-follow:FILE — follow each username (one per line).

Lines starting with '#' are skipped (comments). Empty lines skipped.
Reports per-user status and a final summary. Sleeps 1s between calls
to be polite to GitHub's abuse-detection.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from _env import env_float  # noqa: E402  (the one numeric-knob reader)
import _untrusted  # noqa: E402  (the repo's remote-text convention — #981)

#: How much of `gh`'s error text one row may carry, measured on what prints.
ERROR_MAX = 120


def follow(user: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["gh", "api", f"user/following/{user}", "-X", "PUT"],
        capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
    )
    if result.returncode == 0:
        return True, "ok"
    err = result.stderr.lower()
    if "401" in err or "unauthorized" in err:
        return False, "auth (gh auth login)"
    if "404" in err:
        return False, "not found"
    # Uncut here for the same reason as gh-batch-star: the caller flattens
    # first, and a slice taken before that bounds the wrong string (#981).
    return False, result.stderr.strip()


def main(arg: str) -> int:
    raw = arg.strip()
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    path = Path(raw)
    if not path.is_file():
        sys.stderr.write(f"ERROR: file not found: {path}\n")
        return 2
    users = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        users.append(line.lstrip("@"))
    if not users:
        sys.stderr.write("ERROR: no usernames in file\n")
        return 2
    print(f"(batch-follow {len(users)} users)")
    ok = 0
    failed = 0
    delay = env_float("SUPERTOOL_FOLLOW_DELAY", 1.0, minimum=0.0)
    for i, user in enumerate(users):
        if i > 0:
            time.sleep(delay)
        success, msg = follow(user)
        marker = "OK " if success else "ERR"
        # Same as gh-batch-star: `msg` on the failure arm is `gh`'s stderr and
        # quotes the login back (#981).
        print(f"  {marker} @{_untrusted.flat(user)}: "
              f"{_untrusted.flat(msg)[:ERROR_MAX]}")
        if success:
            ok += 1
        else:
            failed += 1
    print(f"DONE: {ok} followed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
