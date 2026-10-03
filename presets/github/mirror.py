#!/usr/bin/env python3




































from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  
import _mirror  






_READERS = {"issue": _mirror.read_issue, "pr": _mirror.read_pr}


def _usage() -> int:
    kinds = "|".join(_READERS)
    print(f"ERROR: usage: gh-mirror:({kinds}):NUMBER")
    return 1


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) != 3 or sys.argv[1] not in _READERS:
        return _usage()
    kind = sys.argv[1]
    reader = _READERS[kind]
    number = sys.argv[2].strip()
    if not number.isdigit():
        print(f"ERROR: {sys.argv[2]!r} is not a plain {kind} number")
        return 1

    cfg = _mirror.load_config(Path.cwd().resolve())
    if cfg.error is not None:
        print(f"#{number}: mirror-unreadable -- config error: {cfg.error}")
        return 0
    if cfg.path is None:
        key = _mirror.CONFIG_KEY
        print(
            f"gh mirror: not configured -- set \"{key}\" in "
            f".supertool.json to enable it"
        )
        return 0

    hit = reader(cfg.path, number)
    if hit.state == _mirror.CACHED:
        print(f"#{number}: cached ({hit.age})")
    elif hit.state == _mirror.NOT_CACHED:
        print(f"#{number}: not-cached")
    else:
        print(f"#{number}: mirror-unreadable -- {hit.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
