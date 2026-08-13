#!/bin/bash
# PreToolUse(Bash) wrapper — see hooks/pre_bash_guard.py for what it does.
#
# A shell wrapper rather than a direct python entry in hooks.json for the same
# reason session-start.sh is one: hooks.json has no way to fall back, and a
# plugin install where the interpreter is missing must not turn every Bash call
# into a hook error.
#
# **Which interpreter runs it is `hooks/python-ladder.sh`'s decision**, shared
# with `hooks/session-start.sh` since #1382. The bare name `python3` is never
# tried (#572), `py -3` is the last rung (#1402), `SUPERTOOL_PYTHON` is not
# read (#1390), and a candidate has to identify itself before it is run — the
# reasoning for each of those lives in that file rather than being restated in
# two, which is the state #1382 was filed about.
#
# **What is this script's own decision is the floor**, and the three hooks that
# resolve an interpreter all pick a different one on purpose. This one
# discloses and allows: when no rung answers, the gate is off and the session
# continues. `.githooks/pre-push` refuses the push, lists every name it tried
# and documents `PYTHON=` as the way through — a loud refusal with an escape
# hatch does not need the extra rung a disclosed allow does. `session-start.sh`
# prints its disclosure and keeps the `./supertool` symlink, which never needed
# an interpreter at all.
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

# Resolved from this script's own directory, not from `CLAUDE_PLUGIN_ROOT`:
# the ladder is a sibling file and always has been, where the root is an
# environment variable a caller can point somewhere else. A ladder that cannot
# be sourced is itself a reason to decline — the alternative is a syntax error
# in a sourced file turning every Bash call into a hook crash.
LADDER="$(cd "$(dirname "$0")" && pwd)/python-ladder.sh"
# shellcheck source=hooks/python-ladder.sh
. "$LADDER" 2>/dev/null || decline "the shared interpreter ladder could not be sourced"

# attempt INTERPRETER [ARG...] — answer through this candidate, or return 1.
# shellcheck disable=SC2329  # invoked indirectly, as supertool_python_each's callback
attempt() {
    supertool_python_identifies "$@" || return 1
    out=$("$@" "$BIN")
    rc=$?
    if [ "$rc" -ne 0 ]; then
        decline "the interpreter exited $rc"
    fi
    if [ -z "$out" ]; then
        decline "the interpreter ran and produced no verdict"
    fi
    printf '%s' "$out"
    exit 0
}

supertool_python_each attempt

decline "no $SUPERTOOL_LADDER_RUNGS on PATH that executes (the bare name python3 is never tried, see hooks/python-ladder.sh)"
