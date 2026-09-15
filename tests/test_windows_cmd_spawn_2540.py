"""The mechanism behind #2540, asked of the platform rather than assumed.

Seven validator and formatter adapters resolve a node-packaged CLI with
``shutil.which()`` and then spawn it with a bare name through
``subprocess.run()``. On Windows those two calls disagree: ``which()``
consults ``PATHEXT`` and returns ``eslint.cmd``, while ``subprocess``
without a shell goes through ``CreateProcess``, which searches ``PATH`` but
appends only ``.exe`` and cannot execute a ``.cmd`` at all.

That claim was reasoned from documentation when #2540 was filed, not run --
the same gap PR #2538 closed on the Node side, where ``spawnSync("npm")``
failed with ``ENOENT`` for exactly this reason. These tests are what makes
it measured, on the platform that has it, in CI.

**One of these four was filed wrong and the first windows leg corrected it.**
#2540 claimed that naming the `.cmd` in full does not help either, carried
across from #2538's `EINVAL` on the Node side. In Python it runs: CreateProcess
launches a `.bat`/`.cmd` given a path to one, it just will not find one when it
searches PATH. So the bare name is the whole defect and `which()`'s own return
value is the whole fix. The issue body has been corrected.

**The gate asks the filesystem, it does not read the platform's name.**
``tests/_symlink.py`` is the precedent: ``skipif(os.name == "nt")`` and its
four siblings hardcode a verdict about a platform, so a runner that behaves
differently never executes the test and the suite reports coverage it does
not have. Here the question is "does this interpreter resolve an
extensionless name to a ``.cmd`` on this PATH", and the only honest way to
answer it is to put one there and look.

These pin a property of ``CreateProcess`` and of ``shutil.which``, not a
property of any adapter, so they stay true after #2540 is fixed -- they are
the reason its fix has to take the shape it takes, and the positive control
for any helper written to replace the bare-name spawn.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

TOOL = "st-probe-2540"


@pytest.fixture()
def cmd_shim_on_path(tmp_path, monkeypatch) -> "pathlib.Path | None":
    """A ``.cmd`` shim on PATH, or None where the platform ignores one.

    Returns the shim's path when `shutil.which(TOOL)` resolves the
    extensionless name to it -- which is the PATHEXT behaviour this whole
    defect rests on. Returns None otherwise, and the tests skip with a
    reason naming what was asked, rather than asserting a platform name.
    """
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / f"{TOOL}.cmd"
    shim.write_text("@echo off\r\nexit /b 43\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(shim_dir))
    resolved = shutil.which(TOOL)
    if resolved is None:
        return None
    return pathlib.Path(resolved)


def test_which_resolves_an_extensionless_name_to_a_cmd(cmd_shim_on_path) -> None:
    """The first half of the disagreement: `which` says the tool is present."""
    if cmd_shim_on_path is None:
        pytest.skip(
            f"this platform does not resolve a bare `{TOOL}` to `{TOOL}.cmd` "
            "through PATHEXT, so the #2540 disagreement cannot arise here"
        )
    assert cmd_shim_on_path.suffix.lower() == ".cmd", (
        "which() resolved to something other than the .cmd shim, so this "
        f"fixture is not measuring what it claims: {cmd_shim_on_path}"
    )


def test_spawning_the_bare_name_fails_even_though_which_found_it(
        cmd_shim_on_path) -> None:
    """The second half: the spawn `which` licensed cannot run.

    This is #2540 in three lines. Every adapter in that issue does exactly
    this -- gate on `which(TOOL)`, then `subprocess.run([TOOL, ...])`.
    """
    if cmd_shim_on_path is None:
        pytest.skip(
            f"this platform does not resolve a bare `{TOOL}` to `{TOOL}.cmd` "
            "through PATHEXT, so the #2540 disagreement cannot arise here"
        )
    with pytest.raises(FileNotFoundError):
        subprocess.run([TOOL], capture_output=True, timeout=30)


def test_naming_the_cmd_in_full_is_the_whole_fix(cmd_shim_on_path) -> None:
    """Passing which()'s own return value runs the shim. This is the fix.

    #2540 was filed claiming the opposite -- that CreateProcess cannot
    execute a .cmd however it is named, so a fix had to go through a shell.
    That was carried across from PR #2538, which reports exactly that dead
    end from Node: the explicit `npm.cmd` spelling raised EINVAL there.

    It does not transfer to Python, and the first run of this file on a
    windows leg is what said so -- this test asserted `pytest.raises(OSError)`
    and failed with `DID NOT RAISE`. CreateProcess launches a .bat or .cmd
    given a path to it; what it will not do is find one, because it appends
    only `.exe` when it searches PATH. The bare name is the entire defect.

    So the repair for all seven adapters is to spawn the string `which()`
    already returned instead of the name that was passed to it. No shell, no
    quoting surface, no `cmd /c`, and argv stays a list.

    43 is the shim's own exit code, so it can only arrive here if the shim
    actually ran -- this doubles as the positive control for the two
    assertions above, which are otherwise equally satisfied by a platform
    that spawns nothing at all.
    """
    if cmd_shim_on_path is None:
        pytest.skip(
            f"this platform does not resolve a bare `{TOOL}` to `{TOOL}.cmd` "
            "through PATHEXT, so the #2540 disagreement cannot arise here"
        )
    r = subprocess.run([str(cmd_shim_on_path)], capture_output=True, timeout=30)
    assert r.returncode == 43, (
        "the shim did not run, so the two assertions above are about a "
        f"platform that spawns nothing at all: exit {r.returncode}"
    )


def test_cmd_c_also_works_but_is_not_needed(cmd_shim_on_path) -> None:
    """The route #2540 proposed, kept as the record of a rejected option.

    `cmd /c <resolved path>` does reach the shim. It is simply unnecessary
    now that the test above shows the resolved path alone is enough, and an
    extra process in every validator spawn is a cost with nothing bought.

    Kept rather than deleted because the next reader will have the same idea
    #2538 gave me, and one skipped test on darwin is cheaper than rediscovering
    this on a windows leg. What must never come back is `shell=True`: these
    adapters' argv lists carry a caller-supplied file path, and
    `subprocess.run(list, shell=True)` concatenates unescaped on Windows, so a
    path holding a space or an `&` becomes shell syntax. #2538 could use
    `shell: true` safely because its argv is a fixed static list; #2540's is not.
    """
    if cmd_shim_on_path is None:
        pytest.skip(
            f"this platform does not resolve a bare `{TOOL}` to `{TOOL}.cmd` "
            "through PATHEXT, so the #2540 disagreement cannot arise here"
        )
    r = subprocess.run(["cmd", "/c", str(cmd_shim_on_path)],
                       capture_output=True, timeout=30)
    assert r.returncode == 43, (
        "the shim did not run, so the three assertions above are about a "
        f"platform that spawns nothing at all: exit {r.returncode}"
    )
