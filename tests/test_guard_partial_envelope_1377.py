"""A rung that began a verdict and died had its fragment forwarded (#1377).

#1377 removed the wrapper's separate probe spawn: an envelope on stdout is now
both the proof that the candidate is a Python 3 and the answer itself, matched
as a *prefix* so a launcher that prints a preamble of its own is still
rejected. The exit code stopped being consulted, which was deliberate - a rung
that printed nothing and failed must fall through to the next rung rather than
decline outright, where the pre-#1377 shape declined on the spot and a host
with a broken `python3.14` and a working `python3.12` got no guard at all.

What went with it is the case where both things happen at once. An interpreter
killed part-way through `sys.stdout.write` - OOM, a signal, a launcher that
wraps the child - leaves a *prefix* of the envelope on stdout, which is
exactly what the prefix test accepts. The wrapper printed that fragment
verbatim and exited 0. Claude Code cannot parse it, so there is no decision
and no `additionalContext`: the silent fail-open the wrapper exists to
prevent, and the shape `pre-bash-guard.sh`'s own header calls out - "a gate
that did not run must say so rather than read as a command that complied".

**The signal that separates the two cases is the exit code, and every path
through `pre_bash_guard.py` returns 0**, so a rung that wrote a whole envelope
exited 0 by construction. So an envelope is forwarded only from a rung that
also exited 0; anything else is a rung that failed, and the walk continues to
the next one exactly as #1377 intended. A rung whose whole envelope was
followed by a non-zero exit for some unrelated reason is treated the same way
and its answer is dropped, because bash cannot tell that fragment from this
one - and dropping it costs a disclosed decline, where forwarding it costs
silence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_guard_interpreter_ladder_1390 import _BASH, needs_wrapper

_ROOT = Path(__file__).resolve().parent.parent
_WRAPPER = _ROOT / "hooks" / "pre-bash-guard.sh"
_NL = chr(10)

#: The first rung `hooks/python-ladder.sh` walks, so a shim here answers before
#: anything the host really has.
_FIRST_RUNG = "python3.14"
#: The next one, for the row that proves the walk still continues.
_SECOND_RUNG = "python3.13"

_ENVELOPE_PREFIX = '{"hookSpecificOutput"'

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

_POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix",
    reason="the rungs here are shebang scripts on a PATH written with POSIX "
           "literals; under Git Bash `command -v` would not find them and "
           "every row would assert against the decline path instead")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / ".supertool.json").write_text(json.dumps(_OPS),
                                               encoding="utf-8")
    return directory


def _shim(bindir: Path, name: str, body: str) -> Path:
    path = bindir / name
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("#!/bin/bash" + _NL + body + _NL)
    path.chmod(0o755)
    return path


def _run(bindir: Path, project: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + "/usr/bin" + os.pathsep + "/bin"
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    # An activated virtualenv is prepended to the ladder and would answer
    # before any rung under test.
    env.pop("VIRTUAL_ENV", None)
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "gh pr view 1"}})
    assert _BASH is not None, "the gate should have skipped this test"
    return subprocess.run([_BASH, str(_WRAPPER)], input=payload,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(project), env=env,
                          timeout=120)


@_POSIX_ONLY
@needs_wrapper
def test_a_fragment_of_an_envelope_is_never_forwarded_as_a_verdict(
        tmp_path, project):
    """The defect. A dying interpreter, reproduced by a shim that does both.

    Whatever reaches Claude Code has to be a whole envelope or the hook said
    nothing at all - and saying nothing is the state that reads as a clean
    command.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _shim(bindir, _FIRST_RUNG,
          "printf '%s' '" + _ENVELOPE_PREFIX + "'" + _NL + "exit 7")

    proc = _run(bindir, project)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("hookEventName") == "PreToolUse", proc.stdout
    assert "did not run" in (hook.get("additionalContext") or ""), proc.stdout


@_POSIX_ONLY
@needs_wrapper
def test_the_decline_says_the_rung_began_a_verdict_rather_than_wrote_none(
        tmp_path, project):
    """Two failures that need different words, or the reader debugs the wrong
    one. "wrote no verdict" sends someone looking for a silent interpreter;
    what happened is an interpreter that started answering and died.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _shim(bindir, _FIRST_RUNG,
          "printf '%s' '" + _ENVELOPE_PREFIX + "'" + _NL + "exit 7")

    context = json.loads(_run(bindir, project).stdout)[
        "hookSpecificOutput"]["additionalContext"]
    assert "7" in context, context
    assert "without writing a verdict" not in context, context


@_POSIX_ONLY
@needs_wrapper
def test_a_rung_that_dies_still_falls_through_to_the_next_one(
        tmp_path, project):
    """#1377's own property, which the repair must not spend.

    Declining on the first non-zero exit is what #1377 deleted: a host with a
    broken first rung and a working later one got no guard at all. A partial
    envelope must not be forwarded *and* must not stop the walk.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _shim(bindir, _FIRST_RUNG,
          "printf '%s' '" + _ENVELOPE_PREFIX + "'" + _NL + "exit 7")
    _shim(bindir, _SECOND_RUNG,
          'exec ' + sys.executable + ' "$@"')

    proc = _run(bindir, project)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") == "deny", proc.stdout


@_POSIX_ONLY
@needs_wrapper
def test_a_rung_that_writes_nothing_and_dies_still_falls_through(
        tmp_path, project):
    """The other half of the same walk, so the repair cannot pass by
    declining on every non-zero exit."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _shim(bindir, _FIRST_RUNG, "exit 9")
    _shim(bindir, _SECOND_RUNG, 'exec ' + sys.executable + ' "$@"')

    proc = _run(bindir, project)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") == "deny", proc.stdout
