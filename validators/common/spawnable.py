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

import shutil


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
    """
    return shutil.which(name)


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
    """
    return shutil.which(name) or name
