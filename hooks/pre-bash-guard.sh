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
# **An envelope from a rung that then exited non-zero is a fragment, not an
# answer.** The prefix test cannot tell the whole thing from what an
# interpreter killed mid-write leaves behind, and forwarding the fragment is
# unparseable to Claude Code — no decision, no note, the silent fail-open this
# header opens by refusing. Every path through `pre_bash_guard.py` returns 0,
# so an envelope plus exit 0 is the whole condition, and a rung that fails
# either way is recorded and the walk continues. **That record is sticky**: a
# rung that began an answer and died names a broken interpreter, and the six
# rungs tried after it would otherwise overwrite that with "exited N without
# writing a verdict" about a rung that was never going to answer.
# `supertool_python_identifies` stays in the ladder for session-start.sh,
# whose callback runs supertool with real arguments and cannot prefix-test the
# free-form output, and which pays its one probe once per session rather than
# once per command.
BIN="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}/hooks/pre_bash_guard.py"

#: What every path through pre_bash_guard.py starts its stdout with, and the
#: whole of what identifies an answer. `WIRE_PREFIX` in that file is the other
#: half of this rendezvous.
#:
#: **It used to be the envelope's own first bytes, and that was the defect**
#: (#1625). The rung wrote a `hookSpecificOutput` document, this script matched
#: a prefix of it and forwarded the whole thing, so a `$VIRTUAL_ENV/bin/python3`
#: printing a well-formed document carrying `"permissionDecision":"allow"` and
#: exiting 0 had written the harness's verdict. Well-formed JSON: nothing for
#: #1613's escaper, which closes the route where a *path* writes that field by
#: concatenation, not the route where the rung's own stdout does.
#:
#: Authenticating the channel was the other candidate and it cannot work here.
#: Any token proving "the rung ran the script I handed it" has to be handed to
#: the rung, and the rung is the forger - the interpreter *is* what executes
#: `$BIN`, so no secret separates "ran it" from "read it and lied". So the
#: shape changes instead: the rung supplies a verb and a run of text, and every
#: structural byte of the envelope is written below.
WIRE_PREFIX='supertool-guard-v1 '

#: The event JSON, read once. It used to be consumed by whichever rung ran
#: first, which is why the old probe had to read /dev/null; held here, every
#: rung can be offered the same input and a failed one costs nothing.
EVENT=$(cat)

#: The last value passed to `_json_string`, escaped. A global rather than a
#: `$(...)` result on purpose: a command substitution is a fork, on a hook
#: that runs before every Bash call.
JSON_STRING=

# _json_string VALUE - VALUE, safe as the contents of a JSON string literal.
#
# **This exists because three of `decline`'s four call sites pass a value the
# hook did not author** (#1613). `$LAST_TRIED` and `$PARTIAL_TRIED` are the
# argv of the rung that failed, and the first rung is
# `"$VIRTUAL_ENV/bin/python3"` - so a directory name reached a JSON string
# literal built by concatenation. A `"` in it closed `additionalContext`, and
# everything after it was parsed as sibling keys of the same object with
# `permissionDecision` among them: Claude Code read a directory name as this
# hook's own verdict. A bare `"` with nothing after it is the other half - an
# unparseable document, so no decision and no note at all, which is the
# silent fail-open this file's header opens by refusing.
#
# **Escaped in bash rather than serialised by an interpreter, and that is not
# a performance argument** (though it is also free: three parameter
# expansions, no fork and no exec - 0.049ms per call, measured over 10,000
# iterations under bash 3.2 on macOS, against the ~300ms wrapper #1594 cut
# down, and paid at most once per invocation because `decline` exits).
# `decline` is reached *because* no interpreter answered. Handing
# the envelope to `python3` at the one moment the ladder has just finished
# proving there is no `python3` is not slower, it is unreachable.
#
# The order of the passes is load-bearing: the backslash pass has to run
# before every other, or it doubles the backslashes those write. Backslash and
# quote are *escaped* rather than replaced, so `C:\venv\Scripts` stays the path
# it is - a Windows disclosure with its separators rewritten names a directory
# that does not exist, which is #1378's complaint.
#
# **Newline, carriage return and tab are escaped rather than replaced** since
# #1625, and that is not cosmetic: `guard_refusal` is a multi-line document and
# it now reaches JSON through this function rather than through Python's
# `json.dumps`. Replacing its line breaks with `?` would flatten the one
# message a caller actually has to read into a single unreadable line - the
# fix's own legibility regression, on the path that matters most.
#
# Everything else non-printable is still replaced: JSON forbids a raw control
# character inside a string, and a `\u00XX` escape cannot be built by parameter
# expansion. Under a non-UTF-8 locale `[[:print:]]` is byte-wise, so a
# non-ASCII path degrades to one `?` per byte - legible loss, and still a
# document that parses.
_json_string() {
    JSON_STRING=${1//\\/\\\\}
    JSON_STRING=${JSON_STRING//\"/\\\"}
    JSON_STRING=${JSON_STRING//$'\n'/\\n}
    JSON_STRING=${JSON_STRING//$'\r'/\\r}
    JSON_STRING=${JSON_STRING//$'\t'/\\t}
    JSON_STRING=${JSON_STRING//[![:print:]]/?}
}

#: One newline, so the parameter expansions below can name it.
NL='
'

#: One carriage return, for the reason `relay` gives.
CR=$'\r'

# The three envelopes this script may write, and the only three there are.
#
# **Nothing outside these functions prints a document** (#1625). That is the
# whole property: `permissionDecision` appears once in this file, as a literal
# next to the one value it may hold, and every field name is a byte written
# here rather than relayed. A rung supplies text, never structure, and the text
# goes through `_json_string` on its way in.
#
# The cost, stated rather than discovered later: a field Claude Code adds to
# the PreToolUse protocol has to be added here as well as in
# `hooks/pre_bash_guard.py`, and until it is, it cannot be emitted. That is
# the trade re-serialising makes - an open channel into someone else's schema,
# exchanged for a closed one this repository owns both ends of.
# shellcheck disable=SC2329  # reached through `relay`, itself a callback
_silent() {
    printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse"}}'
    exit 0
}

_note() {
    _json_string "$1"
    printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"'"$JSON_STRING"'"}}'
    exit 0
}

# shellcheck disable=SC2329  # reached through `relay`, itself a callback
_deny() {
    _json_string "$1"
    printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"'"$JSON_STRING"'"}}'
    exit 0
}

# Every sink goes through here, present and future: escaping inside `_note`
# rather than at this function's five call sites is what stops a sixth call
# site reintroducing #1613.
decline() {
    _note "supertool raw-command guard did not run: $1. The command was allowed - this is a statement about the guard, not about the command."
}

# relay ANSWER - the envelope this script writes for a rung's answer.
#
# The answer is a verb line and then, from the second line to the end, the
# text. `$(...)` has already stripped the trailing newline, so an answer with
# no newline at all is `silent` with nothing after it rather than a truncation.
#
# **A verb this script does not know is declined, not dropped and not
# forwarded.** It cannot be forwarded - that is the defect. It must not be
# dropped either: a wrapper and a `pre_bash_guard.py` from different installs
# disagreeing about the vocabulary would otherwise turn every Bash call into a
# gate that silently said nothing, which is the fail-open this file's header
# opens by refusing. So it takes the same disclosed-allow the ladder's own
# failures take, and names the dialect it could not read. The verb is
# rung-controlled, so it is cut to a length and escaped like any other text.
# shellcheck disable=SC2329  # reached from `attempt`, itself a callback
relay() {
    _verb=${1%%"$NL"*}
    _verb=${_verb#"$WIRE_PREFIX"}
    # A stray carriage return is a verb, not a dialect. `pre_bash_guard.py`
    # writes bytes rather than text precisely so Windows cannot put one here,
    # but this wrapper also runs under Git Bash against whatever `py -3`
    # resolves to, and a `deny` silently demoted to a disclosed allow by a
    # line ending is the worst failure available on this path. Stripping it
    # reads the verb that was sent; it cannot turn an unknown dialect into a
    # known one.
    _verb=${_verb%"$CR"}
    _text=${1#*"$NL"}
    if [ "$_text" = "$1" ]; then
        _text=
    fi
    case "$_verb" in
        silent) _silent ;;
        note)   _note "$_text" ;;
        deny)   _deny "$_text" ;;
    esac
    decline "the interpreter answered '${_verb:0:60}', which is not a verdict this wrapper knows how to write, so its answer was refused rather than relayed"
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
        "$WIRE_PREFIX"*)
            # The prefix identifies a Python 3 that ran — but a *prefix* of an
            # answer is what an interpreter killed part-way through
            # `sys.stdout.write` leaves behind, and the prefix test accepts
            # it. A fragment is a verb this wrapper cannot read, so relaying
            # one would buy a disclosed allow about a rung that was answering
            # correctly until it died — a worse diagnosis than the one below,
            # which names the interpreter.
            #
            # Every path through `pre_bash_guard.py` returns 0, so a rung that
            # wrote a whole answer exited 0 by construction. Requiring that
            # is not the pre-#1377 shape returning: a rung that fails is still
            # only recorded, and the walk still continues to the next name. It
            # costs the case of a whole answer followed by a non-zero exit
            # for some unrelated reason, whose answer is dropped — bash cannot
            # tell that from a fragment, and dropping it buys a disclosed
            # decline where forwarding it buys silence.
            if [ "$rc" -eq 0 ]; then
                relay "$out"
            fi
            # Sticky, and deliberately not folded into LAST_TRIED below. A
            # rung that began an answer and died is a specific, actionable
            # diagnosis — a broken interpreter, named. The walk continues past
            # it, so without this the message is overwritten by whichever rung
            # happened to be tried last, and the reader is sent looking for a
            # silent interpreter instead of the one that crashed mid-write.
            PARTIAL_TRIED="$*"
            PARTIAL_RC="$rc"
            ;;
    esac
    LAST_TRIED="$*"
    LAST_RC="$rc"
    return 1
}

supertool_python_each attempt

if [ -n "${PARTIAL_TRIED:-}" ]; then
    decline "$PARTIAL_TRIED began writing a verdict and then exited $PARTIAL_RC, so what it had written was discarded rather than forwarded as an answer"
fi
if [ -n "${LAST_TRIED:-}" ]; then
    decline "$LAST_TRIED exited $LAST_RC without writing a verdict"
fi
decline "no $SUPERTOOL_LADDER_RUNGS on PATH that executes (the bare name python3 is never tried, see hooks/python-ladder.sh)"
