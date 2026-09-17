"""Every MCP daemon warm-launch resolver's bare-name PATH lookup must
route through `which_excluding_cwd()`, not raw `shutil.which()` (#2581).

`resolve_bin()` in each of `validators/phpstan-mcp/phpstan-mcp.py`,
`validators/phpunit-mcp/phpunit-mcp.py`, `validators/phpmd-mcp/phpmd-mcp.py`
and `validators/rector-mcp/rector-mcp.py` resolves the daemon binary's
location and reuses that resolved answer directly as the path the daemon is
spawned from -- there is no separate gate-then-spawn pair here the way there
is in an ordinary validator adapter, because the resolution result IS the
spawn target. That makes these files a third, structurally distinct instance
of the #2575/#2579 class from the one `tests/test_gate_matches_spawn_2579.py`
already registers: that register only flags an adapter that calls raw
`shutil.which()` for its gate *while also* importing `argv0`/`spawnable`/
`resolve_bin_cmd` for its actual spawn -- these four files, before this fix,
called raw `which()` (via `from shutil import which`) and nothing else, so
there was no separate chokepoint-routed spawn call to be inconsistent with,
and #2579's own register is correctly blind to them by construction. A repo-
planted binary at these tools' expected bare name would still have been
resolved via cwd-insertion on Windows and handed straight to the daemon
spawner.

This is the register that keeps THIS instance closed: none of the four may
call `shutil.which(...)` or a bare `which(...)` imported from `shutil`.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MCP_DAEMON_FILES = (
    "validators/phpstan-mcp/phpstan-mcp.py",
    "validators/phpunit-mcp/phpunit-mcp.py",
    "validators/phpmd-mcp/phpmd-mcp.py",
    "validators/rector-mcp/rector-mcp.py",
)


def _which_calls(tree: ast.AST) -> "list[int]":
    """Line numbers of every call this file makes that resolves to `which`,
    whether spelled `shutil.which(...)` or a bare `which(...)` reached via
    `from shutil import which`.
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name == "which":
            out.append(node.lineno)
    return out


def test_no_mcp_daemon_resolver_uses_raw_which() -> None:
    offenders = []
    for rel in MCP_DAEMON_FILES:
        path = ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line in _which_calls(tree):
            offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "these MCP daemon resolvers reuse a raw which() answer as the "
        "actual spawn target for the daemon binary -- on Windows that "
        "resolves a repo-planted shim via the cwd-insertion shutil.which() "
        "performs (#2575/#2581). Route through spawnable.which_excluding_cwd() "
        "instead:\n  " + "\n  ".join(offenders)
    )


def test_the_register_can_actually_see_the_defect() -> None:
    """Positive control, same shape as the other #2540/#2579/#2581 registers.

    Without it the assertion above also passes when the walker is broken
    or the file list is wrong.
    """
    bad = "from shutil import which\nresolved = which(name)\n"
    tree = ast.parse(bad)
    assert _which_calls(tree) == [2], "the which() walker is blind"


def test_the_register_covers_a_population_it_can_name() -> None:
    """A register over files that do not exist is green and means nothing."""
    for rel in MCP_DAEMON_FILES:
        assert (ROOT / rel).is_file(), f"{rel} not found -- the file list is stale"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
