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
#
# **One spawn per call, not two** (#1377). A candidate used to be proved a
# Python 3 by a throwaway `-c` run and then spawned again for the answer —
# 52ms of a measured 301ms wrapper, on every Bash call, to learn something the
# answer itself carries. The envelope is now the identification: only this
# script writes one, so a candidate that produces it both is a Python 3 and
# ran. #1402's guard against a launcher that prints a preamble of its own
# survives as a *prefix* test rather than an equality test, and #1390's "ran
# and said nothing" is still reachable — no envelope, no verdict, next rung.
# `supertool_python_identifies` stays in the ladder for session-start.sh,
# whose callback runs supertool with real arguments and cannot prefix-test the
# free-form output, and which pays its one probe once per session rather than
# once per command.
BIN="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}/hooks/pre_bash_guard.py"

#: What every path through pre_bash_guard.py starts its stdout with.
#: `tests/test_guard_hook_cost_1377.py` pins that this is what `_emit` writes.
ENVELOPE_PREFIX='{"hookSpecificOutput"'

#: The event JSON, read once. It used to be consumed by whichever rung ran
#: first, which is why the old probe had to read /dev/null; held here, every
#: rung can be offered the same input and a failed one costs nothing.
EVENT=$(cat)

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
#
# A rung that fails is recorded and the walk continues, where the old shape
# declined outright on a non-zero exit. Trying the next name is strictly more
# robust: a host with a broken python3.14 and a working python3.12 used to get
# no guard at all.
# shellcheck disable=SC2329  # invoked indirectly, as supertool_python_each's callback
attempt() {
    out=$(printf '%s' "$EVENT" | "$@" "$BIN")
    rc=$?
    case "$out" in
        "$ENVELOPE_PREFIX"*)
            printf '%s' "$out"
            exit 0
            ;;
    esac
    LAST_TRIED="$*"
    LAST_RC="$rc"
    return 1
}

supertool_python_each attempt

if [ -n "${LAST_TRIED:-}" ]; then
    decline "$LAST_TRIED exited $LAST_RC without writing a verdict"
fi
decline "no $SUPERTOOL_LADDER_RUNGS on PATH that executes (the bare name python3 is never tried, see hooks/python-ladder.sh)"
