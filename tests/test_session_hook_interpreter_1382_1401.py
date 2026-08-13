"""The SessionStart hook's interpreter, and what it does with none (#1382, #1401).

**#1382.** `hooks/session-start.sh` ran the bare name `python3`. #572 banned
that name from every spawn position in this repo: on Windows it can resolve to
the App Execution Alias stub, which *blocks* rather than erroring, and on a
stock macOS `/usr/bin/python3` is the Xcode Command Line Tools stub, which pops
an install dialog. Either way the hook does not fail - it hangs, at session
start, where the only visible symptom is that Claude Code takes a minute to
come up and then says nothing about supertool.

`hooks/pre-bash-guard.sh` already resolved a versioned ladder for exactly this
reason, so the two hooks in one directory disagreed about their own convention.
The ladder now lives in `hooks/python-ladder.sh` and both source it - the point
being that the next hook written here inherits the decision instead of making
it again.

**#1401, the half that is left.** The issue's central claim was refuted in
`test_hook_interpreter_windows_1401_1402.py`; what survived is that
`SessionStart` is not tool-gated, so a native Windows host with no Git for
Windows runs this hook, fails to start it, and loses both the `./supertool`
symlink and the op roster. Nothing inside a `.sh` file can fix a host that
cannot run a `.sh` file, and that is disclosed in `docs/configuration.md`
rather than papered over with a command string nobody here can test.

What *is* fixable is the neighbouring shape, and it is the one these tests pin:
when the hook does run and the interpreter is the thing that is missing, it
must not take the session down and must not go quiet. Three states - the
roster, or a named disclosure, never a bare failure - and the symlink, which
needs no interpreter at all, survives all of them.

Windows-skipped for the same reason `test_session_hook_plugin_path.py` is: a
bare `bash` on the Windows runners is the WSL launcher stub, so the hook under
test never opens and every assertion here would be about the stub.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_HOOK = _ROOT / "hooks" / "session-start.sh"
_GUARD = _ROOT / "hooks" / "pre-bash-guard.sh"
_LADDER = _ROOT / "hooks" / "python-ladder.sh"
_NL = chr(10)
_BS = chr(92)

#: Every name the ladder probes, so a test can blind all of them at once. The
#: bare `python3` is on this list although the ladder must never try it: these
#: tests have to be able to prove it is not reached, and a name left unblinded
#: is the host's own interpreter answering rather than the code.
_PROBED = ["python3.14", "python3.13", "python3.12", "python3.11",
           "python3.10", "python3.9", "py", "python3"]

windows_has_no_usable_bash = pytest.mark.skipif(
    os.name == "nt",
    reason="bare `bash` on Windows CI is the WSL stub; the hook never runs",
)


def _uncommented(path: Path):
    text = path.read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        yield number, line


def _shim(directory: Path, name: str, body: str) -> Path:
    """A fake interpreter, written POSIX-LF and made executable.

    The explicit empty `newline` is the same load-bearing detail as in
    `test_hook_interpreter_windows_1401_1402.py`: Python text mode would write
    CRLF on Windows and Git Bash reads the carriage return as part of the
    token.
    """
    path = directory / name
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("#!/bin/sh" + _NL + body + _NL)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _blind(directory: Path, name: str) -> Path:
    """Occupy a name with something that resolves, runs and cannot answer.

    Never deletion: removing the shim re-exposes whatever the host has under
    that name one PATH entry further along, and the test then measures the
    runner.
    """
    return _shim(directory, name, "exit 9")


def _blind_the_whole_ladder(directory: Path) -> None:
    for name in _PROBED:
        _blind(directory, name)


def _fake_plugin_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "supertool.py").write_text(
        "print('PLUGIN-BINARY-RAN')" + _NL, encoding="utf-8")
    return root


def _run_hook(cwd: Path, plugin_root: Path, extra_path: Path):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    # An activated venv prepends its own interpreter to the ladder and would
    # answer before any rung under test here.
    env.pop("VIRTUAL_ENV", None)
    env["PATH"] = str(extra_path) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        ["bash", str(_HOOK)], cwd=str(cwd), env=env, capture_output=True,
        text=True, timeout=120, encoding="utf-8", errors="replace")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    return directory


def test_the_session_hook_never_spawns_the_bare_name_python3():
    """#1382, as a static claim: the name is gone from every spawn position.

    Matched at command position only, because the hook legitimately *prints*
    the string - the advice it gives inside a supertool checkout is literally
    `python3 supertool.py 'op:args'`, and banning the characters would ban the
    sentence.
    """
    spawn = re.compile(r"(^|[;&|(]|\$\()\s*python3(\s|$)")
    for number, line in _uncommented(_HOOK):
        assert not spawn.search(line), (
            "hooks/session-start.sh:" + str(number) + " spawns the bare name "
            "`python3`, which #572 bans from every spawn position in this "
            "repo: " + line.strip())


def test_both_hooks_resolve_their_interpreter_through_one_ladder():
    """#1382's actual ask: one helper, or the next hook chooses again.

    Asserted as "neither script carries its own candidate list", not as "both
    files mention the helper" - a script that sources the ladder and then
    keeps a private list of its own would satisfy the weaker form while being
    exactly the drift this is about.
    """
    assert _LADDER.is_file(), "hooks/python-ladder.sh does not exist"
    for path in (_HOOK, _GUARD):
        text = path.read_text(encoding="utf-8")
        assert "python-ladder.sh" in text, (
            path.name + " does not source the shared ladder")
        for number, line in _uncommented(path):
            assert "python3.14" not in line, (
                path.name + ":" + str(number) + " carries its own copy of the "
                "candidate ladder: " + line.strip())


@windows_has_no_usable_bash
def test_the_roster_prints_through_a_versioned_interpreter(project, fake_bin,
                                                           tmp_path):
    """The ladder resolves, so the onboarding output is unchanged.

    Every name is blinded first and exactly one versioned rung is then made to
    work, so this measures the ladder rather than whatever the host has on
    PATH. Its control is the decline test below: with the same PATH and no
    working rung, `PLUGIN-BINARY-RAN` must be absent.
    """
    _blind_the_whole_ladder(fake_bin)
    _shim(fake_bin, "python3.11",
          'exec "' + sys.executable.replace(_BS, "/") + '" "$@"')
    result = _run_hook(project, _fake_plugin_root(tmp_path), fake_bin)
    assert result.returncode == 0, result.stderr
    assert "PLUGIN-BINARY-RAN" in result.stdout, result.stdout + result.stderr


@windows_has_no_usable_bash
def test_no_interpreter_leaves_the_symlink_and_says_why(project, fake_bin,
                                                        tmp_path):
    """Three states, and the third one is the whole point of #1382.

    A hook that cannot resolve an interpreter must not be a broken session and
    must not be a silent one. The symlink needs no interpreter, so it is still
    created; the roster cannot be produced, so its absence is stated in words
    that name what was tried.
    """
    _blind_the_whole_ladder(fake_bin)
    plugin_root = _fake_plugin_root(tmp_path)
    result = _run_hook(project, plugin_root, fake_bin)

    assert result.returncode == 0, (
        "a SessionStart hook that exits non-zero is a broken session on every "
        "platform, to report a missing interpreter on one: " + result.stderr)
    assert "PLUGIN-BINARY-RAN" not in result.stdout, (
        "an interpreter answered anyway, so the assertions here are about the "
        "host rather than about the ladder")
    link = project / "supertool"
    assert link.is_symlink(), (
        "the convenience symlink does not need an interpreter and must "
        "survive one being absent")
    assert os.readlink(link) == str(plugin_root / "supertool.py")
    assert "python3.9" in result.stdout and "py -3" in result.stdout, (
        "the disclosure has to name the rungs it tried, or the reader looks "
        "for the wrong absence: " + repr(result.stdout))
