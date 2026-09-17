"""presets' own copy of `which_excluding_cwd` (#2596).

A preset runs with `presets/` on `sys.path`, not the repo root, so it cannot
import `validators/common/spawnable.py` the way validator/formatter adapters
do -- the same boundary `presets/_untrusted.py::_LINE_BREAK_RE` already
crosses by duplication rather than import (`_supertool._LINE_BREAK_PATTERN`
restated three times "by necessity": the core, `presets/_untrusted.py`, and
`validators/common/linebreaks.py`, each pinned equal to the others). This is
that same shape applied to `which_excluding_cwd`: a third stated copy
(`validators/common/spawnable.py`, `_supertool.py`, and here), pinned equal to
the other two by `tests/test_bare_spawn_cwd_gate_2596.py` rather than trusted
to stay in sync by hand.

See `validators/common/spawnable.py::which_excluding_cwd` for the full
rationale (#2575): `shutil.which()` inserts the current directory ahead of
every real `PATH` entry on Windows, even when a caller passes an explicit
`path=` (the insertion happens in CPython's own source outside the
`path is None` branch), so a repository shipping `gh.exe`, `glab.exe`,
`lsof.exe`, `ps.exe` or `osascript.exe` at its own root would have that file
resolved -- and then spawned, since none of these presets' subprocess calls
pass their own `cwd=` -- ahead of the real tool, while a maintainer's forge
token sits in the environment. Every bare `shutil.which(name)` gate in
`presets/` that feeds a subprocess spawn is routed through this instead of
raw `shutil.which()` (#2596): `presets/git/_git_common.py` (`glab`/`gh`),
`presets/git/conflicts.py` (`glab`/`gh`, an independent copy of the same
lookup), `presets/_git_run.py` and `presets/git/worktrees.py` (`lsof`), and
`presets/watch/transport.py` (`osascript`, `ps`).
"""
from __future__ import annotations

import os
import shutil


def which_excluding_cwd(name: str) -> "str | None":
    """`shutil.which(name)`, but a match found only via the current
    directory never wins (#2575, #2596).

    Identical algorithm to `validators/common/spawnable.py::
    which_excluding_cwd` -- duplicated rather than imported (see module
    docstring) and pinned equal to it, and to `_supertool.py`'s own copy, by
    `tests/test_bare_spawn_cwd_gate_2596.py`.

    A `name` that already contains a directory component carries none of
    this risk and is delegated to `shutil.which()` directly. Otherwise this
    walks `PATH` itself in the same order `shutil.which()` would (PATHEXT
    included on Windows), except a PATH entry that IS the current directory
    is skipped rather than returned -- a genuine PATH entry still resolves;
    only the implicit, attacker-reachable cwd match is refused.
    """
    if os.path.dirname(name):
        return shutil.which(name)
    path_env = os.environ.get("PATH")
    if not path_env:
        return None
    here = os.path.normcase(os.path.abspath(os.curdir))
    exts = [""]
    if os.name == "nt":
        raw_pathext = os.getenv("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        exts = [""] + [e for e in raw_pathext.split(os.pathsep) if e]
    seen = set()
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        entry_abs = os.path.abspath(entry)
        norm = os.path.normcase(entry_abs)
        if norm in seen:
            continue
        seen.add(norm)
        if norm == here:
            continue
        for ext in exts:
            candidate = os.path.join(entry, name + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None
