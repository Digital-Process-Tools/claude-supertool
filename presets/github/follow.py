#!/usr/bin/env python3
"""gh-follow: gh-follow:USERNAME — follow a GitHub user via gh CLI.

Uses the authenticated user's session (`gh auth status`).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _untrusted  # noqa: E402  (the repo's remote-text convention — #981)


def main(arg: str) -> int:
    user = arg.strip()
    if not user:
        sys.stderr.write("ERROR: usage gh-follow:USERNAME\n")
        return 2
    result = subprocess.run(
        ["gh", "api", f"user/following/{user}", "-X", "PUT"],
        capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
    )
    if result.returncode == 0:
        print(f"(followed @{user})")
        return 0
    err = result.stderr.lower()
    if "401" in err or "unauthorized" in err:
        sys.stderr.write("ERROR: gh not authenticated. Run: gh auth login\n")
    elif "404" in err:
        sys.stderr.write(f"ERROR: user @{user} not found\n")
    else:
        # Same as gh-star: gh's stderr quotes the login back (#981).
        sys.stderr.write(
            f"ERROR: gh follow failed: {_untrusted.flat(result.stderr.strip())}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
