"""Core + parts, in load order (#2706).

`_supertool.py` increasingly delegates contiguous chunks of itself to
`_supertool_<x>.py` *part* files, each loaded by `_load_part("_supertool_<x>")`
-- a plain `exec(code, globals())` at the exact source position the moved code
used to occupy, not a real import (see the "Decision: the remaining core
splits as parts sharing one namespace" comment on #2706). Every function
defined inside a part still has `__globals__ is _supertool.__dict__`, so every
existing `monkeypatch.setattr(supertool, "<name>", ...)` keeps reaching the
code it patches -- but a test that scans "the core" by hardcoding
`_supertool.py` as the one file now sees only what has not yet moved out of
it, silently.

This module derives the current list by ast-walking the core file for
`_load_part(...)` call sites, in the order they appear -- source order is
load order, since `_load_part` executes each part at the exact position the
call occupies. A test that needs "all of the core's source" imports
`core_source_paths()` or `core_source_text()` from here instead of hardcoding
`_supertool.py`, so the list tracks the split instead of rotting with it.

A `_supertool_<x>.py` file that exists on disk but is never named by a
`_load_part(...)` call in the core is NOT a part by this module's own
definition -- that is exactly the shape `_supertool_vim.py` has (a real,
separately-`import`-able module, loaded lazily via a plain `import
_supertool_vim` inside `op_vim`'s stub, never via `_load_part`). This module
intentionally reports only `_load_part`-loaded files; a caller that also wants
real sibling modules composes its own list.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "_supertool.py"


def part_names_in_load_order(core_source: Optional[str] = None) -> List[str]:
    """`_load_part("name")` call sites in `_supertool.py`, in source order.

    Only a literal string first argument is recognised -- `_load_part` is
    never called any other way in this codebase, and recognising only the
    literal form means a future indirection is reported as a miss (an empty
    or short list) rather than silently resolved incorrectly.
    """
    source = core_source if core_source is not None else CORE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_load_part"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            hits.append((node.lineno, node.args[0].value))
    hits.sort(key=lambda pair: pair[0])
    return [name for _, name in hits]


def core_source_paths() -> List[Path]:
    """Absolute paths of `_supertool.py` and every part it loads, in load order."""
    return [CORE] + [ROOT / (name + ".py") for name in part_names_in_load_order()]


def core_source_texts() -> List[str]:
    """Source text of `_supertool.py` and every part, in load order, as a list.

    Kept separate from a single joined string so a caller doing a per-file
    scan (and wanting to report which *file* an offending line lives in)
    does not have to re-split a concatenation.
    """
    return [p.read_text(encoding="utf-8") for p in core_source_paths()]


def core_source_text() -> str:
    """`_supertool.py` and every part it loads, concatenated in load order.

    For a scan that only cares about "is this pattern present anywhere in the
    core", not about which file. Joined with a blank line so a regex anchored
    at start-of-line near a file boundary still sees a real line break.
    """
    newline = chr(10)
    return newline.join(core_source_texts())
