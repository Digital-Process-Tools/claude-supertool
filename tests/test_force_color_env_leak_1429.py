"""An operator's ambient FORCE_COLOR must never reach a spawned child (#1429).

CPython 3.13+ colourises its own tracebacks purely because `FORCE_COLOR` is
set in the environment -- even when stderr is a pipe -- so any child this
process spawns hands back ANSI escapes baked into whatever field captures its
output. The confirmed instance (`_mcp_stop_server`'s `detail`, #574/#1333)
already strips escapes on the way IN; this pins the other fix the issue asked
to choose between -- the child never sees the variable in the first place,
so nothing downstream has to guess which bytes were decoration.

Two tests. The first is version-independent: it never depends on CPython's
own colourising machinery, only on whether the child's environment carries
`FORCE_COLOR`/`NO_COLOR`, so it means the same thing on every CI leg (3.9-
3.12) as it does on the 3.13+ interpreter that surfaced the issue. The second
reproduces the issue's own repro verbatim and is skipped below 3.13, where
CPython does not colourise a piped traceback at all -- there the assertion
would be vacuous rather than a real negative (see CLAUDE.md's cross-platform
audit note on that trap).
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

import supertool


def test_disable_force_color_strips_the_var_and_sets_no_color(monkeypatch) -> None:
    """The extracted helper is the whole mechanism -- test it directly."""
    monkeypatch.setenv("FORCE_COLOR", "3")
    monkeypatch.delenv("NO_COLOR", raising=False)

    supertool._disable_force_color_for_children()

    assert "FORCE_COLOR" not in os.environ
    assert os.environ["NO_COLOR"] == "1"


_PROBE_LINES = [
    "import os, sys",
    "sys.stdout.write('FORCE_COLOR=' + os.environ.get('FORCE_COLOR', '<unset>'))",
    "sys.stdout.write(chr(10))",
    "sys.stdout.write('NO_COLOR=' + os.environ.get('NO_COLOR', '<unset>'))",
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
    assert "NO_COLOR=1" in result.stdout, result.stdout


@pytest.mark.skipif(
    sys.version_info < (3, 13),
    reason="CPython colourises a piped traceback via FORCE_COLOR only from 3.13; "
           "below that the assertion below would pass vacuously (no escapes on "
           "either side), which is not a real negative.",
)
def test_the_issues_own_repro_no_longer_leaks_escapes(monkeypatch) -> None:
    """The exact command from the issue body, run through the real mechanism."""
    monkeypatch.setenv("FORCE_COLOR", "3")
    supertool._disable_force_color_for_children()

    result = subprocess.run(
        [sys.executable, "-c", "None()"],
        capture_output=True, timeout=10,
    )

    assert b"\\x1b[" not in result.stderr, result.stderr
