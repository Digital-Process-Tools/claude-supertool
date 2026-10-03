#!/usr/bin/env python3







from __future__ import annotations

import datetime
import os
import re
import sys



_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))  

from _git_common import _git, _git_verbatim, use_utf8_stdout  
from _env import env_int  
import _untrusted  

























_LF = chr(10)

DEFAULT_COMMITS = 15
DEFAULT_BLAME_RECENT = 10


def _blame_entries(stream: str) -> list[tuple[str, str, int, str]]:





















    entries: list[tuple[str, str, int, str]] = []
    current_date = ""
    current_author = ""
    current_line = 0
    for raw in stream.split(_LF):








        line = raw[:-1] if raw.endswith(chr(13)) else raw

        m = re.match(r'^[0-9a-f]{40}\s+\d+\s+(\d+)', line)
        if m:
            current_line = int(m.group(1))
        elif line.startswith("author "):
            current_author = line[7:]
        elif line.startswith("committer-time "):
            try:
                ts = int(line.split()[1])
                current_date = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except (ValueError, IndexError):
                current_date = "?"
        elif line.startswith(chr(9)):
            entries.append((current_date, current_author, current_line, line[1:]))
    return entries


def _format_error(stderr: str, path: str) -> str:

    s = stderr.lower()
    if "does not have any commits" in s:
        return f"ERROR: no git history for {path}. Is this a new file?"
    if "not a git repository" in s:
        return "ERROR: not inside a git repository."
    if "no such path" in s or "does not exist" in s:
        return f"ERROR: {path} not found in the repository. Check the path."
    return f"ERROR: git failed for {path}: {stderr.strip()}"


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: investigate.py PATH")
        return 1

    path = sys.argv[1]
    commits = env_int(os.environ.get("SUPERTOOL_COMMITS"), "SUPERTOOL_COMMITS", DEFAULT_COMMITS, minimum=1)
    blame_recent = env_int(os.environ.get("SUPERTOOL_BLAME_RECENT"), "SUPERTOOL_BLAME_RECENT", DEFAULT_BLAME_RECENT, minimum=0)


    if not os.path.exists(path):
        print(f"ERROR: {path} does not exist.")
        return 1

    print(f"# git-investigate: {path}")


    log_result = _git([
        "log", f"-{commits}", "--format=%h %ad %an | %s",
        "--date=short", "--follow", "--", path
    ])
    if log_result.returncode != 0:
        print(_format_error(log_result.stderr, path))
        return 1







    log_lines = [_untrusted.visible(l)
                 for l in _untrusted.split_lines(log_result.stdout.strip())
                 if l.strip()]
    print(f"\n## Recent commits ({len(log_lines)})")
    if log_lines:
        for line in log_lines:
            print(f"  {line}")
    else:
        print("  (no commits found — new file?)")


    diff_result = _git(["diff", "HEAD", "--", path])
    if diff_result.returncode == 0 and diff_result.stdout.strip():




        diff_lines = [_untrusted.visible(l, keep=chr(9))
                      for l in _untrusted.split_lines(diff_result.stdout.strip())]

        adds = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
        dels = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
        print(f"\n## Uncommitted changes (+{adds} -{dels})")

        for line in diff_lines[:50]:
            print(f"  {line}")
        if len(diff_lines) > 50:
            print(f"  ... ({len(diff_lines) - 50} more lines)")
    else:
        print("\n## Uncommitted changes: none")


    staged_result = _git(["diff", "--cached", "--", path])
    if staged_result.returncode == 0 and staged_result.stdout.strip():
        staged_lines = [_untrusted.visible(l, keep=chr(9))
                        for l in _untrusted.split_lines(
                            staged_result.stdout.strip())]
        adds = sum(1 for l in staged_lines if l.startswith("+") and not l.startswith("+++"))
        dels = sum(1 for l in staged_lines if l.startswith("-") and not l.startswith("---"))
        print(f"\n## Staged changes (+{adds} -{dels})")
        for line in staged_lines[:30]:
            print(f"  {line}")
        if len(staged_lines) > 30:
            print(f"  ... ({len(staged_lines) - 30} more lines)")





    blame_result = _git_verbatim([
        "blame", "--line-porcelain", "--", path
    ])
    if blame_result.returncode == 0 and blame_result.stdout.strip():
        entries = _blame_entries(blame_result.stdout)

        if entries:

            entries.sort(key=lambda e: e[0], reverse=True)
            recent = entries[:blame_recent]

            recent.sort(key=lambda e: e[2])

            print(f"\n## Blame hotspots ({blame_recent} most recently changed lines)")
            for date, author, line_num, content in recent:





                shown = _untrusted.visible(content)
                display = shown[:80] + "..." if len(shown) > 80 else shown
                print(f"  {line_num:>5} | {date} "
                      f"{_untrusted.visible(author):<20} | {display}")
    else:
        print("\n## Blame: unavailable")

    return 0


if __name__ == "__main__":
    sys.exit(main())
