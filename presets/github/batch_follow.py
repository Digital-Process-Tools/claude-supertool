#!/usr/bin/env python3
"""gh-batch-follow: gh-batch-follow:FILE — follow each username (one per line).

A whole line starting with '#' is a comment, and empty lines are skipped. A '#'
**after whitespace** ends the login and starts an inline annotation, which is
what `gh-find-followable` writes (`octocat  # stargazer of octo/tool`) — until
#1387 the annotation was sent as part of the login, so the producer's own output
could not be handed to this op without a hand edit. The rules, and the reason a
'#' with no whitespace before it is NOT a comment, are in `_candidates.py`.

This op follows accounts, and it does not require an OWNER/REPO shape, so it is
the one where a mis-parsed line is a write to somebody's account. Nothing is
silently reinterpreted: the receipt says how many annotations were dropped, and
a line that cannot be a login after the split is skipped by name and counted
rather than sent.

Reports per-user status and a final summary. Sleeps 1s between calls
to be polite to GitHub's abuse-detection.
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
import _status_probe  # noqa: E402  (does this stderr *state* the target is missing or access denied? - #1864)

sys.path.insert(0, str(Path(__file__).parent))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)
import _candidates  # noqa: E402  (the line format both ends honour — #1387)

#: How much of `gh`'s error text one row may carry, measured on what prints.
ERROR_MAX = 120


def follow(user: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["gh", "api", f"user/following/{user}", "-X", "PUT"],
        capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
    )
    if result.returncode == 0:
        return True, "ok"
    err = result.stderr.lower()
    # A status, never a number (#1846): a throttle carries `401` inside its
    # user id, and must reach the arm below that quotes what actually failed.
    if _auth_probe.says_not_authenticated(err):
        return False, "auth (gh auth login)"
    if _status_probe.says_not_found(err):
        return False, "not found"
    # Uncut here for the same reason as gh-batch-star: the caller flattens
    # first, and a slice taken before that bounds the wrong string (#981).
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
    users = []
    annotated = 0
    skipped: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or _candidates.is_comment(raw):
            continue
        value, annotation = _candidates.split_annotation(raw)
        if annotation:
            annotated += 1
        value = value.lstrip("@")
        why = _candidates.unusable(value)
        if why:
            skipped.append(f"{_untrusted.flat(value or raw.strip())}: {why}")
            continue
        users.append(value)
    for note in skipped:
        sys.stderr.write(f"WARN: skipping {note}\n")
    if not users:
        sys.stderr.write("ERROR: no usernames in file\n")
        return 2
    print(f"(batch-follow {len(users)} users)")
    # Stated before the first write, not after the last: this is what the op
    # decided the file meant, and a reader who disagrees wants to stop now.
    if annotated:
        print(f"# {annotated} of these lines carried an inline `# ...` "
              f"annotation — the text after it was dropped and the login "
              f"before it used.")
    for note in skipped:
        print(f"  SKIP {note}")
    ok = 0
    failed = 0
    delay = env_float("SUPERTOOL_FOLLOW_DELAY", 1.0, minimum=0.0)
    for i, user in enumerate(users):
        if i > 0:
            time.sleep(delay)
        success, msg = follow(user)
        marker = "OK " if success else "ERR"
        # Same as gh-batch-star: `msg` on the failure arm is `gh`'s stderr and
        # quotes the login back (#981).
        print(f"  {marker} @{_untrusted.flat(user)}: "
              f"{_untrusted.flat(msg)[:ERROR_MAX]}")
        if success:
            ok += 1
        else:
            failed += 1
    tail = f", {len(skipped)} skipped" if skipped else ""
    print(f"DONE: {ok} followed, {failed} failed{tail}")
    if failed:
        return 1
    # A skipped line is not a success — see gh-batch-star for the same call.
    return 2 if skipped else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
