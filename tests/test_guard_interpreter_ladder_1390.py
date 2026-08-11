"""The hook's interpreter ladder was an off-switch and an exec primitive (#1390).

`hooks/pre-bash-guard.sh` prepended `$SUPERTOOL_PYTHON` to its ladder and
`exec`d the first candidate that survived `-c pass`. **Any binary that exits 0
satisfies that test**, so `SUPERTOOL_PYTHON=/usr/bin/true` turned the guard off
with rc 0, empty stdout and no disclosure — the exact environment-variable
hatch `guard_command`'s own docstring says was deliberately refused:

    The escape hatch is deliberately **not** an environment variable. An env
    var that turns a block off is learned once and then prepended forever,
    which is not a block.

Two separable properties are pinned here.

* **A candidate must be proved to be a Python 3**, not merely to exit 0, and
  `SUPERTOOL_PYTHON` does not participate at all. It exists for the tool's own
  spawns; a hook that decides whether a command may run is a different trust
  context, and inheriting the variable is what merged them.
* **"Could not run" is reachable when a candidate ran and produced nothing.**
  Before this the wrapper only disclosed when the ladder was *empty*, so a
  candidate that executed and said nothing rendered exactly like a command
  that complied. That is the three-state contract with a hole in it, inside
  the enforcement layer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from _toolchain_gate import posix_ci_promised, require_or_skip

_ROOT = Path(__file__).resolve().parent.parent
_WRAPPER = _ROOT / "hooks" / "pre-bash-guard.sh"
_BS = chr(92)

#: What a bash that actually runs a script prints when asked to.
_BASH_PROBE = "supertool-bash-ok"

#: The four `windows-latest` legs of PR #1399, decoded. `subprocess.run` with
#: a bare `"bash"` lets CreateProcess search `PATH`, and System32's `bash.exe`
#: is the **WSL launcher**. On a runner with no distribution installed it
#: writes this, in UTF-16LE, exits 1, and never opens the script it was given.
_WSL_NO_DISTRO = (
    "Windows Subsystem for Linux has no installed distributions." + chr(10)
    + "Use 'wsl.exe --list --online' to list available distributions" + chr(10)
    + "and 'wsl.exe --install <Distro>' to install." + chr(10) + chr(10))


def _bash_candidates() -> List[Optional[str]]:
    """Where a bash that runs scripts might be, most likely first."""
    git_bin = "C:" + _BS + "Program Files" + _BS + "Git" + _BS
    # PATH first, then the two Git-for-Windows locations, then the POSIX
    # ones. The fallbacks matter in both directions: a Windows PATH can put
    # the WSL launcher ahead of Git Bash, and a POSIX PATH can put anything
    # ahead of /bin/bash — in either case giving up on the first candidate
    # would skip this file on a host that can run it perfectly well, which is
    # the same silence this gate exists to remove.
    return [shutil.which("bash"),
            git_bin + "bin" + _BS + "bash.exe",
            git_bin + "usr" + _BS + "bin" + _BS + "bash.exe",
            "/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"]


def _first_bash_that_runs_a_script(
        candidates: Sequence[Optional[str]]) -> Optional[str]:
    """A bash chosen by what it does, not by what it is called.

    `shutil.which("bash")` answers "a file named bash is on PATH", which on
    Windows is satisfied by the WSL launcher — a program that is not a shell,
    cannot open a script, and says so in UTF-16 on stdout. Asking a candidate
    to print a known string separates the two, and separates them the same way
    on every platform, so there is no `os.name` branch here to go vacuous on
    one of them.

    The chosen path is returned and then *used*, rather than falling back to
    the bare name: `subprocess.run(["bash", ...])` on Windows re-searches PATH
    through CreateProcess, which need not agree with `shutil.which`, so
    probing one executable and spawning another proves nothing about the one
    that ran.
    """
    for candidate in candidates:
        if not candidate:
            continue
        try:
            proc = subprocess.run(
                [candidate, "-c", "printf %s " + _BASH_PROBE],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip() == _BASH_PROBE:
            return candidate
    return None


def _envelope_or_reason(returncode: int, stdout: str
                        ) -> Tuple[Optional[dict], str]:
    """The hook's verdict, or why there is not one — never the third thing.

    The gate this replaces asked whether the phrase "did not run" was **absent**
    from stdout, and got "absent" from a shell that had never opened the
    wrapper. An absence produced by the harness, read as an absence in the
    world — this repo's house defect, inside the guard written to stop these
    tests going vacuous. So the question is now positive: is there an envelope
    here, and does it say the guard answered?
    """
    if returncode != 0:
        return None, ("the shell exited " + str(returncode) + " and produced "
                      + repr(stdout[:80]))
    try:
        hook = json.loads(stdout)["hookSpecificOutput"]
    except (ValueError, KeyError, TypeError):
        return None, "no hook envelope in " + repr(stdout[:80])
    if not isinstance(hook, dict):
        return None, "the envelope is not an object: " + repr(stdout[:80])
    note = hook.get("additionalContext") or ""
    if "did not run" in note:
        return None, note[:200]
    return hook, ""


_BASH = _first_bash_that_runs_a_script(_bash_candidates())


def _ladder_answers() -> Tuple[bool, str]:
    """Can this host exercise the wrapper at all, and if not, exactly why?"""
    if _BASH is None:
        return False, "no bash on this host runs a script"
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "ls -la"}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    try:
        proc = subprocess.run(
            [_BASH, str(_WRAPPER)], input=payload, capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=env,
            timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "the wrapper could not be spawned: " + str(exc)
    hook, why = _envelope_or_reason(proc.returncode, proc.stdout)
    return hook is not None, why


_LADDER_OK, _LADDER_WHY = _ladder_answers()

# Two facts, promised separately, because only one of them is a promise this
# repo's CI makes. `bash` on a POSIX runner is a given — `posix_ci_promised`
# says so, and `_toolchain_gate` turns a broken promise into a collection
# error rather than a skip that reads as a pass. Whether a *versioned*
# `python3.X` is on PATH is a property of the runner image on every platform,
# POSIX ones included, so that one is a plain skip that names what it saw.
_NEEDS_BASH = require_or_skip(
    _BASH is not None,
    "no bash on this host runs a script, so the wrapper cannot be exercised",
    promised=posix_ci_promised())

_NEEDS_VERDICT = pytest.mark.skipif(
    not _LADDER_OK,
    reason="the guard hook produced no verdict on this host, so every "
           "assertion here would test the decline path under this test's "
           "name: " + (_LADDER_WHY or "no reason recorded"))


def needs_wrapper(func):
    """Both gates, on the tests that shell out — **not** as `pytestmark`.

    A module-level `pytestmark` applies to every test in the file, which would
    take the gate's own unit tests below down with it on exactly the hosts
    where the gate is doing something: silence in the file that exists to
    remove silence. `test_ci_non_python_coverage_557.py` says the same thing
    about `needs_bash` and it was written before this, so this is the repo's
    answer rather than a new one — found the hard way here by simulating a
    host with no versioned interpreter and watching all ten tests skip.
    """
    return _NEEDS_BASH(_NEEDS_VERDICT(func))


# --------------------------------------------------------------------------
# The gate itself, tested. Ungated on purpose: they run on every platform,
# including the ones where the gate skips the rest of this file, because an
# untested gate is precisely what failed on PR #1399.
# --------------------------------------------------------------------------

def test_a_program_that_is_not_a_shell_is_not_selected_as_bash():
    """PR #1399, four `windows-latest` legs — and the defect was this gate.

    `bash` resolved to the WSL launcher, which exits 1 and writes UTF-16 to
    stdout without ever opening the wrapper. The old gate asked whether a
    phrase was absent from that output, concluded the ladder worked, and let
    every assertion below run against a shell that had never started.

    `sys.executable` stands in for it: a real program, on this box, that is
    not a shell — same shape, no platform branch, no fixture.
    """
    assert _first_bash_that_runs_a_script([sys.executable]) is None
    assert _first_bash_that_runs_a_script([None, ""]) is None


def test_the_wsl_launchers_output_is_never_read_as_a_verdict():
    hook, why = _envelope_or_reason(1, _WSL_NO_DISTRO)
    assert hook is None
    assert "exited 1" in why, why


def test_output_that_is_not_an_envelope_is_never_read_as_a_verdict():
    for stdout in ("", "   ", "not json", "[]", json.dumps({"other": 1}),
                   json.dumps({"hookSpecificOutput": "a string"})):
        hook, why = _envelope_or_reason(0, stdout)
        assert hook is None, stdout
        assert why, stdout


def test_a_real_verdict_is_recognised():
    """The gate must not reject everything: that is vacuous by another road."""
    envelope = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny"}})
    hook, why = _envelope_or_reason(0, envelope)
    assert hook is not None and why == "", why
    declined = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "supertool raw-command guard did not run: x"}})
    hook, why = _envelope_or_reason(0, declined)
    assert hook is None and "did not run" in why

_OPS = {
    "ops": {
        "gh-pr": {
            "safety": "read-only",
            "cmd": "true",
            "syntax": "gh-pr:NUMBER",
            "description": "Review a pull request.",
            "replaces": [{"argv": "gh pr view", "use": "gh-pr:NUMBER"}],
        },
    }
}


def _run_wrapper(command: str, cwd: Path,
                 extra_env: Dict[str, str] | None = None,
                 plugin_root: str | None = None
                 ) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": command}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = plugin_root or str(_ROOT)
    env.update(extra_env or {})
    assert _BASH is not None, "the gate should have skipped this module"
    return subprocess.run(
        [_BASH, str(_WRAPPER)], input=payload, capture_output=True,
        text=True, encoding="utf-8", errors="replace", cwd=str(cwd),
        env=env, timeout=120)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".supertool.json").write_text(json.dumps(_OPS),
                                              encoding="utf-8")
    return tmp_path


@needs_wrapper
def test_the_wrapper_denies_a_replaced_command(project):
    """The control. Without this the rows below prove nothing."""
    proc = _run_wrapper("gh pr view 12", project)
    assert proc.returncode == 0, proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny", proc.stdout


@needs_wrapper
@pytest.mark.parametrize("value", ["/usr/bin/true", "/bin/echo"])
def test_an_env_var_cannot_turn_the_guard_off(value, project):
    """`SUPERTOOL_PYTHON=/usr/bin/true` used to be rc 0, no stdout, no note."""
    if not os.path.exists(value):
        pytest.skip(value + " is not present on this host")
    proc = _run_wrapper("gh pr view 12", project,
                        {"SUPERTOOL_PYTHON": value})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), (
        "the guard produced nothing at all: silence from a gate is "
        "indistinguishable from a command that complied")
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") == "deny", proc.stdout


def test_the_wrapper_does_not_read_supertool_python():
    """The variable is out of this trust context entirely, not merely probed.

    A version probe stops `/usr/bin/true` and does not stop a script that
    prints the right line and then does nothing — so the property worth
    pinning is that the hook's ladder is not steerable by this variable.
    """
    wrapper = _WRAPPER.read_text(encoding="utf-8")
    for line in wrapper.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "SUPERTOOL_PYTHON" not in stripped, line


@needs_wrapper
def test_a_candidate_that_ran_and_said_nothing_is_disclosed(project,
                                                            tmp_path):
    """The third state, at the layer that had two.

    A real Python is selected and executed, and it produces nothing because
    the script it was pointed at does not exist. Before #1390 the wrapper
    `exec`d and whatever happened next was the hook's whole output — an empty
    stdout that reads as `clean`.
    """
    empty = tmp_path / "not-an-install"
    empty.mkdir()
    proc = _run_wrapper("gh pr view 12", project,
                        plugin_root=str(empty))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "the wrapper said nothing at all"
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") != "deny"
    assert "did not run" in hook.get("additionalContext", ""), proc.stdout


def test_the_versioned_ladder_survived():
    """#572: the bare name `python3` is never run."""
    wrapper = _WRAPPER.read_text(encoding="utf-8")
    assert "python3.9" in wrapper
    for line in wrapper.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "command -v python3 " not in stripped, line
