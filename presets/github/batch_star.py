#!/usr/bin/env python3
"""gh-batch-star: gh-batch-star:FILE — star each repo (one OWNER/REPO per line).

Lines starting with '#' are comments. Sleeps SUPERTOOL_STAR_DELAY (default 1s)
between calls.
"""
import os
import subprocess
import sys
import time
from pathlib import Path


def star(repo: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["gh", "api", f"user/starred/{repo}", "-X", "PUT", "-H", "Content-Length: 0"],
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
    repos = []
    for line in path.read_text().splitlines():
        line = line.strip().lstrip("/")
        if not line or line.startswith("#"):
            continue
        if "/" not in line:
            sys.stderr.write(f"WARN: skipping {line!r} (not OWNER/REPO)\n")
            continue
        repos.append(line)
    if not repos:
        sys.stderr.write("ERROR: no OWNER/REPO entries in file\n")
        return 2
    print(f"(batch-star {len(repos)} repos)")
    ok = 0
    failed = 0
    delay = float(os.environ.get("SUPERTOOL_STAR_DELAY", "1.0"))
    for i, repo in enumerate(repos):
        if i > 0:
            time.sleep(delay)
        success, msg = star(repo)
        marker = "OK " if success else "ERR"
        print(f"  {marker} {repo}: {msg}")
        if success:
            ok += 1
        else:
            failed += 1
    print(f"DONE: {ok} starred, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
