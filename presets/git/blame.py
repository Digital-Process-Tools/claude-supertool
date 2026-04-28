#!/usr/bin/env python3
"""Git blame — show N lines around a specific line number with author/date/commit info."""
import os
import subprocess
import sys

DEFAULT_CONTEXT = 5


def main() -> int:
    if len(sys.argv) < 3:
        print("ERROR: usage: blame.py PATH LINE [N]")
        print("  PATH — file to blame")
        print("  LINE — line number to center on")
        print("  N    — lines of context each side (default 5)")
        return 1

    path = sys.argv[1]
    try:
        line = int(sys.argv[2])
    except ValueError:
        print(f"ERROR: LINE must be a number, got {sys.argv[2]!r}")
        return 1
    n = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_CONTEXT

    if not os.path.isfile(path):
        print(f"ERROR: file not found: {path}")
        return 1
    if line < 1:
        print(f"ERROR: line number must be >= 1, got {line}")
        return 1

    # Check git repo
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            print("ERROR: not inside a git repository.")
            return 1
    except (OSError, subprocess.TimeoutExpired):
        print("ERROR: git not available.")
        return 1

    start = max(1, line - n)
    end = line + n
    try:
        result = subprocess.run(
            ["git", "blame", "-L", f"{start},{end}", "--date=short", path],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        print(f"ERROR: git blame timed out for {path}")
        return 1
    except OSError as e:
        print(f"ERROR: git blame failed: {e}")
        return 1

    if result.returncode != 0:
        stderr = result.stderr.strip().lower()
        if "no such path" in stderr:
            print(f"ERROR: {path} not tracked by git. Is it a new file?")
        elif "invalid line range" in stderr or "has only" in stderr:
            print(f"ERROR: line {line} is out of range for {path}. Check the file length.")
        else:
            print(f"ERROR: git blame failed: {result.stderr.strip()}")
        return 1

    print(result.stdout, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
