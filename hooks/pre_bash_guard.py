#!/usr/bin/env python
"""PreToolUse(Bash) — refuse a raw command the op registry says it replaces (#1347).

The enforcement ships **with supertool**, so every plugin user gets the same
gate rather than each repo hand-writing regexes for the conventions it wants.
The mapping itself is not here: it is `replaces` on the op's own registry
entry, and this file only asks `guard_command` and renders the answer.

Three outcomes, deliberately not two:

* a match  -> `permissionDecision: deny`, with the op's own description.
* no match, and the guard could see everything -> an envelope with no
  decision in it, which is silence to the caller.
* the guard could **not** answer -> the command runs, and the transcript says
  so in `additionalContext`. Failing closed makes an unreadable config a wall
  the caller cannot even edit their way out of; failing open *silently* is the
  exact defect #1347 opens with, where a gate that intermittently did not run
  was indistinguishable from a command that complied. Allow, and disclose.

**Every path writes an envelope, including the clean one** (#1390). Writing
nothing was the same bytes an interpreter that never started writes, so the
wrapper above could not tell "checked, nothing replaced" from "did not run" —
the third state existing in this file and not surviving the layer below it.
The clean envelope carries `hookEventName` and no decision, so what the caller
sees is unchanged; what the *wrapper* sees is the difference between an answer
and an absence.
"""
from __future__ import annotations

import json
import os
import sys


def _emit(payload: dict) -> None:
    payload["hookEventName"] = "PreToolUse"
    sys.stdout.write(json.dumps({"hookSpecificOutput": payload}))


def _nothing_to_say() -> None:
    """An answer that decides nothing — not the same bytes as no answer."""
    _emit({})


def _undecided(reason: str) -> None:
    _emit({"additionalContext":
           "supertool raw-command guard did not run on this command: "
           + reason
           + ". The command was allowed - this is a statement about the "
             "guard, not about the command."})


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        _undecided(f"the hook input did not parse ({exc})")
        return 0

    if event.get("tool_name") not in (None, "Bash"):
        _nothing_to_say()
        return 0
    command = (event.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        _nothing_to_say()
        return 0

    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import _supertool
    except Exception as exc:  # pragma: no cover - a broken install
        _undecided(f"supertool could not be imported from {root} ({exc})")
        return 0

    try:
        verdict = _supertool.guard_command(command)
    except Exception as exc:  # pragma: no cover - defensive
        _undecided(f"the guard raised {type(exc).__name__}: {exc}")
        return 0

    if verdict.state == "blocked":
        _emit({"permissionDecision": "deny",
               "permissionDecisionReason": _supertool.guard_refusal(verdict)})
    elif verdict.state == "undecided":
        _undecided("; ".join(verdict.notes))
    else:
        _nothing_to_say()
    return 0


if __name__ == "__main__":
    sys.exit(main())
