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
# **`SUPERTOOL_PYTHON` is deliberately not read here** (#1390). It selected the
# interpreter, the only test was `-c pass`, and every binary that exits 0
# passes that — so `SUPERTOOL_PYTHON=/usr/bin/true` was rc 0, empty stdout and
# no disclosure: the guard off, from one environment variable, which is the
# hatch `guard_command`'s docstring says was refused on purpose. It is also an
# exec primitive that fires before every Bash call. The variable exists for
# supertool's own spawns; a gate deciding whether a command may run is a
# different trust context and does not inherit it.
#
# `VIRTUAL_ENV` stays, because on Windows it is often the only interpreter
# there is, and it is required to look like a venv (`pyvenv.cfg`) rather than
# merely to be set. That narrows the same primitive without closing it: an
# attacker who can write two files and set one variable still gets an exec.
# Said plainly here rather than implied, because the ladder cannot be made
# PATH-free — PATH is itself an environment variable, and anyone who controls
# it already controls every command in the session.
#
# If nothing runs, the guard declines in words and the command proceeds. That
# is the same third state the guard itself uses: a gate that did not run must
# say so rather than read as a command that complied. **A candidate that ran
# and produced nothing reaches that state too** (#1390) — before, only an
# empty ladder did, so an interpreter that executed and said nothing rendered
# exactly like a clean verdict. `pre_bash_guard.py` therefore always writes an
# envelope, and empty stdout here means the guard did not answer.
BIN="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}/hooks/pre_bash_guard.py"

decline() {
    printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"supertool raw-command guard did not run: '"$1"'. The command was allowed - this is a statement about the guard, not about the command."}}'
    exit 0
}

CANDIDATES=(python3.14 python3.13 python3.12 python3.11 python3.10 python3.9)
if [ -n "${VIRTUAL_ENV:-}" ] && [ -f "$VIRTUAL_ENV/pyvenv.cfg" ]; then
    CANDIDATES=("$VIRTUAL_ENV/bin/python3" "$VIRTUAL_ENV/Scripts/python.exe" "${CANDIDATES[@]}")
fi

# The event JSON arrives on stdin and only the real run may consume it, so the
# probe reads from /dev/null.
PROBE='import sys; sys.stdout.write("supertool-python-" + str(sys.version_info[0]))'

for candidate in "${CANDIDATES[@]}"; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    # Not `-c pass`. Exiting 0 is a property of `/usr/bin/true`, of `/bin/ls`
    # and of every other binary on the box; printing this line is a property
    # of a Python 3 (#1390).
    said=$("$candidate" -c "$PROBE" 2>/dev/null </dev/null) || continue
    [ "$said" = "supertool-python-3" ] || continue
    out=$("$candidate" "$BIN")
    rc=$?
    if [ "$rc" -ne 0 ]; then
        decline "the interpreter exited $rc"
    fi
    if [ -z "$out" ]; then
        decline "the interpreter ran and produced no verdict"
    fi
    printf '%s' "$out"
    exit 0
done

decline "no python3.9-3.14 on PATH that executes (the bare name python3 is never tried, see hooks/pre-bash-guard.sh)"
