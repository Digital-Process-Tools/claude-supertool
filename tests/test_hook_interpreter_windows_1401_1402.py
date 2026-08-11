"""The two Windows hook defects, one refuted and one fixed (#1401, #1402).

**#1401 is refuted here rather than fixed, and the refutation is pinned.** The
issue reasons that `bash "${CLAUDE_PLUGIN_ROOT}/..."` is resolved by
`CreateProcess` searching PATH, which on Windows finds System32's `bash.exe` -
the WSL launcher. That is how *this repository's own pytest* reaches the
launcher (`subprocess.run(["bash", ...])`, PR #1399), and it is not how the
harness runs a hook. Claude Code's hook contract:

* A command hook with no `args` is **shell form**: the string is handed to a
  shell - `sh -c` on macOS and Linux, **Git Bash on Windows**, or PowerShell
  when Git Bash is not installed.
  <https://code.claude.com/docs/en/hooks#exec-form-and-shell-form>
* A command hook **with** `args` is exec form: `command` is resolved as an
  executable on PATH and spawned directly. That is the `CreateProcess` search
  the issue describes - so adding `args` would *introduce* the defect here,
  not fix it.
* On native Windows the Bash tool exists only when Git for Windows is
  installed; without it Claude Code routes shell commands through the
  PowerShell tool instead.
  <https://code.claude.com/docs/en/setup#windows>

Those compose into the reason #1401 cannot fire for `pre-bash-guard.sh`: the
only host where the hook string reaches PowerShell - and so where `bash` could
resolve to the WSL launcher - is a host with no Git Bash, which is exactly the
host with **no Bash tool**, so a `PreToolUse` hook matched on `Bash` never
fires there at all.

What this file therefore pins is the shape the refutation depends on: both
entries stay shell form. A future reader "fixing" #1401 by adding `args` would
be handing the command to the very PATH search the issue was written about.

**#1402 is fixed.** The ladder probed `python3.9`-`python3.14` and nothing
else, and neither python.org's installer nor GitHub's `hostedtoolcache`
creates those names on Windows - both create `python.exe` and `python3.exe`,
and the bare `python3` is permanently banned (#572) because it can resolve to
a blocking App Execution Alias stub. So on a Windows host that *can* run the
hook, the ladder found nothing and the guard declined on every Bash call.
`py -3` - the Windows Python launcher, a real executable - is added as the
last rung.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pytest

from _toolchain_gate import posix_ci_promised, require_or_skip

_ROOT = Path(__file__).resolve().parent.parent
_WRAPPER = _ROOT / "hooks" / "pre-bash-guard.sh"
_BS = chr(92)
_NL = chr(10)

#: Every versioned name the wrapper probes, so a test can blind all of them.
_VERSIONED = ["python3.14", "python3.13", "python3.12", "python3.11",
              "python3.10", "python3.9"]

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


# --------------------------------------------------------------------------
# #1401 - the refutation, pinned as a shape rather than argued in prose.
# Ungated: these read files and run on every platform.
# --------------------------------------------------------------------------

def _command_hooks() -> List[Dict[str, object]]:
    hooks = json.loads((_ROOT / "hooks" / "hooks.json").read_text(
        encoding="utf-8"))["hooks"]
    found = []
    for groups in hooks.values():
        for group in groups:
            for hook in group.get("hooks") or []:
                if hook.get("type") == "command":
                    found.append(hook)
    return found


def test_both_shipped_hooks_stay_shell_form():
    """Adding `args` would *create* the PATH search #1401 describes.

    Exec form resolves `command` as an executable on PATH and spawns it
    directly, which on Windows is `CreateProcess` finding System32's WSL
    launcher under the name `bash`. Shell form hands the string to a shell,
    and on Windows that shell is Git Bash - whose own `bash` is its own.

    So the plausible-looking repair is the defect. This test is the note that
    survives the next reader not having read #1401.
    """
    hooks = _command_hooks()
    assert len(hooks) >= 2, hooks
    for hook in hooks:
        assert "args" not in hook, (
            "a hook with `args` is exec form: `command` is resolved on PATH "
            "and spawned directly, which is the CreateProcess search #1401 is "
            "about. Shell form is what keeps `bash` meaning Git Bash's bash "
            "on Windows: " + json.dumps(hook))


def test_both_shipped_hooks_are_registered_the_same_way():
    """#1401's one correct observation: this is a class of two, not one.

    Whatever is true of the interpreter in one entry is true of the other, so
    they must not drift apart silently - a reader who finds them different
    concludes the untouched one was already correct.
    """
    interpreters = set()
    for hook in _command_hooks():
        command = str(hook.get("command", ""))
        interpreters.add(command.split(None, 1)[0] if command else "")
    assert len(interpreters) == 1, (
        "the shipped hooks no longer agree on an interpreter, so one of them "
        "was changed alone: " + repr(sorted(interpreters)))


# --------------------------------------------------------------------------
# #1402 - the ladder, exercised against fabricated interpreters.
# --------------------------------------------------------------------------

def _bash_candidates() -> List[Optional[str]]:
    git_bin = "C:" + _BS + "Program Files" + _BS + "Git" + _BS
    return [shutil.which("bash"),
            git_bin + "bin" + _BS + "bash.exe",
            git_bin + "usr" + _BS + "bin" + _BS + "bash.exe",
            "/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"]


def _first_bash_that_runs_a_script(
        candidates: Sequence[Optional[str]]) -> Optional[str]:
    """Chosen by what it does, not by what it is called (#1390)."""
    for candidate in candidates:
        if not candidate:
            continue
        try:
            proc = subprocess.run(
                [candidate, "-c", "printf %s supertool-bash-ok"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip() == "supertool-bash-ok":
            return candidate
    return None


_BASH = _first_bash_that_runs_a_script(_bash_candidates())

_NEEDS_BASH = require_or_skip(
    _BASH is not None,
    "no bash on this host runs a script, so the wrapper cannot be exercised",
    promised=posix_ci_promised())


def _shim(directory: Path, name: str, body: str) -> Path:
    """A fake interpreter, written as a POSIX script and made executable.

    The explicit empty `newline` is load-bearing and is not style. Python's
    text mode translates every line feed to `os.linesep` on write, so on
    Windows these files would be written CRLF - and Git Bash, the shell that
    runs them on the one platform this module exists to exercise, reads the
    carriage return as part of the token: `shift` and `fi` with a CR glued on
    are not `shift` and `fi`. Every fabricated interpreter would fail for a
    reason with nothing to do with the ladder, on Windows only, while the
    whole file stayed green here.
    """
    path = directory / name
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("#!/bin/sh" + _NL + body + _NL)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
               | stat.S_IXOTH)
    return path


def _real_python() -> str:
    return sys.executable.replace(_BS, "/")


def _blind_the_versioned_ladder(directory: Path) -> None:
    """Shadow every `python3.X` with one that exists and does not work.

    Not an empty PATH: stripping PATH entirely breaks the shell itself on
    Windows, and this file must not go vacuous on the one platform it is
    about. Shadowing keeps the shell intact and still guarantees the loop
    falls through to the rung under test.
    """
    for name in _VERSIONED:
        _shim(directory, name, "exit 9")


def _run_wrapper(command: str, cwd: Path, extra_path: Path,
                 timeout: int = 120) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": command}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    # An activated venv prepends its own interpreter to the ladder, which
    # would answer before any rung under test here.
    env.pop("VIRTUAL_ENV", None)
    env["PATH"] = str(extra_path) + os.pathsep + env.get("PATH", "")
    assert _BASH is not None, "the gate should have skipped this test"
    return subprocess.run(
        [_BASH, str(_WRAPPER)], input=payload, capture_output=True,
        text=True, encoding="utf-8", errors="replace", cwd=str(cwd),
        env=env, timeout=timeout)


def _envelope(proc: subprocess.CompletedProcess) -> Dict[str, object]:
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), (
        "the wrapper produced nothing at all: silence from a gate is "
        "indistinguishable from a command that complied")
    return json.loads(proc.stdout)["hookSpecificOutput"]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".supertool.json").write_text(json.dumps(_OPS),
                                             encoding="utf-8")
    return project


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    return directory


@_NEEDS_BASH
def test_the_launcher_answers_when_no_versioned_name_does(project, fake_bin):
    """#1402: the whole Windows population, in one test.

    `python.exe` and `python3.exe` exist, no `python3.X` does, and `python3`
    is banned. Before this the ladder ran out and the guard declined on every
    Bash call while `raw_command_guard` defaulted to on.
    """
    _blind_the_versioned_ladder(fake_bin)
    _shim(fake_bin, "py",
          'if [ "$1" != "-3" ]; then exit 9; fi' + _NL
          + "shift" + _NL
          + 'exec "' + _real_python() + '" "$@"')
    hook = _envelope(_run_wrapper("gh pr view 12", project, fake_bin))
    assert hook.get("permissionDecision") == "deny", hook


@_NEEDS_BASH
def test_the_launcher_is_the_fallback_and_not_the_default(project, fake_bin,
                                                          tmp_path):
    """Ordering, which the issue asks for by name.

    A host with a real `python3.12` must keep using it. `py` records that it
    ran; the recording must not appear.
    """
    marker = tmp_path / "py-was-run"
    _blind_the_versioned_ladder(fake_bin)
    versioned = _shim(fake_bin, "python3.12",
                      'exec "' + _real_python() + '" "$@"')
    _shim(fake_bin, "py",
          ': > "' + str(marker).replace(_BS, "/") + '"' + _NL
          + 'if [ "$1" != "-3" ]; then exit 9; fi' + _NL
          + "shift" + _NL
          + 'exec "' + _real_python() + '" "$@"')

    # The control comes first, and it is the half that makes the assertion
    # below mean anything. A ladder that never reaches `py` at all satisfies
    # "the marker is absent" for free, so without this the test would pass on
    # the unfixed wrapper - the shape this repo calls a test that would pass
    # if the code did nothing.
    versioned.unlink()
    hook = _envelope(_run_wrapper("gh pr view 12", project, fake_bin))
    assert hook.get("permissionDecision") == "deny", hook
    assert marker.exists(), (
        "the launcher never ran even with no versioned interpreter present, "
        "so the ordering assertion below would be vacuous")

    marker.unlink()
    _shim(fake_bin, "python3.12", 'exec "' + _real_python() + '" "$@"')
    hook = _envelope(_run_wrapper("gh pr view 12", project, fake_bin))
    assert hook.get("permissionDecision") == "deny", hook
    assert not marker.exists(), (
        "the launcher ran even though a versioned interpreter was present, "
        "so `py -3` is the default rather than the fallback")


@_NEEDS_BASH
def test_a_launcher_that_prints_a_preamble_is_not_trusted(project, fake_bin):
    """"The probe stays a probe" - the issue's third prerequisite.

    A launcher that writes anything of its own fails the equality check and is
    skipped, so the guard declines in words rather than shipping a verdict
    built on an interpreter it could not identify. Pinned as a decision, not
    left to be discovered.
    """
    _blind_the_versioned_ladder(fake_bin)
    quiet = 'if [ "$1" != "-3" ]; then exit 9; fi' + _NL + "shift" + _NL

    # Control: the same launcher without the preamble is trusted. A wrapper
    # that never tries `py` declines for its own reasons and would satisfy
    # the real assertion for free.
    _shim(fake_bin, "py", quiet + 'exec "' + _real_python() + '" "$@"')
    hook = _envelope(_run_wrapper("gh pr view 12", project, fake_bin))
    assert hook.get("permissionDecision") == "deny", hook

    _shim(fake_bin, "py",
          quiet
          + "printf 'Python launcher 3.12" + _NL.join(["", ""]) + "'" + _NL
          + 'exec "' + _real_python() + '" "$@"')
    hook = _envelope(_run_wrapper("gh pr view 12", project, fake_bin))
    assert hook.get("permissionDecision") != "deny", hook
    assert "did not run" in str(hook.get("additionalContext", "")), hook


@_NEEDS_BASH
def test_no_interpreter_at_all_still_declines_in_words(project, fake_bin):
    """The third state survives the new rung: no `py`, no versioned name."""
    _blind_the_versioned_ladder(fake_bin)
    hook = _envelope(_run_wrapper("gh pr view 12", project, fake_bin))
    assert hook.get("permissionDecision") != "deny", hook
    context = str(hook.get("additionalContext", ""))
    assert "did not run" in context, hook
    assert "py -3" in context, (
        "the decline names the rungs it tried, and the launcher is now one "
        "of them: a reader told only about python3.9-3.14 looks for the "
        "wrong absence")


def test_the_bare_name_python3_is_still_never_tried():
    """#572 has to survive #1402: `py` is tried, `python3` never is.

    The launcher is a real executable installed by python.org; the bare
    `python3` on Windows can be an App Execution Alias stub that *blocks*
    rather than erroring, and this hook runs before every Bash call.
    """
    wrapper = _WRAPPER.read_text(encoding="utf-8")
    for line in wrapper.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "command -v python3 " not in stripped, line
        assert not stripped.startswith("exec python3 "), line
    assert "py -3" in wrapper, "the launcher rung is gone"


def test_the_launcher_is_only_ever_invoked_with_a_version_selector():
    """A bare `py` picks the newest installed Python, which may be a 2.

    `py -3` is the selector that makes the rung mean what the ladder means.
    The probe would catch a Python 2 anyway; this pins the intent so the
    catch never has to fire.
    """
    wrapper = _WRAPPER.read_text(encoding="utf-8")
    found = False
    for line in wrapper.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "attempt py" in stripped:
            found = True
            assert "attempt py -3" in stripped, line
    # Without this the loop matches nothing once the rung is removed, and a
    # test that executes no assertion reports as a pass.
    assert found, "no `attempt py` line at all, so nothing above was checked"
