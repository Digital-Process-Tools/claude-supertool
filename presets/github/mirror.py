#!/usr/bin/env python3
"""Read op over the write-through gh mirror `presets/_mirror.py` populates (#1955).

`gh-issue` writes an untruncated copy of every issue it fetches, opt-in via
`.supertool.json`'s `gh_mirror_dir`. This op answers "what does the mirror
currently hold for issue N" without touching the network -- the whole point
of a write-through cache: an answer whose age IS the age of the read that
produced it, never a poll that invents a freshness it cannot have.

Four renders, and each is a distinct question:

  not configured   -- nobody turned the mirror on (`gh_mirror_dir` unset).
                       Not a finding: most repos will never set this key.
  cached (AGE)      -- `gh-issue` fetched this number and the mirror is intact.
  not-cached        -- the mirror is on and intact, but this number has never
                       been read via `gh-issue`. Scope note: ONLY `gh-issue`
                       writes through this mirror so far (see `_mirror.py`'s
                       module docstring) -- this state does NOT mean the
                       issue does not exist on the tracker, only that this
                       mirror has never seen it.
  mirror-unreadable -- the mirror is configured but broken (bad ownership,
                       corrupted manifest, a body file the manifest promises
                       but disk does not have). Never rendered as either of
                       the states above -- a caller must be able to tell "the
                       cache is empty" from "the cache is lying".

**Not an authority for anything a decision turns on** (#1955's own "what it
must not become"). This op answers what is on disk in the mirror, nothing
about the tracker's live state.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  # noqa: E402  (self-review, #1955: hit.detail/cfg.error can embed an arbitrary path or exception message, and every sibling gh-* op makes this call before printing one)
import _mirror  # noqa: E402


def _usage() -> int:
    print("ERROR: usage: gh-mirror:issue:NUMBER")
    return 1


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) != 3 or sys.argv[1] != "issue":
        return _usage()
    number = sys.argv[2].strip()
    if not number.isdigit():
        print(f"ERROR: {sys.argv[2]!r} is not a plain issue number")
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

    hit = _mirror.read_issue(cfg.path, number)
    if hit.state == _mirror.CACHED:
        print(f"#{number}: cached ({hit.age})")
    elif hit.state == _mirror.NOT_CACHED:
        print(f"#{number}: not-cached")
    else:
        print(f"#{number}: mirror-unreadable -- {hit.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
