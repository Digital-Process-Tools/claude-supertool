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

`validators/phpstan/phpstan.py` is the one adapter deliberately left off this
register (see the module-level `ALLOWLIST` below): its resolved `_BIN` value
is never passed through `argv0()`/`spawnable()`/`resolve_bin_cmd()` at all --
it is spliced as a literal script argument to `php`, not spawned as argv[0] --
so fixing its gate alone would not make it consistent with anything, and
migrating it properly is a separate, larger change than this register's scope.
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

#: `phpstan_bin` is spliced into `php`'s argv as a literal script path, never
#: resolved through `argv0()`/`spawnable()`/`resolve_bin_cmd()` -- there is no
#: chokepoint-routed spawn for its gate to be inconsistent WITH. Tracked
#: separately (see the module docstring); fixing its gate alone would not
#: close anything.
ALLOWLIST = {"validators/phpstan/phpstan.py"}


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
        if _calls_raw_which(tree) and _uses_the_chokepoint_at_spawn_time(tree):
            hits.append(rel)
    return hits


def test_no_gate_calls_raw_which_when_the_spawn_is_already_cwd_safe() -> None:
    offenders = _offenders()
    assert not offenders, (
        "these adapters spawn through the cwd-excluding chokepoint "
        "(argv0()/spawnable()/resolve_bin_cmd()) but still gate on raw "
        "shutil.which(), which searches a DIFFERENT PATH list (#2579): a "
        "repo-planted shim passes the gate and then fails to spawn, "
        "reported as a tool that vanished between the two checks rather "
        "than a tool that was never really findable at all. Gate on "
        "spawnable(name) instead:\n  " + "\n  ".join(offenders)
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
    import at all (phpstan's actual shape) must not be flagged just for
    calling `shutil.which()` -- there is nothing for its gate to disagree
    with yet.
    """
    fine = (
        "import shutil, subprocess\n"
        "if not shutil.which(TOOL) and not (Path(TOOL).exists()):\n"
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
