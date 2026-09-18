"""An absent-tool gate must ask the same PATH the spawn will use (#2579).

Follow-up from #2575 / PR #2577's own review. #2575 fixed the *spawn* side of
every adapter listed below -- `argv0(name)` and `resolve_bin_cmd(raw, default)`
both route through `spawnable.which_excluding_cwd()`, so a repo-planted
`ruff.cmd` at the project root can no longer be executed. It left the *gate*
side untouched: `validators/ruff/ruff.py:200` (at the time) still asked
`shutil.which(TOOL)` -- the raw, cwd-including form -- before ever reaching
that spawn.

The two calls then search different PATH lists. A repo-planted shim passes
the gate (raw `which()` still finds it via cwd) and then fails to spawn (the
corrected `argv0()`/`resolve_bin_cmd()` refuses it) -- not a security hole
(the fix still holds), but a correctness gap: the gate says "found" and the
spawn says "not found", for a reason a caller debugging a validator failure
has no way to connect without reading both call sites.

This is the register that keeps the class closed, the same shape as
`tests/test_spawnable_register_2540.py`: an adapter whose actual subprocess
spawn already goes through the cwd-excluding chokepoint (`argv0`, `spawnable`,
or `resolve_bin_cmd`) must not gate on a raw `shutil.which()` call instead.

`validators/phpstan/phpstan.py` was the one adapter deliberately left off this
register, tracked separately (#2581): its resolved `phpstan_bin` value is spliced as
a literal script argument to `php`, not spawned as argv[0], so its own gate
had nothing to be inconsistent WITH until it gated on `spawnable()` too --
matching every other `_BIN` adapter's own convention, `spawnable()` there is
a presence-only check and `argv0(phpstan_bin)` builds the actual argv element
separately. Done now, so the `ALLOWLIST` below is empty rather than removed
outright (kept as the register's own escape hatch for the next adapter shaped
this way, not because this one still needs it).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ADAPTER_DIRS = ("validators", "formatters")

#: The chokepoint modules themselves call `shutil.which()` internally (the
#: dirname-branch delegation in `which_excluding_cwd`) -- that is the
#: implementation, not an instance of the defect.
CHOKEPOINT_FILES = {"spawnable.py", "bin_resolve.py"}

#: `phpstan_bin` was spliced into `php`'s argv as a literal script path, with
#: nothing to check it against -- no chokepoint-routed spawn its gate could
#: disagree with. Fixed by #2581 (gated on `spawnable()` too, argv built
#: separately via `argv0()`, same split every other `_BIN` adapter already
#: uses), so nothing needs allowlisting here today; kept empty rather than
#: removed as the register's own escape hatch for the next adapter shaped
#: this way.
ALLOWLIST: set = set()


def _adapter_sources() -> "list[pathlib.Path]":
    out = []
    for d in ADAPTER_DIRS:
        out.extend(sorted((ROOT / d).rglob("*.py")))
    return [p for p in out if p.name not in CHOKEPOINT_FILES]


def _calls_raw_which(tree: ast.AST) -> bool:
    """Does this file call `shutil.which(...)` anywhere?"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name == "which":
            return True
    return False


def _bare_existence_check_without_dirname_guard(tree: ast.AST) -> bool:
    """Does this file check `X.exists()`/`os.path.isfile(X)` alongside
    `os.access(X, os.X_OK)` in the same boolean expression with no
    `os.path.dirname(X)` guard anywhere in that same expression (#2602)?

    This is the second-disjunct shape #2575/#2579/#2581 never touched:
    `if not spawnable(X) and not (Path(X).exists() and os.access(X,
    os.X_OK)):`. A bare, separator-free name checked this way resolves
    relative to the current directory exactly like the raw `which()` call
    the first disjunct was already fixed to stop -- see
    `spawnable._already_a_path`'s own docstring. Scoped to a single
    `and`-chain (`ast.BoolOp`) rather than the whole file: that is the
    actual shape of the gate at every site #2602 fixed, and it is what
    lets the dirname guard and the existence check be told apart once
    both are added to the same expression.
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And)):
            continue
        has_existence = False
        has_dirname = False
        for value in node.values:
            if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)):
                continue
            if value.func.attr in ("exists", "isfile"):
                has_existence = True
            if value.func.attr == "dirname":
                has_dirname = True
        if has_existence and not has_dirname:
            return True
    return False


def _uses_the_chokepoint_at_spawn_time(tree: ast.AST) -> bool:
    """Does this file import the cwd-excluding chokepoint at all?

    `argv0`/`spawnable` (direct import from `spawnable`) or `resolve_bin_cmd`
    (from `bin_resolve`, itself routed through the same guard) -- either one
    means this adapter's actual subprocess spawn is already cwd-safe, so its
    gate is now the only place the old, unsafe `shutil.which()` can survive.
    """
    names = set()
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.asname or alias.name)
    if names & {"argv0", "spawnable", "resolve_bin_cmd"}:
        return True
    # `import spawnable` / `import bin_resolve` (module-style, attribute
    # access at the call site -- `spawnable.argv0(...)`) is a second way to
    # reach the same chokepoint that `from X import Y` does not surface as
    # a bare name. No shipped adapter uses this style today, but the walker
    # must not go blind the day one does (#2579 review).
    if modules & {"spawnable", "bin_resolve"}:
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                    "argv0", "spawnable", "resolve_bin_cmd"):
                return True
    return False


def _offenders() -> "list[str]":
    hits = []
    for path in _adapter_sources():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not _uses_the_chokepoint_at_spawn_time(tree):
            continue
        if _calls_raw_which(tree) or _bare_existence_check_without_dirname_guard(tree):
            hits.append(rel)
    return hits


def test_no_gate_calls_raw_which_when_the_spawn_is_already_cwd_safe() -> None:
    offenders = _offenders()
    assert not offenders, (
        "these adapters spawn through the cwd-excluding chokepoint "
        "(argv0()/spawnable()/resolve_bin_cmd()) but still gate on raw "
        "shutil.which(), or on a bare Path(X).exists()/os.path.isfile(X) "
        "check with no os.path.dirname(X) guard (#2602) -- either one "
        "searches a DIFFERENT path than the spawn (#2579): a repo-planted "
        "shim passes the gate and then either fails to spawn (POSIX) or "
        "is spawned from the current directory (Windows, #2602). Gate on "
        "spawnable(name) and already_a_path(name) instead:\n  " + "\n  ".join(offenders)
    )


def test_the_register_catches_a_bare_existence_gate_with_no_dirname_guard() -> None:
    """Positive control for the #2602 widening.

    This is the exact shape all seven #2602 sites had before the fix:
    a chokepoint-routed spawn (argv0/spawnable), gated on `spawnable()`
    OR a bare `Path(X).exists() and os.access(X, os.X_OK)` check with no
    dirname guard. The old walker (raw `shutil.which()` only) was blind
    to this -- it does not call `which()` at all -- which is exactly why
    #2602 shipped past #2579's own register.
    """
    vulnerable = (
        "import os, pathlib, subprocess\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(TOOL) and not (\n"
        "    pathlib.Path(TOOL).exists() and os.access(TOOL, os.X_OK)\n"
        "):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(vulnerable)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert _bare_existence_check_without_dirname_guard(tree), (
        "the widened walker does not see a bare Path(X).exists() check "
        "with no dirname guard, alongside a chokepoint-routed spawn -- "
        "the exact shape #2602 fixed at seven call sites"
    )
    assert not _calls_raw_which(tree), (
        "this fixture must not also trip the OLD detector -- it is "
        "testing the NEW one in isolation"
    )


def test_a_dirname_guarded_existence_gate_is_not_flagged() -> None:
    """Positive control's mirror: the same shape, fixed, must clear."""
    fixed = (
        "import os, pathlib, subprocess\n"
        "from spawnable import already_a_path, argv0, spawnable\n"
        "if not spawnable(TOOL) and not already_a_path(TOOL):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(fixed)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert not _bare_existence_check_without_dirname_guard(tree), (
        "a gate that calls already_a_path() instead of reimplementing the "
        "check inline must not be flagged -- there is no bare exists()/"
        "isfile() call left in this source to see"
    )
    inline_but_guarded = (
        "import os, pathlib, subprocess\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(TOOL) and not (\n"
        "    os.path.dirname(TOOL) and pathlib.Path(TOOL).exists()\n"
        "    and os.access(TOOL, os.X_OK)\n"
        "):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree2 = ast.parse(inline_but_guarded)
    assert not _bare_existence_check_without_dirname_guard(tree2), (
        "an inline existence check that DOES carry a dirname guard in "
        "the same and-chain must not be flagged"
    )


def test_the_register_can_actually_see_the_defect() -> None:
    """Positive control, same shape as #2540's own register.

    Without this, the assertion above also passes when the walker is
    broken, matches nothing, or is pointed at an empty tree -- which is
    this repository's recurring defect class wearing a test's clothes.
    """
    bad = (
        "import shutil, subprocess\n"
        "from spawnable import argv0\n"
        "if not shutil.which(TOOL):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(bad)
    assert _calls_raw_which(tree), "the shutil.which() walker is blind"
    assert _uses_the_chokepoint_at_spawn_time(tree), (
        "the chokepoint-import walker is blind"
    )


def test_a_module_style_chokepoint_import_is_still_seen() -> None:
    """`import spawnable` + `spawnable.argv0(...)` is a second way to reach
    the chokepoint that `from spawnable import argv0` does not surface as a
    bare name (auditor review, #2579). No shipped adapter is written this
    way today, but the walker must not go blind if one ever is.
    """
    bad = (
        "import shutil, subprocess, spawnable\n"
        "if not shutil.which(TOOL):\n"
        "    absent()\n"
        "cmd = [spawnable.argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(bad)
    assert _calls_raw_which(tree)
    assert _uses_the_chokepoint_at_spawn_time(tree), (
        "the module-style import walker is blind"
    )


def test_a_gate_with_no_chokepoint_spawn_is_not_flagged() -> None:
    """Negative control: an adapter with no argv0/spawnable/resolve_bin_cmd
    import at all -- phpstan's own shape before #2581 fixed it, kept here
    as a synthetic snippet now that the real file no longer matches it --
    must not be flagged just for calling `shutil.which()`: there is
    nothing for its gate to disagree with.
    """
    fine = (
        "import shutil, subprocess, os\n"
        "if not shutil.which(TOOL) and not (os.path.dirname(TOOL) and Path(TOOL).exists()):\n"
        "    absent()\n"
        "cmd = [\"php\", TOOL, \"analyse\"]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(fine)
    assert _calls_raw_which(tree)
    assert not _uses_the_chokepoint_at_spawn_time(tree), (
        "the negative control itself imports the chokepoint -- it is not "
        "testing what it claims to"
    )
    assert not _bare_existence_check_without_dirname_guard(tree), (
        "the negative control's own existence check has a dirname guard "
        "(os.path.dirname(TOOL)) -- it must not be flagged by the #2602 "
        "detector either. Before this fix the fixture was the literal "
        "vulnerable shape (Path(TOOL).exists(), no dirname guard) and "
        "this test still called it \"fine\" -- correct for the narrow "
        "claim it was making (no chokepoint import) but a fixture that "
        "IS the exact defect #2602 reports should not be the one this "
        "register holds up as an example of a clean gate."
    )


def test_the_register_covers_a_population_it_can_name() -> None:
    """A register over zero files is green and means nothing."""
    sources = _adapter_sources()
    assert len(sources) >= 40, (
        f"only {len(sources)} adapter sources found under "
        f"{ADAPTER_DIRS} -- the walk root is wrong, and an empty walk "
        "reads exactly like a clean one"
    )
    with_chokepoint = [
        p for p in sources
        if _uses_the_chokepoint_at_spawn_time(ast.parse(p.read_text(encoding="utf-8")))
    ]
    assert with_chokepoint, (
        "no adapter imports argv0/spawnable/resolve_bin_cmd at all, so this "
        "register is asserting nothing about anything"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
