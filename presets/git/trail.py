#!/usr/bin/env python3





from __future__ import annotations

import os
import re
import sys



_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))  

from _git_common import _git, use_utf8_stdout  
from _env import env_int  
import _untrusted  

DEFAULT_MAX_COMMITS = 20
DEFAULT_CONTEXT = 3




DEFAULT_DETAIL_CAP = 10


def _format_error(stderr: str, pattern: str) -> str:

    s = stderr.lower()
    if "not a git repository" in s:
        return "ERROR: not inside a git repository."
    if "bad revision" in s:
        return f"ERROR: invalid revision range while searching for {pattern!r}."


    return (f"ERROR: git failed searching for {pattern!r}: "
            f"{_untrusted.flat(stderr.strip())}")


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: trail.py PATTERN [PATH]")
        print("  PATTERN — string to trace (function name, constant, variable)")
        print("  PATH    — optional, limit search to this file/directory")
        return 1

    pattern = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else ""
    max_commits = env_int("SUPERTOOL_MAX_COMMITS", DEFAULT_MAX_COMMITS, minimum=1)
    context = env_int("SUPERTOOL_CONTEXT", DEFAULT_CONTEXT, minimum=0)
    detail_cap = env_int("SUPERTOOL_TRAIL_DETAIL_CAP", DEFAULT_DETAIL_CAP, minimum=0)

    print(f"# git-trail: {pattern!r}" + (f" in {path}" if path else ""))





    log_args = [
        "log", f"-{max_commits + 1}", f"-S{pattern}",
        "--format=%h %ad %an | %s", "--date=short"
    ]
    if path:
        log_args.extend(["--", path])

    log_result = _git(log_args, timeout=15)
    if log_result.returncode != 0:
        print(_format_error(log_result.stderr, pattern))
        return 1









    commits = [_untrusted.visible(l.strip())
               for l in _untrusted.split_lines(log_result.stdout.strip())
               if l.strip()]

    if not commits:

        log_args_regex = [
            "log", f"-{max_commits + 1}", f"-G{pattern}",
            "--format=%h %ad %an | %s", "--date=short"
        ]
        if path:
            log_args_regex.extend(["--", path])
        regex_result = _git(log_args_regex, timeout=15)
        if regex_result.returncode == 0:

            commits = [_untrusted.visible(l.strip())
                       for l in _untrusted.split_lines(regex_result.stdout.strip())
                       if l.strip()]
            if commits:
                print("(matched via regex -G, not exact pickaxe -S)")

    if not commits:
        print(f"\nNo commits found where {pattern!r} was added or removed.")
        if path:
            print(f"Searched in: {path}")
        print("Try without a path restriction, or check spelling.")
        return 0

    timeline_cut = len(commits) > max_commits
    if timeline_cut:
        commits = commits[:max_commits]

    timeline_header = f"\n## Timeline ({len(commits)} commits)"
    if timeline_cut:



        timeline_header += (" [CAPPED: newest {} by count, more exist — raise "
                            "SUPERTOOL_MAX_COMMITS=N]").format(max_commits)
    print(timeline_header)
    for line in commits:
        print(f"  {line}")





    commit_hashes = [c.split()[0] for c in commits]
    detailed = commit_hashes[:detail_cap]
    detail_cut = len(commit_hashes) > len(detailed)



    pool = f"{len(commit_hashes)}+" if timeline_cut else str(len(commit_hashes))

    header = "\n## Details"
    if detail_cut:
        header += (f" [CAPPED: {len(detailed)} of {pool} commits "
                   f"shown by count — raise SUPERTOOL_TRAIL_DETAIL_CAP=N]")
    print(header)

    for sha in detailed:

        msg_result = _git(["log", "-1", "--format=%h %ad %an | %s", "--date=short", sha])




        msg = (_untrusted.flat(msg_result.stdout.strip())
               if msg_result.returncode == 0 else sha)


        diff_args = ["show", sha, f"--diff-filter=ACDMR", f"-U{context}"]
        if path:
            diff_args.extend(["--", path])

        diff_result = _git(diff_args, timeout=10)
        if diff_result.returncode != 0:
            print(f"\n### {msg}")
            print("  (diff unavailable)")
            continue











        diff_lines = [_untrusted.visible(l, keep="\t")
                      for l in _untrusted.split_lines(diff_result.stdout)]
        relevant_hunks: list[str] = []
        current_hunk: list[str] = []
        current_file = ""
        hunk_has_pattern = False

        for line in diff_lines:
            if line.startswith("diff --git"):

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


        if hunk_has_pattern and current_hunk:
            if current_file and current_file not in relevant_hunks:
                relevant_hunks.append(current_file)
            relevant_hunks.extend(current_hunk)

        print(f"\n### {msg}")
        if relevant_hunks:
            for line in relevant_hunks[:40]:  
                print(f"  {line}")
            if len(relevant_hunks) > 40:
                print(f"  ... ({len(relevant_hunks) - 40} more lines)")
        else:
            print("  (pattern in binary or renamed file — diff not shown)")

    if detail_cut:
        print(
            f"\n({len(detailed)} of {pool} commits shown in detail "
            f"— cut by a count limit of {detail_cap}, not a size budget; raise "
            f"SUPERTOOL_TRAIL_DETAIL_CAP=N or pass a PATH to narrow the search)"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
