
















from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_NOTE = '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}'
_DENY = ('{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
         '"permissionDecision":"deny","permissionDecisionReason":"%s"}}')


def _json_string(text: str) -> str:


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


    said = []
    try:
        import pre_bash_guard
        pre_bash_guard._say = lambda verb, text="": said.append((verb, text))
        pre_bash_guard.main()
    except Exception as exc:  
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
