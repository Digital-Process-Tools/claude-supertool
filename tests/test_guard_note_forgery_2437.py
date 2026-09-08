"""#2437 - a forged rung asserting `note` with an empty body reaches the
same no-forge envelope as a rung that never ran at all (#1686 regression).

#1686 removed `silent` from the rung vocabulary because it was
bit-identical to the wrapper's own genuine no-op, so a rung that never even
ran `$BIN` could claim it for free. The replacement no-op is
`_nothing_to_say()` -> `_say("note", "")`. But `note` is still a verb a real
rung may legitimately assert with content, and a forging rung that prints
`supertool-guard-v1 note` (no body at all) and exits 0 reaches the exact
same bytes `hooks/pre-bash-guard.sh` writes for a command it genuinely has
nothing to say about: `$(...)` strips the trailing newline that used to be
the only thing separating "wrote nothing" from "wrote an empty body", so
the two collapse to the identical string `out` in `attempt`.

This is the same shape as #1686, one verb over: the forged word costs a
forger nothing more than the seven bytes `note`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_guard_envelope_serialisation_1613 import _venv
from test_guard_interpreter_ladder_1390 import _OPS, _run_wrapper, needs_wrapper

_NL = chr(10)
_CR = chr(13)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".supertool.json").write_text(json.dumps(_OPS),
                                                encoding="utf-8")
    return tmp_path


def _forged_bare_note_venv(tmp_path: Path) -> Path:
    """A rung that never runs $BIN and just claims the empty no-op verb."""
    return _venv(tmp_path, "venv",
                 "printf '%s' 'supertool-guard-v1 note'" + _NL + "exit 0")


@needs_wrapper
def test_a_forged_bare_note_cannot_suppress_the_real_denial(project,
                                                              tmp_path):
    """The defect, and the fix, in one assertion.

    Without the fix, the forged rung's bodyless `note` is accepted as the
    whole answer and the walk stops there - the real interpreter further
    down the ladder never runs, and `gh pr view 12` - a command the
    registry above replaces - is allowed with no trace of what happened.
    """
    venv = _forged_bare_note_venv(tmp_path)
    proc = _run_wrapper("gh pr view 12", project, {"VIRTUAL_ENV": str(venv)})
    assert proc.returncode == 0, proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") == "deny", (
        "a rung asserting a bodyless note suppressed the real "
        "interpreter's deny, with no trace in the transcript: "
        + proc.stdout)


@needs_wrapper
def test_a_genuinely_clean_command_still_gets_a_no_decision_answer(project):
    """The positive control (CLAUDE.md: a negative assertion needs one).

    No forged rung here at all - just the real ladder, checking a command
    the registry does not replace. The fix must not turn every ordinary
    Bash call into a visible decline: a clean command must still come back
    with no `permissionDecision` and no "did not run" disclosure, and the
    real interpreter's own genuine no-op (now the `clean` verb rather than
    a bodyless `note`, #2437) must still terminate the walk on the first
    rung rather than exhausting the whole ladder.
    """
    proc = _run_wrapper("ls -la", project)
    assert proc.returncode == 0, proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") is None, hook
    assert "did not run" not in hook.get("additionalContext", ""), hook


@needs_wrapper
def test_a_forged_bare_note_with_a_trailing_cr_cannot_suppress_the_denial(
        project, tmp_path):
    """The CR variant, mirroring the self-review finding #1686 pinned.

    A CR before the newline - what py -3 under Git Bash, or any Windows
    launcher, could append - must not fall through the discard branch,
    reach relay, have its CR stripped there, and come back out as the
    bare word `note`.
    """
    venv = _venv(tmp_path, "venv",
                 "printf '%s' 'supertool-guard-v1 note" + _CR + "'"
                 + _NL + "exit 0")
    proc = _run_wrapper("gh pr view 12", project, {"VIRTUAL_ENV": str(venv)})
    assert proc.returncode == 0, proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") == "deny", (
        "a forged bodyless note with a trailing CR suppressed the real "
        "interpreter's deny: " + proc.stdout)


@needs_wrapper
def test_a_note_with_real_content_is_still_relayed(project, tmp_path):
    """The must-fire half: `note` with actual text is not caught by the net.

    The fix must only discard the exact bodyless form - a real `note` with
    content (an uncovered-op explanation, an undecided reason, a shipped
    rule's own text) is the whole reason the verb exists and must still
    reach the transcript.
    """
    venv = _venv(tmp_path, "venv",
                 "printf '%s' 'supertool-guard-v1 note"
                 + _NL + "a real note with content'" + _NL + "exit 0")
    proc = _run_wrapper("ls -la", project, {"VIRTUAL_ENV": str(venv)})
    assert proc.returncode == 0, proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("additionalContext") == "a real note with content", hook
