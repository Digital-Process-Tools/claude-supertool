#!/usr/bin/env python3
"""gh-batch-follow: gh-batch-follow:FILE — follow each username (one per line).

Lines starting with '#' are skipped (comments). Empty lines skipped.
Reports per-user status and a final summary. Sleeps 1s between calls
to be polite to GitHub's abuse-detection.
"""
import os
import subprocess
import sys
import time
from pathlib import Path


def follow(user: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["gh", "api", f"user/following/{user}", "-X", "PUT"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        return True, "ok"
    err = result.stderr.lower()
    if "401" in err or "unauthorized" in err:
        return False, "auth (gh auth login)"
    if "404" in err:
        return False, "not found"
    return False, result.stderr.strip()[:120]


def main(arg: str) -> int:
    raw = arg.strip()
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    path = Path(raw)
    if not path.is_file():
        sys.stderr.write(f"ERROR: file not found: {path}\n")
        return 2
    users = []
    for line in path.read_text().splitlines():
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
    delay = float(os.environ.get("SUPERTOOL_FOLLOW_DELAY", "1.0"))
    for i, user in enumerate(users):
        if i > 0:
            time.sleep(delay)
        success, msg = follow(user)
        marker = "OK " if success else "ERR"
        print(f"  {marker} @{user}: {msg}")
        if success:
            ok += 1
        else:
            failed += 1
    print(f"DONE: {ok} followed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
