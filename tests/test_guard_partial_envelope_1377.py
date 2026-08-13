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

**Every rung is shadowed, and that is the invariant rather than a detail.**
The first version of this file installed the dying shim at the ladder's first
rung and left the rest of the ladder alone, which passes on macOS and fails on
ubuntu: `/usr/bin` there carries a real `python3.10`/`python3.11`, `/usr/bin`
here carries only the bare `python3` that is never a rung (#572). So the walk
stepped past the dying shim, found a genuine interpreter, and returned a
genuine `deny` - the test asserted against the real guard under the shim's
name and went red for the right reason on four ubuntu legs of PR #1608 while
staying green on the platform it was written on. That is this repository's own
defect class aimed at a test: an absence here (no later rung) was a property
of the author's laptop read as a property of the world.

The rung list is derived from `hooks/python-ladder.sh` rather than copied, so
a rung added there cannot silently un-blind this file, and the parse refuses
to return nothing - a fixture that shadowed zero names would leave every test
below measuring the runner while still reporting green.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

from test_guard_interpreter_ladder_1390 import _BASH, needs_wrapper

_ROOT = Path(__file__).resolve().parent.parent
_WRAPPER = _ROOT / "hooks" / "pre-bash-guard.sh"
_LADDER = _ROOT / "hooks" / "python-ladder.sh"
_NL = chr(10)

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


def _ladder_rung_names() -> List[str]:
    """Every name `supertool_python_each` looks up, read from the ladder.

    Derived rather than copied: a seventh versioned rung added there would
    otherwise be a name this file does not shadow, which is the host's own
    interpreter answering under a test's name - the failure that took four
    ubuntu legs of PR #1608. The `$VIRTUAL_ENV` rung is not a PATH lookup and
    is handled by unsetting the variable in `_run`.
    """
    source = _LADDER.read_text(encoding="utf-8")
    match = re.search(r"_candidates=\(([^)]*)\)", source)
    assert match, (
        "hooks/python-ladder.sh no longer declares `_candidates=(...)`, so "
        "this file cannot know which names to shadow and every test below "
        "would silently measure the host's interpreters")
    names = match.group(1).split()
    assert len(names) >= 2, names
    if re.search(r"command -v py\b", source):
        names.append("py")
    assert "py" in names, (
        "the ladder no longer probes `py`, or the probe was spelled another "
        "way: an unshadowed `py` is a live Windows launcher answering here")
    return names


#: Resolved once. The first entry is the rung consulted first, which is where
#: a shim has to sit to be the one under test.
_RUNGS = _ladder_rung_names()
_FIRST_RUNG = _RUNGS[0]
_SECOND_RUNG = _RUNGS[1]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / ".supertool.json").write_text(json.dumps(_OPS),
                                               encoding="utf-8")
    return directory


def _shim(bindir: Path, name: str, body: str) -> Path:
    """A fake interpreter, written with an explicit empty `newline`.

    Text mode would translate the line feeds to `os.linesep`, and a bash that
    reads `exit 7` with a carriage register glued on runs neither.
    """
    path = bindir / name
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("#!/bin/bash" + _NL + body + _NL)
    path.chmod(0o755)
    return path


@pytest.fixture
def bindir(tmp_path: Path) -> Path:
    """A PATH head where every rung exists, runs, and cannot answer.

    Shadowing rather than emptying PATH: the wrapper itself needs `cat` and
    `dirname`, so the directory is prepended and the real ones stay reachable
    behind it. A test then overwrites only the rungs it is about, and no host
    interpreter can reach the walk.
    """
    directory = tmp_path / "bin"
    directory.mkdir()
    for name in _RUNGS:
        _shim(directory, name, "exit 9")
    return directory


def _dying_rung(bindir: Path, name: str = _FIRST_RUNG) -> None:
    """An interpreter killed part-way through writing its envelope."""
    _shim(bindir, name,
          "printf '%s' '" + _ENVELOPE_PREFIX + "'" + _NL + "exit 7")


def _real_rung(bindir: Path, name: str) -> None:
    _shim(bindir, name, 'exec ' + sys.executable + ' "$@"')


def _run(bindir: Path, project: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + "/usr/bin" + os.pathsep + "/bin"
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    # An activated virtualenv is prepended to the ladder ahead of every
    # versioned name and is not a PATH lookup, so shadowing cannot reach it.
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
def test_the_shadowed_ladder_lets_no_host_interpreter_answer(bindir, project):
    """The fixture, tested. Guard-the-guard, and the row PR #1608 needed.

    Every assertion below reads as a statement about the wrapper only while
    no real interpreter can reach the walk. If the shadowing ever stops
    working - a rung renamed in the ladder, a shim not marked executable -
    the rows below quietly begin asserting against the genuine guard and pass
    or fail on what the runner image happens to ship. So the blinding is
    asserted directly rather than assumed: with every rung exiting 9 and
    nothing else installed, the wrapper must decline, never decide.
    """
    hook = json.loads(_run(bindir, project).stdout)["hookSpecificOutput"]
    assert "permissionDecision" not in hook, (
        "a real interpreter answered through the shadowed ladder, so every "
        "other test in this file is measuring the runner: " + json.dumps(hook))
    assert "did not run" in hook.get("additionalContext", ""), hook


@_POSIX_ONLY
@needs_wrapper
def test_a_fragment_of_an_envelope_is_never_forwarded_as_a_verdict(
        bindir, project):
    """The defect. A dying interpreter, reproduced by a shim that does both.

    Whatever reaches Claude Code has to be a whole envelope or the hook said
    nothing at all - and saying nothing is the state that reads as a clean
    command.
    """
    _dying_rung(bindir)

    proc = _run(bindir, project)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("hookEventName") == "PreToolUse", proc.stdout
    assert "did not run" in (hook.get("additionalContext") or ""), proc.stdout


@_POSIX_ONLY
@needs_wrapper
def test_the_decline_says_the_rung_began_a_verdict_rather_than_wrote_none(
        bindir, project):
    """Two failures that need different words, or the reader debugs the wrong
    one. "wrote no verdict" sends someone looking for a silent interpreter;
    what happened is an interpreter that started answering and died.

    **And the specific diagnosis has to survive the rest of the walk.** The
    dying rung is the first one, and six more are tried after it - so a
    message built from whichever rung was tried *last* names a rung that was
    never going to answer and buries the one that crashed mid-write. Found by
    shadowing the whole ladder, which the first version of this file did not
    do; with only the first rung installed there was no later rung to
    overwrite it and the defect was invisible.
    """
    _dying_rung(bindir)

    context = json.loads(_run(bindir, project).stdout)[
        "hookSpecificOutput"]["additionalContext"]
    assert _FIRST_RUNG in context, context
    assert "7" in context, context
    assert "began writing a verdict" in context, context
    assert "without writing a verdict" not in context, context


@_POSIX_ONLY
@needs_wrapper
def test_a_rung_that_dies_still_falls_through_to_the_next_one(bindir, project):
    """#1377's own property, which the repair must not spend.

    Declining on the first non-zero exit is what #1377 deleted: a host with a
    broken first rung and a working later one got no guard at all. A partial
    envelope must not be forwarded *and* must not stop the walk.
    """
    _dying_rung(bindir)
    _real_rung(bindir, _SECOND_RUNG)

    proc = _run(bindir, project)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") == "deny", proc.stdout


@_POSIX_ONLY
@needs_wrapper
def test_a_rung_that_writes_nothing_and_dies_still_falls_through(
        bindir, project):
    """The other half of the same walk, so the repair cannot pass by
    declining on every non-zero exit."""
    _shim(bindir, _FIRST_RUNG, "exit 9")
    _real_rung(bindir, _SECOND_RUNG)

    proc = _run(bindir, project)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") == "deny", proc.stdout
