"""PreToolUse hook, run by Python directly (#2734).

hooks/pre-bash-guard.sh stays the source plugin's hook. It walks an
interpreter ladder, runs hooks/pre_bash_guard.py, reads back a one-line verb
on a wire protocol and authors the hook JSON itself -- because a bash
wrapper cannot tell which interpreter answered, so it must not let one
write `permissionDecision` (#1625, #1686, #2437).

The release build's hooks.json runs THIS file through a literal interpreter
ladder in hooks.json itself, so no shell script sits between the hook and
the Python (the Anthropic directory validator holds a hook script that runs
a further file). There is no foreign rung to distrust here: the guard runs
in this same process, its verb is captured in memory, and this file writes
the same envelope the wrapper writes, byte for byte -- same escaping as the
wrapper's `_json_string`, same `_note`/`_deny` templates.
tests/test_direct_hooks_2734.py runs both on the same events and compares.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_NOTE = '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}'
_DENY = ('{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
         '"permissionDecision":"deny","permissionDecisionReason":"%s"}}')


def _json_string(text: str) -> str:
    """pre-bash-guard.sh's `_json_string`: backslash, quote, LF, CR and TAB
    escaped; any other control character becomes `?`."""
    bs, dq = chr(92), chr(34)
    text = text.replace(bs, bs + bs).replace(dq, bs + dq)
    text = text.replace(chr(10), bs + "n").replace(chr(13), bs + "r")
    text = text.replace(chr(9), bs + "t")
    return "".join("?" if (ord(c) < 32 or ord(c) == 127) else c for c in text)


def _write(document: str) -> None:
    data = document.encode("utf-8", "replace")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(document)
    else:
        stream.write(data)
    sys.stdout.flush()


def main() -> int:
    """Always exit 0: hooks.json chains the ladder's rungs with `||`, so a
    non-zero exit would run the guard again under the next rung."""
    said = []
    try:
        import pre_bash_guard
        pre_bash_guard._say = lambda verb, text="": said.append((verb, text))
        pre_bash_guard.main()
    except Exception as exc:  # the wrapper's decline, in-process
        said[:] = [("note", "supertool raw-command guard did not run: the guard raised "
                    + type(exc).__name__ + ": " + str(exc)
                    + ". The command proceeded - this is a statement about the guard, "
                      "not about the command.")]
    verb, text = said[-1] if said else ("clean", "")
    if verb == "deny":
        _write(_DENY % _json_string(text))
    elif verb == "note":
        _write(_NOTE % _json_string(text))
    else:
        _write(_NOTE % "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
