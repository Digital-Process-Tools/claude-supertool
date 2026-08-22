#!/usr/bin/env python3
"""One runnable `supertool` invocation, for every printed remedy (#1012/#905).

A printed command is a claim about the environment it will be pasted into,
and every one of these was written from the environment of whoever wrote it.
`./supertool` is a gitignored symlink: present in a clone, absent in a linked
worktree -- which is where agents work. In *this* repo it is worse than
absent there, because the global `supertool` on PATH then resolves to the
live clone and runs master's core against the branch's presets, the mixed
tree #678 discloses after the fact and the repo's own rule forbids in
advance. So the hint is decided by what is on disk beside the presets, never
by what the author had.

That matters most where it is printed. A rule in a docs page is consulted; a
command in an error is pasted, by a reader who is mid-conflict and least
likely to second-guess it.

The interpreter is `sys.executable` -- the one demonstrably running this
code -- never the literal `python3` (#1017). `python3` is not the launcher on
Windows, where it is `py` or `python`, so the hard-coded spelling printed a
remedy that did not run on the platform this project cannot see.

Three states. With neither route present the invocation is unknown, and an
invented one would be a remedy that cannot be run -- the defect one layer
down.

Written for #905 alongside `presets/git/_git_common.py`'s own `st_hint`, not
as a replacement for it: that module's version was only reachable from
`presets/git/`, so every printed remedy outside that one directory
(`_branch_locale.py`, `_repo_target.py`, `presets/gitlab/`, `presets/github/`)
kept a hand-built `./supertool '...'` literal instead -- correct in a clone, a
raw `no such file or directory` from a worktree, which is the exact
environment agents work in. `_git_common.py` is deliberately left untouched
rather than turned into a re-export of this module: `commit.py`, `conflicts.py`,
`status.py` and `push.py` still import ITS `st_hint`, and
`tests/test_printed_invocation_worktree_1012.py` pins
`conflicts.st_hint.__globals__ is vars(git_common)`, plus several tests
monkeypatch `git_common.install_dir` and read the effect back through
`git_common.st_hint` -- both of which a re-export from this module's globals
would break. The duplication of the three-state logic below is that trade-off,
not an oversight; see `tests/test_supertool_hint_register_905.py`'s own
"DEFINITION" entries for both.
"""
from __future__ import annotations

import os
import shlex
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def install_dir() -> str:
    """Directory holding the `supertool` wrapper and `supertool.py`.

    One level above this file -- `presets/_st_hint.py` sits directly inside
    `presets/`, so its own parent is the repo (or install) root. Named rather
    than inlined because every printed remedy depends on it and a test has to
    be able to stand a fake install somewhere else.
    """
    return os.path.dirname(_HERE)


def _quoted_interpreter() -> str:
    """`sys.executable`, quoted for the shell that will receive it if it must be.

    An interpreter path with a space in it is the ordinary Windows install --
    `C:\\Program Files\\Python312\\python.exe` -- and a POSIX box gets one from
    any user whose home has a space. Unquoted, the hint asks the shell to run
    a program named `C:\\Program` and the remedy fails for the same reason
    #1017 filed: a printed command that is wrong about where it will be
    pasted.

    Double quotes on Windows because they are the only form both `cmd.exe`
    and PowerShell honour; `shlex.quote` elsewhere because it is POSIX's own
    answer.
    """
    exe = sys.executable
    if " " not in exe:
        return exe
    return '"' + exe + '"' if os.name == "nt" else shlex.quote(exe)


def st_hint(*args: str) -> str:
    """A runnable supertool invocation for `args`, for printed remedies.

    Variadic (#905) because one printed remedy chains two ops in a single
    call -- `repo:OWNER/NAME` ahead of the op it scopes -- and that is one
    invocation, not two hints concatenated with no shared route between them.
    A single-op caller passes one string, unchanged from before.
    """
    root = install_dir()
    quoted = " ".join(chr(39) + a + chr(39) for a in args)
    wrapper = os.path.join(root, "supertool")
    if os.path.isfile(wrapper) and os.access(wrapper, os.X_OK):
        return "./supertool " + quoted
    if os.path.isfile(os.path.join(root, "supertool.py")):
        return _quoted_interpreter() + " supertool.py " + quoted
    return ("(no runnable supertool found in " + root + " -- the op"
            + ("s are " if len(args) > 1 else " is ") + quoted + ")")
