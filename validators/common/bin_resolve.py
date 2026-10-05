































from __future__ import annotations

import os
import pathlib
import shlex
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from spawnable import _cwd_only_match, _refuse_spawn, which_excluding_cwd  


def _is_executable(path: str) -> bool:










    has_dir = bool(os.path.dirname(path))
    return bool(which_excluding_cwd(path)) or (
        has_dir and os.path.isfile(path) and os.access(path, os.X_OK)
    )


def _spawnable(name: str) -> str:





































    if os.path.dirname(name) and os.path.isfile(name) and os.access(name, os.X_OK):
        return name
    resolved = which_excluding_cwd(name)
    if resolved is not None:
        return resolved
    if _cwd_only_match(name):
        return _refuse_spawn(name)
    return name


def resolve_bin_cmd(raw: str, default: str) -> list[str]:






    if not raw:
        return [default]






    candidate = raw.replace("\\", "/") if os.name == "nt" else raw

    if _is_executable(candidate):
        return [_spawnable(candidate)]

    parts = shlex.split(candidate, posix=True)
    if parts:
        parts[0] = _spawnable(parts[0])
        return parts
    return [default]


def describe_unresolved(raw: str, resolved: str) -> str:










    if not raw or resolved == raw:
        return resolved



    return f'{resolved} (configured: "{raw}")'

