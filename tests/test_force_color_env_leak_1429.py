"""An operator's ambient FORCE_COLOR must never reach a spawned child (#1429).

CPython 3.13+ colourises its own tracebacks purely because `FORCE_COLOR` is
set in the environment -- even when stderr is a pipe -- so any child this
process spawns hands back ANSI escapes baked into whatever field captures its
output. The confirmed instance (`_mcp_stop_server`'s `detail`, #574/#1333)
already strips escapes on the way IN; this pins the other fix the issue asked
to choose between -- the child never sees the variable in the first place,
so nothing downstream has to guess which bytes were decoration.

Only `FORCE_COLOR` is removed, never `NO_COLOR` forced (self-review finding):
CPython's own `_colorize.can_colorize()` checks `NO_COLOR` BEFORE
`FORCE_COLOR`, so forcing `NO_COLOR=1` here would silently out-rank a
declared preset's own `.supertool.json` `env: {FORCE_COLOR: ...}` -- an
operator who deliberately wants colour on one command would lose it with no
way back. `test_a_declared_env_override_still_wins` pins that this stays
live.

The first two tests are version-independent: they never depend on CPython's
own colourising machinery, only on whether the child's environment carries
`FORCE_COLOR`, so they mean the same thing on every CI leg (3.9-3.12) as on
the 3.13+ interpreter that surfaced the issue. The last one reproduces the
issue's own repro verbatim and is skipped below 3.13, where CPython does not
colourise a piped traceback at all -- there the assertion would be vacuous
rather than a real negative (see CLAUDE.md's cross-platform audit note on
that trap).
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

import supertool


def test_disable_force_color_pops_the_var_only(monkeypatch) -> None:
    """The extracted helper is the whole mechanism -- test it directly.

    `NO_COLOR` is untouched either way: not set if absent, not cleared if an
    operator already had it set for their own reasons.
    """
    monkeypatch.setenv("FORCE_COLOR", "3")
    monkeypatch.delenv("NO_COLOR", raising=False)

    supertool._disable_force_color_for_children()

    assert "FORCE_COLOR" not in os.environ
    assert "NO_COLOR" not in os.environ


_PROBE_LINES = [
    "import os, sys",
    "sys.stdout.write('FORCE_COLOR=' + os.environ.get('FORCE_COLOR', '<unset>'))",
    "sys.stdout.write(chr(10))",
]


def test_a_spawned_child_never_sees_force_color(monkeypatch) -> None:
    """Real subprocess, real (inherited) env -- not a mock of the mechanism.

    Red before the fix: an operator's `FORCE_COLOR=3` (this repo's own agent
    harness exports exactly that, per CLAUDE.md) is plain ambient environment
    and Python's default `subprocess.run(..., env=None)` inherits it
    unchanged, so the child prints back `FORCE_COLOR=3`. Green after: the
    module-level `_disable_force_color_for_children()` call already popped it
    from `os.environ` before this test's `monkeypatch.setenv` even ran, so
    re-arming it here and re-calling the function (simulating what happens
    once, for real, at process start on an operator's machine) proves the
    child inherits the *scrubbed* environment, not the operator's.
    """
    monkeypatch.setenv("FORCE_COLOR", "3")
    supertool._disable_force_color_for_children()

    result = subprocess.run(
        [sys.executable, "-c", "\n".join(_PROBE_LINES)],
        capture_output=True, text=True, timeout=10,
        encoding="utf-8", errors="replace",
    )

    assert "FORCE_COLOR=<unset>" in result.stdout, result.stdout


def test_a_declared_env_override_still_wins(monkeypatch) -> None:
    """A caller who deliberately re-adds FORCE_COLOR keeps it (self-review).

    Mirrors what a `.supertool.json` preset's own `env:` block does: it is
    applied AFTER the `{**os.environ, ...}` merge that already reflects the
    import-time strip, so a value it sets survives. The first draft of this
    fix also forced `NO_COLOR=1`, which CPython checks ahead of `FORCE_COLOR`
    and would have defeated exactly this override silently -- this test
    pins that the escape hatch this comment claims to leave open actually
    stays open.
    """
    monkeypatch.setenv("FORCE_COLOR", "3")
    supertool._disable_force_color_for_children()

    merged_env = {**os.environ, "FORCE_COLOR": "3"}  # a preset's env: block
    result = subprocess.run(
        [sys.executable, "-c", "\n".join(_PROBE_LINES)],
        capture_output=True, text=True, timeout=10,
        encoding="utf-8", errors="replace", env=merged_env,
    )

    assert "FORCE_COLOR=3" in result.stdout, result.stdout


@pytest.mark.skipif(
    sys.version_info < (3, 13),
    reason="CPython colourises a piped traceback via FORCE_COLOR only from 3.13; "
           "below that the assertion below would pass vacuously (no escapes on "
           "either side), which is not a real negative. No configured CI leg "
           "runs 3.13+ today (#1429 comment thread), so this test currently "
           "exercises the issue's own repro only on a 3.13+ operator machine.",
)
def test_the_issues_own_repro_no_longer_leaks_escapes(monkeypatch) -> None:
    """The exact command from the issue body, run through the real mechanism."""
    monkeypatch.setenv("FORCE_COLOR", "3")
    supertool._disable_force_color_for_children()

    result = subprocess.run(
        [sys.executable, "-c", "None()"],
        capture_output=True, timeout=10,
    )

    assert b"\x1b[" not in result.stderr, result.stderr
