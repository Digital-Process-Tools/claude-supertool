"""A bare, cwd-relative existence check with no dirname guard is a real spawn
hazard, not just an AST pattern (#2602).

#2575/#2579/#2581 fixed the FIRST disjunct of every adapter's absent-tool
gate: `shutil.which(X)` -> `spawnable(X)`, both routed through
`which_excluding_cwd` so a repo-planted shim at the project root can never
win. They left the SECOND disjunct alone -- seven call sites all shaped

    if not spawnable(X) and not (Path(X).exists() and os.access(X, os.X_OK)):
        decline

-- with no `os.path.dirname()` guard. A bare, separator-free name checked
this way resolves relative to the CURRENT DIRECTORY exactly like the raw
`which()` curdir insertion the first disjunct was already fixed to stop: a
repository under inspection shipping an extensionless executable literally
named e.g. `prettier` at its own root satisfies `Path("prettier").exists()
and os.access("prettier", os.X_OK)`, the gate does not decline, `argv0()`
falls back to the bare name, and `subprocess.run(["prettier", ...])` hands
`CreateProcess` a bare name whose Windows search order puts the parent's
current directory ahead of PATH -- with the maintainer's forge tokens in the
child's environment (adapter chokepoints pass no `cwd=`).

This test replays that exact boolean expression against a real planted
executable to prove the hazard is real (not merely a string pattern an AST
walker could be fooled by either way), then asserts the actual fix in
effect at all seven sites -- `spawnable.already_a_path()`, the same
dirname-guarded check `_already_a_path`/`bin_resolve._is_executable` already
used -- closes it.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "validators" / "common"))
from spawnable import already_a_path  # noqa: E402


@pytest.fixture()
def bare_executable_in_cwd(tmp_path, monkeypatch):
    """A mode-0755 file named `faketool`, planted at a tmp cwd's own root,
    with the test chdir'd there -- the exact shape a repository under
    inspection can ship to try to shadow a real tool of that name.
    """
    exe = tmp_path / "faketool"
    exe.write_text("#!/bin/sh\necho pwned\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    return "faketool"


def test_the_vulnerable_expression_is_a_real_hazard(bare_executable_in_cwd) -> None:
    """Observed, not argued: the literal pre-#2602 second disjunct
    (`Path(X).exists() and os.access(X, os.X_OK)`, no dirname guard)
    evaluates True for a bare name that only exists via the current
    directory. This is the shape all seven sites had before the fix --
    replayed here so the hazard is pinned independently of whether any
    particular adapter still contains it.
    """
    name = bare_executable_in_cwd
    vulnerable_second_disjunct = (
        pathlib.Path(name).exists() and os.access(name, os.X_OK)
    )
    assert vulnerable_second_disjunct is True, (
        "the pre-#2602 second disjunct was expected to wrongly pass a bare "
        "cwd-relative name -- if this is False the reproduction itself is "
        "broken, not the fix"
    )


def test_already_a_path_declines_the_same_bare_name(bare_executable_in_cwd) -> None:
    """The actual fix now in effect at all seven #2602 sites: `already_a_path`
    (the dirname-guarded check every adapter now calls instead of
    reimplementing the vulnerable expression inline) must refuse the exact
    same bare name the test above just proved the old expression accepted.
    """
    name = bare_executable_in_cwd
    assert already_a_path(name) is False, (
        "already_a_path() accepted a bare, separator-free name resolved "
        "only via the current directory -- the #2602 dirname guard is not "
        "doing its job"
    )


def test_already_a_path_still_finds_a_real_absolute_tool(tmp_path) -> None:
    """Positive control, in the same fixture family as the two negative
    checks above: a real tool addressed by an absolute path (carrying a
    directory component) must still be found and spawned normally. Without
    this, a dirname guard broad enough to reject the bare name could also
    reject every legitimate absolute-path tool and nobody would notice from
    the negative tests alone.
    """
    exe = tmp_path / "realtool"
    exe.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    exe.chmod(0o755)
    assert already_a_path(str(exe)) is True, (
        "already_a_path() must still accept an absolute path to a real, "
        "executable file -- it carries a directory component, so the "
        "dirname guard must not block it"
    )


def test_a_relative_path_with_a_directory_component_still_works(tmp_path, monkeypatch) -> None:
    """A second positive control: a RELATIVE path that still carries a
    directory separator (e.g. `./tool` or `sub/tool`) is not the bare-name
    hazard #2602 is about and must still be accepted.
    """
    sub = tmp_path / "sub"
    sub.mkdir()
    exe = sub / "subtool"
    exe.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    assert already_a_path(os.path.join("sub", "subtool")) is True, (
        "a relative path WITH a directory component must still resolve -- "
        "only a bare, separator-free name is the hazard"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
