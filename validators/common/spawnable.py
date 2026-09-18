"""Turn a tool name into something `subprocess` can actually spawn (#2540).

`shutil.which()` and `subprocess` disagree on Windows, and every adapter
that gated on the first and spawned the second inherited the disagreement.
`which()` consults ``PATHEXT``, so it finds and returns `eslint.cmd`.
``subprocess`` without a shell goes through ``CreateProcess``, which
searches ``PATH`` but appends only ``.exe`` -- it cannot find that file, and
raises ``FileNotFoundError`` for a tool `which()` just said was present.

Measured, not argued. ``tests/test_windows_cmd_spawn_2540.py`` puts a
``.cmd`` shim on ``PATH`` and asks a windows runner, on 3.9 through 3.12:

  * `which("probe")` returns `probe.cmd`
  * `subprocess.run(["probe"])` raises `FileNotFoundError`
  * `subprocess.run([<that .cmd path>])` runs it and returns its exit code

The third is the fix and it is the whole fix. ``CreateProcess`` launches a
``.bat`` or ``.cmd`` given a path to one; what it will not do is *find* one.
So the bare name is the entire defect and `which()`'s own return value is
the entire repair.

**Not `shell=True`.** #2538 could pass `shell: true` safely because its argv
is a fixed static list with no metacharacters. Adapter argv lists carry a
caller-supplied file path, and ``subprocess.run(list, shell=True)``
concatenates unescaped on Windows, so a path holding a space or an ``&``
would become shell syntax. Not ``cmd /c`` either: it works, measured, but it
is an extra process per spawn buying nothing over the line below.

The failure this closes is quiet by construction. Five of the seven adapters
#2540 named routed the `FileNotFoundError` into `refusal.absent()`, so
nothing ever fabricated a clean verdict -- they reported `skipped`, which is
honest and also indistinguishable from a tool nobody installed. On Windows
they simply never ran, on any file, and said the same wrong thing about why:
"a PATH entry that vanished between the two", word for word in three files,
guessing at a race for something that fires every time.

``tests/test_spawnable_register_2540.py`` is the register that keeps the
class closed.
"""
from __future__ import annotations

import os
import shutil


def which_excluding_cwd(name: str) -> "str | None":
    """`shutil.which(name)`, but a match found only via the current
    directory never wins (#2575).

    On Windows, `shutil.which()` inserts `os.curdir` at the front of its
    own search path unless `NoDefaultCurrentDirectoryInExePath` is set --
    and it does this even when a caller passes an explicit `path=`: in
    CPython's own source the insertion happens after the `path is None`
    branch, not inside it, so overriding `path` does not skip it. A
    repository containing `ruff.cmd`, `markdownlint.cmd` or `git.cmd` at
    its own root therefore has that file resolved ahead of every real PATH
    entry -- and every validator/formatter chokepoint in this repo spawns
    its adapter subprocess with no `cwd=`, so that adapter inherits
    supertool's own cwd: the repository under inspection.

    An **absolute** directory component carries none of this risk --
    `shutil.which()`'s own dirname branch never touches PATH or the
    current directory at all -- and is delegated to directly. A
    **relative** one (`./bin`, `tools/x`) is still resolved against cwd;
    that shape is deliberate and already config-gated (a validator's
    `env` can set a binary to a relative path, per docs/validators.md),
    not an oversight, and is left alone here (#2613).

    Otherwise this walks `PATH` itself, in the same order `shutil.which()`
    would search it past the curdir-insertion step (PATHEXT included on
    Windows), except an entry whose directory is the current one is
    skipped rather than returned. A genuine PATH entry still resolves; only
    the implicit, attacker-reachable cwd match is refused, so a repo-planted
    binary can shadow nothing further down PATH than itself.
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


def spawnable(name: str) -> "str | None":
    """The spawnable form of `name`, or None when it is not on PATH.

    Replaces the `shutil.which(name)` presence check *and* supplies argv[0]:
    the two must be the same answer, and this returns it once so they cannot
    drift. `None` is the absent-tool arm the caller already has -- keep
    routing it through `refusal.absent()`, unchanged.

    Applied to every tool, not only the ones shipped as npm packages. An
    allowlist of "which tools are a .cmd on Windows" is a list that rots and
    a new adapter never joins; spawning the resolved path is correct for a
    real ``.exe`` too, and makes every adapter spawn exactly what it probed.

    Routed through `which_excluding_cwd` rather than `shutil.which`
    directly: a bare match that only exists in the current directory --
    the repository under inspection -- is never trusted (#2575).
    """
    return which_excluding_cwd(name)


def _already_a_path(name: str) -> bool:
    """Is `name` an executable file on its own, needing no PATH search?

    Never send one of those through `which()`. On Python 3.12 `shutil.which`
    was rewritten to resolve even an explicit path as
    ``os.path.join(os.path.dirname(cmd), os.path.basename(cmd))``, so a
    forward-slash-normalised Windows path comes back with a native separator
    spliced in before the filename:
    ``C:/Program Files/glab/glab.exe`` -> ``C:/Program Files/glab\\glab.exe``.
    Measured on the windows 3.12 leg, and on no other leg: 3.9 through 3.11
    return the argument verbatim, which is why this cost eight tests on one
    of twelve legs and none locally.

    That rewriting would undo the separator normalisation #2176 and #2249
    exist to get right, and it buys nothing: a path that resolves here was
    already spawnable, `.cmd` included.

    Gated on `name` containing a directory component (#2575). Without that,
    a BARE name -- "ruff", no separator -- checked with `os.path.isfile()`
    resolves relative to the current directory exactly like the
    `shutil.which()` curdir insertion this module exists to stop: a repo
    shipping a file literally named `ruff` at its own root would satisfy
    this check and then be returned unresolved-but-"already a path" by
    `argv0`, which is the same "file supplied by the repository is run as
    a program" shape, reached without ever calling `which()` at all.
    """
    return bool(os.path.dirname(name)) and os.path.isfile(name) and os.access(name, os.X_OK)


def already_a_path(name: str) -> bool:
    """Public alias of `_already_a_path` (#2602).

    Seven adapter gates each reimplemented this check inline as
    `Path(X).exists() and os.access(X, os.X_OK)` -- with no dirname guard,
    so a bare separator-free name resolved relative to the current
    directory exactly like the `which()` curdir insertion this module
    exists to stop (#2575 fixed the first disjunct of those same gates,
    `shutil.which` -> `spawnable`, and left this second one behind). Call
    this instead of duplicating the check.
    """
    return _already_a_path(name)


def argv0(name: str) -> str:
    """The spawnable form of `name`, or `name` unchanged when it is absent.

    This is what goes at the head of an argv list. The caller's existing
    absent-tool gate is untouched and still decides whether to run at all;
    when it passed, this hands `subprocess` the resolved path rather than the
    name, which is the difference between running and `FileNotFoundError` on
    Windows.

    Returning `name` on a miss rather than raising keeps every caller's
    `except FileNotFoundError` arm reachable and reporting exactly what it
    reports today -- a tool that disappeared between the gate and the spawn
    is still an absent tool, and that arm already says so.

    A value that is already an executable path is returned byte-identical;
    see `_already_a_path` for the 3.12 `shutil.which` rewrite that makes
    that explicit rather than incidental.

    Resolved through `which_excluding_cwd` rather than `shutil.which`
    directly, so a match that only exists in the current directory -- the
    repository under inspection -- falls through to the miss arm above
    instead of being spawned (#2575).
    """
    if _already_a_path(name):
        return name
    return which_excluding_cwd(name) or name
