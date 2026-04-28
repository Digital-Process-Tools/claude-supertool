#!/usr/bin/env python3
"""Git file investigation — everything about a file's recent history in one call.

Combines:
1. Last N commits touching the file (who, when, what)
2. Uncommitted changes (staged + unstaged diff)
3. Blame hotspots (most recently changed lines)
"""
import os
import re
import subprocess
import sys

DEFAULT_COMMITS = 15
DEFAULT_BLAME_RECENT = 10


def _git(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a git command."""
    return subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _format_error(stderr: str, path: str) -> str:
    """Classify git errors into actionable messages."""
    s = stderr.lower()
    if "does not have any commits" in s:
        return f"ERROR: no git history for {path}. Is this a new file?"
    if "not a git repository" in s:
        return "ERROR: not inside a git repository."
    if "no such path" in s or "does not exist" in s:
        return f"ERROR: {path} not found in the repository. Check the path."
    return f"ERROR: git failed for {path}: {stderr.strip()}"


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: investigate.py PATH")
        return 1

    path = sys.argv[1]
    commits = int(os.environ.get("SUPERTOOL_COMMITS", str(DEFAULT_COMMITS)))
    blame_recent = int(os.environ.get("SUPERTOOL_BLAME_RECENT", str(DEFAULT_BLAME_RECENT)))

    # Check file exists in repo
    if not os.path.exists(path):
        print(f"ERROR: {path} does not exist.")
        return 1

    print(f"# git-investigate: {path}")

    # 1. Recent commits
    log_result = _git([
        "log", f"-{commits}", "--format=%h %ad %an | %s",
        "--date=short", "--follow", "--", path
    ])
    if log_result.returncode != 0:
        print(_format_error(log_result.stderr, path))
        return 1

    log_lines = [l for l in log_result.stdout.strip().splitlines() if l.strip()]
    print(f"\n## Recent commits ({len(log_lines)})")
    if log_lines:
        for line in log_lines:
            print(f"  {line}")
    else:
        print("  (no commits found — new file?)")

    # 2. Uncommitted changes
    diff_result = _git(["diff", "HEAD", "--", path])
    if diff_result.returncode == 0 and diff_result.stdout.strip():
        diff_lines = diff_result.stdout.strip().splitlines()
        # Count additions/deletions
        adds = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
        dels = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
        print(f"\n## Uncommitted changes (+{adds} -{dels})")
        # Show the diff, capped at 50 lines
        for line in diff_lines[:50]:
            print(f"  {line}")
        if len(diff_lines) > 50:
            print(f"  ... ({len(diff_lines) - 50} more lines)")
    else:
        print("\n## Uncommitted changes: none")

    # 3. Staged changes (separate from unstaged)
    staged_result = _git(["diff", "--cached", "--", path])
    if staged_result.returncode == 0 and staged_result.stdout.strip():
        staged_lines = staged_result.stdout.strip().splitlines()
        adds = sum(1 for l in staged_lines if l.startswith("+") and not l.startswith("+++"))
        dels = sum(1 for l in staged_lines if l.startswith("-") and not l.startswith("---"))
        print(f"\n## Staged changes (+{adds} -{dels})")
        for line in staged_lines[:30]:
            print(f"  {line}")
        if len(staged_lines) > 30:
            print(f"  ... ({len(staged_lines) - 30} more lines)")

    # 4. Blame hotspots — find the N most recently changed lines
    blame_result = _git([
        "blame", "--line-porcelain", path
    ])
    if blame_result.returncode == 0 and blame_result.stdout.strip():
        # Parse porcelain blame: extract commit date + line number
        entries: list[tuple[str, str, int, str]] = []  # (date, author, line, content)
        current_date = ""
        current_author = ""
        current_line = 0
        for line in blame_result.stdout.splitlines():
            # First line of each entry: hash orig_line final_line [num_lines]
            m = re.match(r'^[0-9a-f]{40}\s+\d+\s+(\d+)', line)
            if m:
                current_line = int(m.group(1))
            elif line.startswith("author "):
                current_author = line[7:]
            elif line.startswith("committer-time "):
                try:
                    import datetime
                    ts = int(line.split()[1])
                    current_date = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                except (ValueError, IndexError):
                    current_date = "?"
            elif line.startswith("\t"):
                entries.append((current_date, current_author, current_line, line[1:]))

        if entries:
            # Sort by date descending, take the N most recent
            entries.sort(key=lambda e: e[0], reverse=True)
            recent = entries[:blame_recent]
            # Re-sort by line number for display
            recent.sort(key=lambda e: e[2])

            print(f"\n## Blame hotspots ({blame_recent} most recently changed lines)")
            for date, author, line_num, content in recent:
                # Truncate long lines
                display = content[:80] + "..." if len(content) > 80 else content
                print(f"  {line_num:>5} | {date} {author:<20} | {display}")
    else:
        print("\n## Blame: unavailable")

    return 0


if __name__ == "__main__":
    sys.exit(main())
