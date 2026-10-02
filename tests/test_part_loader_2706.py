"""`_load_part` (#2706): the mechanics of a core split into parts.

#2706 is "_supertool.py is 1.7 MB, over the plugin directory's 256 KiB
per-file rule". The Decision comment on that issue chose `exec(code,
globals())` over a real `import` for the parts still to come out of the
core, specifically because 1,301 test-patch sites and 30 globals rebound with
`global X` across what would become module boundaries would otherwise break
silently, with no test built to catch it. The guard (#2706 lane 0) is the
pilot part: 0 patch sites, picked so the mechanism could be built and proven
before a part with real patch sites has to ride on top of it.

This file pins the mechanism itself, independent of which part is loaded:
namespace honesty, the standalone-import refusal, the part-header shape, the
bytecode cache (#931) still applying to a part, a missing part failing loudly
rather than with a later NameError, and that two parts (or a part and the
core) cannot silently define the same top-level name twice.
"""
from __future__ import annotations

import ast
import importlib
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _core_sources import core_source_paths, part_names_in_load_order  # noqa: E402

import supertool  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Namespace honesty
# ---------------------------------------------------------------------------

def test_every_function_defined_in_a_part_shares_the_core_globals():
    """A part's functions must have `__globals__ is _supertool.__dict__`.

    This is the property the whole mechanism exists to keep: a real `import`
    would give the part its own globals() and silently break every
    `monkeypatch.setattr(supertool, "<name>", ...)` that reaches code living
    there. Checked by `co_filename`, not by name: a function whose compiled
    code object says it came from a part file is in scope, whatever it is
    called.
    """
    part_paths = {str(p) for p in core_source_paths() if p.name != "_supertool.py"}
    assert part_paths, "no parts loaded -- this test would vacuously pass"
    checked = 0
    for name in dir(supertool):
        obj = getattr(supertool, name)
        code = getattr(obj, "__code__", None)
        if code is None or code.co_filename not in part_paths:
            continue
        checked += 1
        assert obj.__globals__ is supertool.__dict__, (
            f"supertool.{name} is defined in a part ({code.co_filename}) but "
            f"its __globals__ is not _supertool's own dict -- the part was "
            f"imported rather than exec'd into this module's namespace, so "
            f"patches aimed at supertool.{name} no longer reach it")
    assert checked > 0, "no part-defined function found on supertool at all"


def test_monkeypatching_a_part_defined_name_changes_real_behaviour():
    """Namespace honesty, proven by actually patching one (not just inspected).

    `guard_command_words` is defined in `_supertool_guard.py` and called by
    `guard_command` (also in that file) by plain name lookup -- which only
    resolves through the patch if both share one globals() dict.
    """
    original = supertool.guard_command_words
    sentinel = {("sentinel-command",)}
    try:
        supertool.guard_command_words = lambda: sentinel
        assert supertool.guard_command_words() == sentinel
        # guard_command calls guard_command_words() by bare name from inside
        # _supertool_guard.py -- if that file had its own globals(), this
        # patch would be invisible to it and the line below would still see
        # the ORIGINAL registry.
        result = supertool.guard_command("totally-not-a-real-command-xyz")
        assert result is not None
    finally:
        supertool.guard_command_words = original


# ---------------------------------------------------------------------------
# Part header guard
# ---------------------------------------------------------------------------

def test_every_part_refuses_a_standalone_import():
    """`import _supertool_guard` (or any future part) must raise ImportError.

    A part assumes names _supertool.py already defined (Dict, Any, os, re,
    shlex, the `_load_part` marker itself); imported on its own it would fail
    with a NameError on the first one it reaches, which names an interpreter
    builtin rather than the real problem. The header guard turns that into
    one clear, immediate ImportError instead.
    """
    for name in part_names_in_load_order():
        proc = subprocess.run(
            [sys.executable, "-c", f"import {name}"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        assert proc.returncode != 0, f"{name} imported standalone without error"
        assert "cannot be imported directly" in proc.stderr, (
            f"{name} failed standalone import, but not with the part's own "
            f"clear message -- got:" + chr(10) + proc.stderr)


def test_every_part_has_the_required_header_shape():
    """Docstring, `from __future__ import annotations`, then the `_load_part`
    marker check, in that order, before any other top-level code -- the
    shape every part must have for the standalone-import refusal above to
    actually fire before anything else runs.
    """
    for path in core_source_paths():
        if path.name == "_supertool.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        body = tree.body
        assert body, f"{path.name}: empty part"
        idx = 0
        if (isinstance(body[idx], ast.Expr)
                and isinstance(body[idx].value, ast.Constant)
                and isinstance(body[idx].value.value, str)):
            idx += 1
        assert idx < len(body) and isinstance(body[idx], ast.ImportFrom) and (
            body[idx].module == "__future__"
            and any(a.name == "annotations" for a in body[idx].names)
        ), f"{path.name}: must open with `from __future__ import annotations`"
        idx += 1
        guard = body[idx] if idx < len(body) else None
        assert isinstance(guard, ast.If), (
            f"{path.name}: must check for `_load_part` in globals() before "
            f"any other top-level code")
        raises = any(isinstance(n, ast.Raise) for n in ast.walk(guard))
        assert raises, f"{path.name}: the _load_part guard must raise"


# ---------------------------------------------------------------------------
# A missing part fails loudly, never with a later NameError
# ---------------------------------------------------------------------------

def test_a_missing_part_file_raises_the_2706_message_not_a_nameerror(tmp_path):
    for src in core_source_paths():
        if src.name == "_supertool.py":
            (tmp_path / src.name).write_bytes(src.read_bytes())
    # every part deliberately absent
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "_supertool.py"), "version"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode != 0
    assert "incomplete install" in proc.stderr and "#2706" in proc.stderr, (
        "expected the #2706 incomplete-install message, got:" + chr(10) + proc.stderr)
    assert "NameError" not in proc.stderr, (
        "a missing part must fail at the _load_part() call site, not later "
        "with a NameError on the first name the missing code was supposed "
        "to define:" + chr(10) + proc.stderr)


# ---------------------------------------------------------------------------
# No duplicate top-level name across core + parts
# ---------------------------------------------------------------------------

def _top_level_names(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = node.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.lineno
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                out[node.target.id] = node.lineno
    return out


def test_no_top_level_name_is_defined_in_two_places():
    """A part is exec'd into the core's own globals() -- a name the core (or
    an earlier part) already defines would be silently shadowed, in whichever
    direction load order runs, with no error from Python at all."""
    seen = {}
    collisions = []
    for path in core_source_paths():
        for name, lineno in _top_level_names(path).items():
            if name in seen:
                collisions.append(
                    f"{name!r}: {seen[name]} and {path.name}:{lineno}")
            else:
                seen[name] = f"{path.name}:{lineno}"
    assert not collisions, "defined in more than one place:" + chr(10) + chr(10).join(collisions)


# ---------------------------------------------------------------------------
# Declared parts/modules census
# ---------------------------------------------------------------------------

#: `_supertool_vim.py` is a real, separately-importable module (#2711),
#: loaded lazily via a plain `import _supertool_vim` -- not through
#: `_load_part`, and deliberately not returned by `core_source_paths()` (see
#: that module's own docstring). Every OTHER root `_supertool_*.py` must
#: either be named by a `_load_part(...)` call, or be declared here as a real
#: module -- so a new root file that is neither gets caught rather than
#: silently shipping unreferenced or, worse, unshipped.
DECLARED_REAL_MODULES = frozenset({"_supertool_vim"})


def test_every_root_supertool_underscore_file_is_accounted_for():
    on_disk = {p.stem for p in ROOT.glob("_supertool_*.py")}
    parts = set(part_names_in_load_order())
    unaccounted = on_disk - parts - DECLARED_REAL_MODULES
    assert not unaccounted, (
        f"{sorted(unaccounted)} exist as root _supertool_*.py files but are "
        f"neither loaded by _load_part() nor declared in "
        f"DECLARED_REAL_MODULES above -- declare which one it is")


# ---------------------------------------------------------------------------
# Size ceilings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", list(Path(__file__).resolve().parent.parent.glob("_supertool_*.py")))
def test_a_part_stays_under_the_256kib_hard_ceiling(path: Path):
    """#2706's whole reason to exist: the plugin directory's 256 KiB per-file
    limit. A part over that limit defeats the split that was supposed to fix
    it."""
    size = path.stat().st_size
    assert size < 256 * 1024, (
        f"{path.name} is {size} bytes, over the 256 KiB hard ceiling #2706 "
        f"exists to get every file under")


# ---------------------------------------------------------------------------
# #931 bytecode cache still applies to a part
# ---------------------------------------------------------------------------

def test_loading_a_part_writes_and_reuses_a_pyc(tmp_path):
    part_names = part_names_in_load_order()
    assert part_names
    name = part_names[0]
    src = ROOT / f"{name}.py"
    path = tmp_path / f"{name}.py"
    path.write_bytes(src.read_bytes())

    cache = importlib.util.cache_from_source(str(path))
    assert not Path(cache).exists()
    importlib.machinery.SourceFileLoader(name, str(path)).get_code(name)
    assert Path(cache).exists(), "no __pycache__ entry written for the part"
    before = Path(cache).read_bytes()

    # Reused, not recompiled -- proven cross-platform via the loader's own
    # cache-validity check (mtime + size), not via chmod(0o000): on Windows,
    # chmod only toggles the read-only attribute and leaves a file readable
    # (CPython's own documented `os.chmod` behaviour), so an unreadable-file
    # negative control is silently vacuous on that platform while still
    # reporting green. Corrupting the source to invalid syntax, while
    # restoring its exact original mtime and size, makes the two checks that
    # actually decide reuse agree with the stale cache on every OS: if
    # get_code() ever fell back to re-reading and recompiling this source,
    # the SyntaxError below would fire immediately rather than silently
    # succeeding.
    stat = path.stat()
    broken = (b"x" * (stat.st_size - 1)) + b"\n"
    assert len(broken) == stat.st_size, "corrupted source must keep the same size"
    path.write_bytes(broken)
    os.utime(path, (stat.st_atime, stat.st_mtime))

    importlib.machinery.SourceFileLoader(name, str(path)).get_code(name)
    after = Path(cache).read_bytes()
    assert before == after, "the cache was rewritten rather than reused"
