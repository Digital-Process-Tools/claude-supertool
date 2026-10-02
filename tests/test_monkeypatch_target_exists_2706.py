"""#2706 — a monkeypatch/mock.patch aimed at a name that moved out of
_supertool.py must fail loudly, not test a decoy.

The vim split (#2706 step 1) moved ~19 functions out of _supertool.py into
_supertool_vim.py. Nothing re-exports a moved name back into _supertool's own
namespace, so `monkeypatch.setattr(supertool, "_vim_save_state", fake)` raises
AttributeError today -- loud, correct, easy to fix by migrating the patch
site. The failure mode this guards against is the quiet one: a future refactor
that re-exports a moved name (`from _supertool_vim import X` at the top of
_supertool.py, say, for a soft back-compat shim) would make that same
monkeypatch.setattr call succeed again -- green, but patching an attribute
the real call site no longer reads, which is a test that passes while
asserting nothing.

So the check is not "does the attribute exist" (an AttributeError already
catches that) -- it is "was this name ever actually DEFINED at module level in
_supertool.py" (a def/class/assignment in its own source), as opposed to
merely being importABLE into it. A name that moved away and was never
re-exported fails both tests; a name re-exported without being redefined
fails only this one, which is exactly the gap an AttributeError cannot see.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERTOOL_MODULES = ("supertool", "_supertool")


def _defined_in_supertool() -> set:
    """Names bound by a def/class/assignment at _supertool.py's own module
    level -- NOT names merely imported into it (`import os`, `import re`),
    which is the exact distinction #2706's own brief draws: an imported name
    sitting on the module is not the same claim as a name _supertool.py
    defines, and only the latter is what a monkeypatch of a *moved* name is
    supposed to mean.
    """
    src = (REPO_ROOT / "_supertool.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="_supertool.py")
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _patch_targets(test_dir: Path):
    """Yield (path, lineno, name) for every monkeypatch.setattr(supertool_or_
    _supertool, "NAME", ...) and mock.patch("supertool.NAME"/"_supertool.NAME")
    call across every test file -- AST-based (not a text grep) so a call
    split across lines, or using single vs. double quotes, is still seen.
    """
    for path in sorted(test_dir.glob("test_*.py")):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # monkeypatch.setattr(supertool, "NAME", ...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in SUPERTOOL_MODULES
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                yield path, node.lineno, node.args[1].value
                continue
            # mock.patch("supertool.NAME") / patch("_supertool.NAME")
            if (
                isinstance(func, ast.Attribute) and func.attr == "patch"
            ) or (isinstance(func, ast.Name) and func.id == "patch"):
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    continue
                target = node.args[0].value
                if not isinstance(target, str):
                    continue
                for mod in SUPERTOOL_MODULES:
                    prefix = mod + "."
                    if target.startswith(prefix):
                        yield path, node.lineno, target[len(prefix):].split(".")[0]
                        break


def test_every_patched_supertool_name_is_still_defined_there():
    defined = _defined_in_supertool()
    bad = []
    for path, lineno, name in _patch_targets(REPO_ROOT / "tests"):
        if name not in defined:
            bad.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: patches "
                        f"supertool.{name!r}, which is not defined at module "
                        f"level in _supertool.py (moved, renamed, or removed)")
    assert not bad, (
        "monkeypatch/mock.patch target(s) no longer defined in _supertool.py "
        "-- migrate the patch site to wherever the name actually lives now:\n"
        + "\n".join(bad)
    )


def test_the_census_itself_is_not_vacuous():
    """A population of zero would let the assertion above pass for the wrong
    reason -- nothing to check, not everything checked out. This repo's own
    suite patches dozens of supertool names today, so the walk must find a
    non-trivial number of them or the AST matching above has silently broken."""
    count = sum(1 for _ in _patch_targets(REPO_ROOT / "tests"))
    assert count > 50, f"expected a substantial population, found {count}"
