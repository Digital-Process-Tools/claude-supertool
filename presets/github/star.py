#!/usr/bin/env python3
"""gh-star: gh-star:OWNER/REPO — star a GitHub repository via gh CLI."""
import subprocess
import sys


def main(arg: str) -> int:
    repo = arg.strip().lstrip("/")
    if not repo or "/" not in repo:
        sys.stderr.write("ERROR: usage gh-star:OWNER/REPO\n")
        return 2
    result = subprocess.run(
        ["gh", "api", f"user/starred/{repo}", "-X", "PUT", "-H", "Content-Length: 0"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        print(f"(starred {repo})")
        return 0
    err = result.stderr.lower()
    if "401" in err or "unauthorized" in err:
        sys.stderr.write("ERROR: gh not authenticated. Run: gh auth login\n")
    elif "404" in err:
        sys.stderr.write(f"ERROR: repo {repo} not found\n")
    else:
        sys.stderr.write(f"ERROR: gh star failed: {result.stderr.strip()}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
