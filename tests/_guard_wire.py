"""The envelope `hooks/pre-bash-guard.sh` writes for a rung's answer (#1625).

`hooks/pre_bash_guard.py` no longer writes the `hookSpecificOutput` document.
It writes a verb line, and the wrapper authors the document - which is the
whole of the fix, because a document the wrapper merely forwards is one an
attacker-controlled interpreter can write instead.

Tests that drive `pre_bash_guard.py` directly still want to assert about the
envelope, so this reproduces the wrapper's serialisation in Python. **It is a
mirror, and a mirror can drift**, so
`tests/test_guard_reserialised_envelope_1625.py` runs the real wrapper for
every verb and asserts this function agrees with it. Without that row, a wrong
mirror would make every caller here agree with itself.
"""

from __future__ import annotations

from typing import Any, Dict

#: `hooks/pre_bash_guard.py`'s `WIRE_PREFIX`, spelled out rather than imported:
#: a test that reads the constant from the file under test cannot notice the
#: file changing it.
PREFIX = "supertool-guard-v1 "

_NL = chr(10)


def envelope(stdout: str) -> Dict[str, Any]:
    """The document Claude Code would receive for this answer."""
    assert stdout.startswith(PREFIX), (
        "not an answer from the guard at all: " + repr(stdout[:120]))
    # No `assert sep`: the wrapper reads the answer out of `$(...)`, which has
    # already stripped the trailing newline, so an answer with no newline at
    # all is a verb and no text rather than a truncation. A mirror stricter
    # than the thing it mirrors fails on input the wrapper accepts.
    verb, _sep, text = stdout[len(PREFIX):].partition(_NL)
    if verb == "silent":
        hook: Dict[str, Any] = {}
    elif verb == "note":
        hook = {"additionalContext": text}
    elif verb == "deny":
        hook = {"permissionDecision": "deny", "permissionDecisionReason": text}
    else:
        raise AssertionError("verb the wrapper cannot write: " + repr(verb))
    hook["hookEventName"] = "PreToolUse"
    return {"hookSpecificOutput": hook}
