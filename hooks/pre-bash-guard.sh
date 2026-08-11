#!/bin/bash
# PreToolUse(Bash) wrapper — see hooks/pre_bash_guard.py for what it does.
#
# A shell wrapper rather than a direct python entry in hooks.json for the same
# reason session-start.sh is one: hooks.json has no way to fall back, and a
# plugin install where the interpreter is missing must not turn every Bash call
# into a hook error.
#
# **The bare name `python3` is never run** (#572). On Windows it can resolve to
# the App Execution Alias stub, which *blocks* rather than erroring — and this
# hook runs before EVERY Bash call, so that is not one slow command, it is a
# session where nothing runs until each hook timeout expires. Only versioned
# names and an activated venv are tried, and each must execute, not merely
# resolve. Same ladder as .githooks/pre-push.
#
# If nothing runs, the guard declines in words and the command proceeds. That
# is the same third state the guard itself uses: a gate that did not run must
# say so rather than read as a command that complied.
BIN="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}/hooks/pre_bash_guard.py"

CANDIDATES=(python3.14 python3.13 python3.12 python3.11 python3.10 python3.9)
if [ -n "${VIRTUAL_ENV:-}" ]; then
    CANDIDATES=("$VIRTUAL_ENV/bin/python3" "$VIRTUAL_ENV/Scripts/python.exe" "${CANDIDATES[@]}")
fi
if [ -n "${SUPERTOOL_PYTHON:-}" ]; then
    CANDIDATES=("$SUPERTOOL_PYTHON" "${CANDIDATES[@]}")
fi

for candidate in "${CANDIDATES[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "pass" >/dev/null 2>&1; then
        exec "$candidate" "$BIN"
    fi
done

printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"supertool raw-command guard did not run: no python3.9-3.14 on PATH that executes (the bare name python3 is never tried, see hooks/pre-bash-guard.sh). The command was allowed - this is a statement about the guard, not about the command."}}'
