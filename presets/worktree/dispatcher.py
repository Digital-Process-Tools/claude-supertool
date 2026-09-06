#!/usr/bin/env python3
"""Dispatcher for `worktree:setup[:PATH]` / `worktree:teardown[:PATH]` (#532).

Kept deliberately thin: argument parsing and target resolution live here,
everything else is `_common.py`, `setup_op.py`, `teardown_op.py`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

from _console import use_utf8_stdout  # noqa: E402

import _common  # noqa: E402
import setup_op  # noqa: E402
import teardown_op  # noqa: E402

MODES = {"setup": setup_op, "teardown": teardown_op}


def main(argv: list) -> int:
    use_utf8_stdout()
    if len(argv) < 2 or argv[1] not in MODES:
        print("ERROR: usage: worktree:setup[:PATH] | worktree:teardown[:PATH]")
        return 1

    mode = argv[1]
    path_arg = argv[2] if len(argv) > 2 else None
    invocation_cwd = os.getcwd()

    try:
        target = _common.resolve_target(path_arg)
    except _common.TargetError as exc:
        print(f"ERROR: {exc}")
        return 1

    # This op's PATH argument is exempt from the generic cwd/repo containment
    # gate (worktree.json declares "paths": {"args": []}) because pointing
    # outside cwd is the documented use case, not an edge case — provisioning
    # a SIBLING worktree, or running from inside one to read the PRIMARY
    # checkout. What is enforced instead: PATH must resolve to a worktree of
    # the SAME repository this call was invoked from, never an arbitrary
    # directory (see `_common.common_dir`'s docstring).
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

    code, output = MODES[mode].run(target)
    print(output)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
