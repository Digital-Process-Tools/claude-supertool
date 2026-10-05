#!/usr/bin/env python3










































from __future__ import annotations

import os
import shlex
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _wrapper_is_runnable(path: str) -> bool:




















    if os.name == "nt":
        try:
            with open(path, "rb") as fh:
                return fh.read(2) == b"#!"
        except OSError:
            return False
    return os.access(path, os.X_OK)


def install_dir() -> str:







    return os.path.dirname(_HERE)


def _quoted_interpreter() -> str:













    exe = sys.executable
    if " " not in exe:
        return exe
    return '"' + exe + '"' if os.name == "nt" else shlex.quote(exe)


def st_hint(*args: str) -> str:







    root = install_dir()
    quoted = " ".join(chr(39) + a + chr(39) for a in args)
    wrapper = os.path.join(root, "supertool")
    if os.path.isfile(wrapper) and _wrapper_is_runnable(wrapper):
        return "./supertool " + quoted
    if os.path.isfile(os.path.join(root, "supertool.py")):
        return _quoted_interpreter() + " supertool.py " + quoted
    return ("(no runnable supertool found in " + root + " -- the op"
            + ("s are " if len(args) > 1 else " is ") + quoted + ")")
