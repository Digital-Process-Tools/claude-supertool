






































from __future__ import annotations

import os
import shutil
import uuid


def which_excluding_cwd(name: str) -> "str | None":





























    if os.path.dirname(name):
        return shutil.which(name)
    path_env = os.environ.get("PATH")
    if path_env is None:



        try:
            path_env = os.confstr("CS_PATH")
        except (AttributeError, ValueError):
            path_env = os.defpath
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
















    return which_excluding_cwd(name)


def _already_a_path(name: str) -> bool:

























    return bool(os.path.dirname(name)) and os.path.isfile(name) and os.access(name, os.X_OK)


def already_a_path(name: str) -> bool:










    return _already_a_path(name)


def _cwd_only_match(name: str) -> bool:


















    if os.path.dirname(name):
        return False
    return which_excluding_cwd(name) is None and shutil.which(name) is not None


def _refuse_spawn(name: str) -> str:






























    return os.path.join(os.sep, "supertool-2578-refused-" + uuid.uuid4().hex, name)


def argv0(name: str) -> str:




























    if _already_a_path(name):
        return name
    resolved = which_excluding_cwd(name)
    if resolved is not None:
        return resolved
    if _cwd_only_match(name):
        return _refuse_spawn(name)
    return name
