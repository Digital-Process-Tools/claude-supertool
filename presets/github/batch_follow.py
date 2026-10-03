#!/usr/bin/env python3


















from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from _env import env_float  
import _untrusted  
import _auth_probe  
import _status_probe  

sys.path.insert(0, str(Path(__file__).parent))
from _console import use_utf8_stdout  
import _candidates  


ERROR_MAX = 120


def follow(user: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["gh", "api", f"user/following/{user}", "-X", "PUT"],
        capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
    )
    if result.returncode == 0:
        return True, "ok"
    err = result.stderr.lower()


    if _auth_probe.says_not_authenticated(err):
        return False, "auth (gh auth login)"
    if _status_probe.says_not_found(err):
        return False, "not found"


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

    return 2 if skipped else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
