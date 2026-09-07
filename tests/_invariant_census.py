"""#2360 -- which test files in this suite are tree-walking guards over this
repository's own source or config whose answer cannot vary by OS or
interpreter, the way `claude-oss`'s own `invariant` marker deselects 126 of
its 428 test files on 11 of its 12 legs (its #1176).

Two predicates, kept separate on purpose rather than folded into one AST
rule, because they are not the same question -- #2360's own worked example:
`test_symlink_capability_1143.py`, `test_git_shim_subcommand_1206.py` and
`test_no_bare_python3_spawn.py` all walk this repository's tree, and all
three are still about *platform behaviour*. Deselecting them on Windows, or
on the 3.9 floor, would quietly narrow the coverage this matrix exists to
keep -- silently, since a deselected test reports nothing, it does not fail.

1. `_walks_tree` -- mechanical, via AST. Does the module call `.rglob(`,
   `.glob(`, or `os.walk(` anywhere, or import a name from `_repo_walk`
   (the shared tree-walk `tests/_repo_walk.py`'s own docstring says #555/#575
   consolidated three drifting copies into, itself built on `Path.rglob`)? A
   module that answers no is not a candidate at all: it is not reading this
   repository's tree, so nothing here applies to it.

2. `_PLATFORM_SIGNAL` -- a named-token scan of the module's source text for
   the vocabulary this repo's own platform-sensitive guards already use:
   `sys.platform`, `os.name`, `platform.system(`, a symlink-privilege check,
   a Windows error code, `shutil.which`, the literal `python3` (#529/#564's
   own class), a git-shim reference, a POSIX-only marker. This is the same
   shape as `PLATFORM_TOKENS` in `test_symlink_gating_register_1232.py`,
   generalised from "skipped on this platform" to "asserts something that can
   differ by platform" -- #2360's three named files are not
   `@requires_symlink` sites, they are files whose *assertions* are about a
   platform difference even though the walk that feeds them is not.

   **This is a heuristic, not a proof, and the direction of its error matters
   more than its precision.** A file this signal misses would be marked
   `invariant` and silently narrowed off Windows and the other interpreters --
   the expensive direction. A false positive only keeps a file in the
   12-leg-run population it would have been in anyway -- the safe direction,
   the same "wider than needed, never narrower" argument `tests/_repo_walk.py`
   makes for its own machine-state exclusions. New platform-dependent
   vocabulary this scan does not yet know about is the residual risk; the
   test below pins the three files #2360 named so that risk cannot regress
   unnoticed for at least those three.

Population, not registry: recomputed from the AST on every run, the same
argument `test_symlink_gating_register_1232.py` and
`test_lint_budget_gating_1360.py` make for their own populations -- a new
tree-walking test file joins, or is excluded from, the `invariant` marker
without anyone remembering to list it by hand.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent

_PLATFORM_SIGNAL = re.compile(
    r"sys\.platform|platform\.system\(|os\.name|symlink|winerror|WinError|"
    r"posix_only|require_symlink|windows_has_no_usable_bash|codepage|cp1252|"
    r"UnicodeEncodeError|shutil\.which|python3|App Execution Alias|"
    r"_shim|geteuid|O_NOFOLLOW|AF_UNIX|SeCreateSymbolicLinkPrivilege|"
    r"win32|is_windows|IS_WINDOWS",
    re.IGNORECASE,
)


def _walks_tree(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("rglob", "glob"):
                return True
            if (isinstance(func, ast.Attribute) and func.attr == "walk"
                    and isinstance(func.value, ast.Name) and func.value.id == "os"):
                return True
        if isinstance(node, ast.ImportFrom) and node.module == "_repo_walk":
            return True
    return False


def population() -> dict:
    """``{file name: platform_sensitive}`` for every tree-walking test file
    under this directory.

    ``platform_sensitive=True`` means `_PLATFORM_SIGNAL` found a reason NOT
    to mark the file `invariant`, even though it walks the tree.
    """
    found = {}
    for path in sorted(TESTS.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        if not _walks_tree(tree):
            continue
        found[path.name] = bool(_PLATFORM_SIGNAL.search(source))
    return found


def invariant_files() -> frozenset:
    """File names whose answer cannot vary by OS or interpreter -- the
    population `tests/conftest.py` marks `pytest.mark.invariant` at
    collection time.
    """
    return frozenset(name for name, flagged in population().items() if not flagged)
