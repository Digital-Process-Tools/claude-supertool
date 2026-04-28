#!/usr/bin/env python3
"""Git trail — trace a symbol/pattern through history via pickaxe search.

Answers: "When was this added? When was it changed? When was it removed?"
Combines git log -S (pickaxe) with contextual diffs for each hit.
"""
import os
import re
import subprocess
import sys

DEFAULT_MAX_COMMITS = 20
DEFAULT_CONTEXT = 3


def _git(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a git command."""
    return subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _format_error(stderr: str, pattern: str) -> str:
    """Classify git errors into actionable messages."""
    s = stderr.lower()
    if "not a git repository" in s:
        return "ERROR: not inside a git repository."
    if "bad revision" in s:
        return f"ERROR: invalid revision range while searching for {pattern!r}."
    return f"ERROR: git failed searching for {pattern!r}: {stderr.strip()}"


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: trail.py PATTERN [PATH]")
        print("  PATTERN — string to trace (function name, constant, variable)")
        print("  PATH    — optional, limit search to this file/directory")
        return 1

    pattern = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else ""
    max_commits = int(os.environ.get("SUPERTOOL_MAX_COMMITS", str(DEFAULT_MAX_COMMITS)))
    context = int(os.environ.get("SUPERTOOL_CONTEXT", str(DEFAULT_CONTEXT)))

    print(f"# git-trail: {pattern!r}" + (f" in {path}" if path else ""))

    # 1. Pickaxe search — find commits where pattern was added or removed
    log_args = [
        "log", f"-{max_commits}", f"-S{pattern}",
        "--format=%h %ad %an | %s", "--date=short"
    ]
    if path:
        log_args.extend(["--", path])

    log_result = _git(log_args, timeout=15)
    if log_result.returncode != 0:
        print(_format_error(log_result.stderr, pattern))
        return 1

    commits = [l.strip() for l in log_result.stdout.strip().splitlines() if l.strip()]

    if not commits:
        # Try regex search as fallback
        log_args_regex = [
            "log", f"-{max_commits}", f"-G{pattern}",
            "--format=%h %ad %an | %s", "--date=short"
        ]
        if path:
            log_args_regex.extend(["--", path])
        regex_result = _git(log_args_regex, timeout=15)
        if regex_result.returncode == 0:
            commits = [l.strip() for l in regex_result.stdout.strip().splitlines() if l.strip()]
            if commits:
                print("(matched via regex -G, not exact pickaxe -S)")

    if not commits:
        print(f"\nNo commits found where {pattern!r} was added or removed.")
        if path:
            print(f"Searched in: {path}")
        print("Try without a path restriction, or check spelling.")
        return 0

    print(f"\n## Timeline ({len(commits)} commits)")
    for line in commits:
        print(f"  {line}")

    # 2. Show contextual diff for each commit (what changed around the pattern)
    print(f"\n## Details")
    commit_hashes = [c.split()[0] for c in commits]

    for i, sha in enumerate(commit_hashes[:10]):  # limit detail to 10
        # Get the commit one-liner
        msg_result = _git(["log", "-1", "--format=%h %ad %an | %s", "--date=short", sha])
        msg = msg_result.stdout.strip() if msg_result.returncode == 0 else sha

        # Get the diff for this commit, filtered to lines containing the pattern
        diff_args = ["show", sha, f"--diff-filter=ACDMR", f"-U{context}"]
        if path:
            diff_args.extend(["--", path])

        diff_result = _git(diff_args, timeout=10)
        if diff_result.returncode != 0:
            print(f"\n### {msg}")
            print("  (diff unavailable)")
            continue

        # Extract only hunks containing the pattern
        diff_lines = diff_result.stdout.splitlines()
        relevant_hunks: list[str] = []
        current_hunk: list[str] = []
        current_file = ""
        hunk_has_pattern = False

        for line in diff_lines:
            if line.startswith("diff --git"):
                # Save previous hunk if relevant
                if hunk_has_pattern and current_hunk:
                    if current_file:
                        relevant_hunks.append(current_file)
                    relevant_hunks.extend(current_hunk)
                current_hunk = []
                hunk_has_pattern = False
                current_file = line
            elif line.startswith("@@"):
                if hunk_has_pattern and current_hunk:
                    if current_file and current_file not in relevant_hunks:
                        relevant_hunks.append(current_file)
                    relevant_hunks.extend(current_hunk)
                current_hunk = [line]
                hunk_has_pattern = False
            else:
                current_hunk.append(line)
                if pattern in line:
                    hunk_has_pattern = True

        # Don't forget the last hunk
        if hunk_has_pattern and current_hunk:
            if current_file and current_file not in relevant_hunks:
                relevant_hunks.append(current_file)
            relevant_hunks.extend(current_hunk)

        print(f"\n### {msg}")
        if relevant_hunks:
            for line in relevant_hunks[:40]:  # cap per commit
                print(f"  {line}")
            if len(relevant_hunks) > 40:
                print(f"  ... ({len(relevant_hunks) - 40} more lines)")
        else:
            print("  (pattern in binary or renamed file — diff not shown)")

    if len(commit_hashes) > 10:
        print(f"\n({len(commit_hashes) - 10} more commits — showing first 10 only)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
