#!/usr/bin/env python3
"""gh-batch-star: gh-batch-star:FILE — star each repo (one OWNER/REPO per line).

A whole line starting with '#' is a comment. A '#' **after whitespace** ends the
name and starts an inline annotation, which is what `gh-find-starable` writes
(`octo/tool  # 12 stars, a description`) — until #1387 the annotation was sent
as part of the repository path and every such line 404'd, so the two ops could
not form the pipeline they exist for. The rules, and the reason a '#' with no
whitespace before it is NOT a comment, are in `_candidates.py`.

Nothing is silently reinterpreted: the receipt says how many annotations were
dropped, and a line that cannot be a name after the split is skipped by name
and counted rather than sent.

Sleeps SUPERTOOL_STAR_DELAY (default 1s) between calls.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from _env import env_float  # noqa: E402  (the one numeric-knob reader)
import _untrusted  # noqa: E402  (the repo's remote-text convention — #981)
import _auth_probe  # noqa: E402  (does this stderr *state* that the credential is unusable? - #1846)

sys.path.insert(0, str(Path(__file__).parent))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)
import _candidates  # noqa: E402  (the line format both ends honour — #1387)

#: How much of `gh`'s error text one row may carry, measured on what prints.
ERROR_MAX = 120


def star(repo: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["gh", "api", f"user/starred/{repo}", "-X", "PUT", "-H", "Content-Length: 0"],
        capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
    )
    if result.returncode == 0:
        return True, "ok"
    err = result.stderr.lower()
    # A status, never a number (#1846): a throttle carries `401` inside its
    # user id, and must reach the arm below that quotes what actually failed.
    if _auth_probe.says_not_authenticated(err):
        return False, "auth (gh auth login)"
    if "404" in err:
        return False, "not found"
    # Returned uncut: the 120-character budget is on the rendered row, and
    # `flat()` spells one U+2028 as eight characters, so a slice taken here
    # would not bound anything. The caller flattens, then slices (#970/#981).
    return False, result.stderr.strip()


def main(arg: str) -> int:
    use_utf8_stdout()
    raw = arg.strip()
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    path = Path(raw)
    if not path.is_file():
        sys.stderr.write(f"ERROR: file not found: {path}\n")
        return 2
    repos = []
    annotated = 0
    skipped: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or _candidates.is_comment(raw):
            continue
        value, annotation = _candidates.split_annotation(raw)
        if annotation:
            annotated += 1
        value = value.lstrip("/")
        why = _candidates.unusable(value) or (
            "" if "/" in value else "is not OWNER/REPO")
        if why:
            skipped.append(f"{_untrusted.flat(value or raw.strip())}: {why}")
            continue
        repos.append(value)
    for note in skipped:
        sys.stderr.write(f"WARN: skipping {note}\n")
    if not repos:
        sys.stderr.write("ERROR: no OWNER/REPO entries in file\n")
        return 2
    print(f"(batch-star {len(repos)} repos)")
    # Stated before the first write, not after the last: this is what the op
    # decided the file meant, and a reader who disagrees wants to stop now.
    if annotated:
        print(f"# {annotated} of these lines carried an inline `# ...` "
              f"annotation — the text after it was dropped and the name "
              f"before it used.")
    for note in skipped:
        print(f"  SKIP {note}")
    ok = 0
    failed = 0
    delay = env_float("SUPERTOOL_STAR_DELAY", 1.0, minimum=0.0)
    for i, repo in enumerate(repos):
        if i > 0:
            time.sleep(delay)
        success, msg = star(repo)
        marker = "OK " if success else "ERR"
        # One candidate, one row. `msg` on the failure arm is `gh`'s stderr,
        # which quotes the repository name back at us, and a fifty-line run is
        # skimmed for exactly the rows a forged one imitates (#981).
        print(f"  {marker} {_untrusted.flat(repo)}: "
              f"{_untrusted.flat(msg)[:ERROR_MAX]}")
        if success:
            ok += 1
        else:
            failed += 1
    tail = f", {len(skipped)} skipped" if skipped else ""
    print(f"DONE: {ok} starred, {failed} failed{tail}")
    if failed:
        return 1
    # A skipped line is not a success. Exit 2 is this op's "the file was not
    # what it looked like" code — the same one a missing file gets — because a
    # zero here would report a run that covered fewer repositories than the
    # reviewed list named.
    return 2 if skipped else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
