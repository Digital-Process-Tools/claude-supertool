#!/usr/bin/env python3





from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

from _console import use_utf8_stdout  

import _common  
import setup_op  
import teardown_op  

MODES = {"setup": setup_op, "teardown": teardown_op}


def main(argv: list) -> int:
    use_utf8_stdout()
    if len(argv) < 2 or argv[1] not in MODES:
        print("ERROR: usage: worktree:setup[:PATH] | worktree:teardown[:PATH][:force]")
        return 1

    mode = argv[1]
    path_arg = argv[2] if len(argv) > 2 else None
    flag = argv[3] if len(argv) > 3 else None
    force = False
    if flag:
        if flag != "force":
            print(f"ERROR: unrecognised flag {flag!r} — only 'force' is accepted")
            return 1
        if mode != "teardown":
            print("ERROR: 'force' only applies to worktree:teardown (a stale `copy` entry, #2429)")
            return 1
        force = True
    invocation_cwd = os.getcwd()

    try:
        target = _common.resolve_target(path_arg)
    except _common.TargetError as exc:
        print(f"ERROR: {exc}")
        return 1








    if path_arg:
        this_repo = _common.common_dir(Path(invocation_cwd))
        target_repo = _common.common_dir(target)
        if this_repo is None or target_repo is None:
            print("ERROR: could not confirm PATH belongs to the same repository this was called from — refusing to touch it")
            return 1
        if this_repo != target_repo:
            print(f"ERROR: {target} is a worktree of a different repository ({target_repo}) "
                  f"than the one this was called from ({this_repo}) — refusing to touch it")
            return 1

    if mode == "teardown":
        code, output = teardown_op.run(target, force=force)
    else:
        code, output = setup_op.run(target)
    print(output)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
