#!/usr/bin/env python3
"""Git file investigation — everything about a file's recent history in one call.

Combines:
1. Last N commits touching the file (who, when, what)
2. Uncommitted changes (staged + unstaged diff)
3. Blame hotspots (most recently changed lines)
"""
from __future__ import annotations

import datetime
import os
import re
import sys

# Sibling import: runtime puts this dir on sys.path[0]; the test harness
# loads scripts via importlib (no dir on path), so add it explicitly.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))  # for _env (#654)

from _git_common import _git, _git_verbatim, use_utf8_stdout  # noqa: E402
from _env import env_int  # noqa: E402  (the one numeric-knob reader)
import _untrusted  # noqa: E402  (git hands log subjects and file CONTENT back raw — #1693)

#: The one separator `git blame --line-porcelain` defines (#1693).
#:
#: Every record git writes there is LF-terminated, and the last line of each
#: record is the blamed file's own line behind a single TAB. A file line cannot
#: contain LF — that is what makes it a line — so once the bytes are intact,
#: splitting on LF alone makes a forged record structurally impossible rather
#: than merely unlikely.
#:
#: **"Once the bytes are intact" is the load-bearing half, and it is why the
#: blame call goes through `_git_verbatim`.** Two readers had to be narrowed
#: here, not one:
#:
#: * `str.splitlines()`, which this was, folds on U+2028, U+0085 and the
#:   vertical tab — none of which git writes in this stream — so a source line
#:   spelling `<U+2028>author Mallory<U+2028><TAB>text` produced a blame row
#:   carrying an author, a date and a line number no commit ever had.
#: * `_git` itself, which runs `Popen(text=True)` and therefore
#:   rewrites a lone CR into LF before any splitter sees it. Measured on git
#:   2.46.2 against a real repository: `x = 1<CR>author Mallory<CR><TAB>I did
#:   this` arrived as three lines, two of them reading as git's. No splitter
#:   can undo that, so the reader takes the bytes instead.
#:
#: This site is absent from the #1130 register by construction; see
#: `test_the_narrowed_readers_did_not_quietly_revert`, which names why.
_LF = chr(10)

DEFAULT_COMMITS = 15
DEFAULT_BLAME_RECENT = 10


def _blame_entries(stream: str) -> list[tuple[str, str, int, str]]:
    """`(date, author, line number, content)` per line of a porcelain blame.

    Named and lifted out of `main` so the forgery this closes can be driven
    directly (#1693). The stream must come from `_git_verbatim`: `_git` runs in
    text mode, which rewrites a lone CR into LF, and that rewrite is itself the
    forgery — see that function.

    The split is git's own separator and nothing wider. Every record git writes
    here is LF-terminated and its last line is the blamed file's own line
    behind one TAB, so with the bytes intact a file line cannot start a record:
    a line containing LF is not a line. `str.splitlines()`, which this was,
    folds on U+2028, U+0085 and the vertical tab as well — none of which git
    writes in this stream — so a source line spelling
    `<U+2028>author X<U+2028><TAB>text` added a row carrying an author, a date
    and a line number no commit had.

    Nothing here is a state machine, deliberately. A record's fields are only
    interpreted at all because a content line begins with TAB and no header or
    field does; once the separator is git's own, that is the whole grammar and
    a stricter parse would be machinery around a property already held.
    """
    entries: list[tuple[str, str, int, str]] = []
    current_date = ""
    current_author = ""
    current_line = 0
    for raw in stream.split(_LF):
        # One trailing CR, and only a trailing one. Reading the bytes means a
        # CRLF blob's content lines each arrive with their own CR still on
        # them, and `visible()` would then put a `␍` at the end of every row in
        # a repository that has done nothing wrong — the readability `_git`'s
        # text mode used to give away for free. `_untrusted.visible`'s own
        # docstring says the caller normalises the pair before asking (#854),
        # and this is that. A CR anywhere ELSE in the line stays, which is the
        # whole point: mid-line is where the forgery lives.
        line = raw[:-1] if raw.endswith(chr(13)) else raw
        # First line of each entry: hash orig_line final_line [num_lines]
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
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: investigate.py PATH")
        return 1

    path = sys.argv[1]
    commits = env_int("SUPERTOOL_COMMITS", DEFAULT_COMMITS, minimum=1)
    blame_recent = env_int("SUPERTOOL_BLAME_RECENT", DEFAULT_BLAME_RECENT, minimum=0)

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

    # #1681's class, in the function it did not reach: every line rendered and
    # counted, so the count IS the product. `log --format=%s` hands a U+2028
    # back raw (measured, git 2.46.2), so one commit could add a row here and
    # choose the number beside it. `split_lines` keeps the count honest;
    # `visible()` keeps the separator out of a row this tool presents as its
    # own — the split alone fixes only the first half (#1693).
    log_lines = [_untrusted.visible(l)
                 for l in _untrusted.split_lines(log_result.stdout.strip())
                 if l.strip()]
    print(f"\n## Recent commits ({len(log_lines)})")
    if log_lines:
        for line in log_lines:
            print(f"  {line}")
    else:
        print("  (no commits found — new file?)")

    # 2. Uncommitted changes
    diff_result = _git(["diff", "HEAD", "--", path])
    if diff_result.returncode == 0 and diff_result.stdout.strip():
        # Same class, same reason (#1693): these are the file's own lines, so
        # the `+`/`-` counts and the rows below are both a stranger's to move.
        # `keep` is the TAB because a diff body is indented source and the row
        # is read as code.
        diff_lines = [_untrusted.visible(l, keep=chr(9))
                      for l in _untrusted.split_lines(diff_result.stdout.strip())]
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

    # 4. Blame hotspots — find the N most recently changed lines
    # `_git_verbatim`, not `_git`: this stream carries the blamed file's own
    # lines, and text mode would turn a bare CR in one of them into a line
    # break before any splitter here could refuse to honour it (#1693).
    blame_result = _git_verbatim([
        "blame", "--line-porcelain", "--", path
    ])
    if blame_result.returncode == 0 and blame_result.stdout.strip():
        entries = _blame_entries(blame_result.stdout)

        if entries:
            # Sort by date descending, take the N most recent
            entries.sort(key=lambda e: e[0], reverse=True)
            recent = entries[:blame_recent]
            # Re-sort by line number for display
            recent.sort(key=lambda e: e[2])

            print(f"\n## Blame hotspots ({blame_recent} most recently changed lines)")
            for date, author, line_num, content in recent:
                # Disclosed before truncation, not after: `visible()` expands a
                # control character into an escape, so measuring 80 first would
                # cut one in half. The author is git's relay of a commit's own
                # author field and the content is the file's own line — neither
                # is the tool's text, and both land in a table (#1693).
                shown = _untrusted.visible(content)
                display = shown[:80] + "..." if len(shown) > 80 else shown
                print(f"  {line_num:>5} | {date} "
                      f"{_untrusted.visible(author):<20} | {display}")
    else:
        print("\n## Blame: unavailable")

    return 0


if __name__ == "__main__":
    sys.exit(main())
