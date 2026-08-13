# shellcheck shell=bash
# The interpreter ladder both shipped hooks resolve through (#1382).
# Sourced, never executed - hence a `shell` directive and no shebang, and
# hence bash rather than sh: both sourcing hooks are `#!/bin/bash` and the
# candidate list is an array.
#
# It lives in its own file because it used to live in pre-bash-guard.sh alone,
# and session-start.sh - the other hook in this directory - ran the bare name
# `python3`. Two scripts a few lines apart disagreeing about the repo's own
# convention is not a typo, it is what happens when a decision has no home:
# the next hook written here would have chosen for a third time. One file, so
# a hook inherits the decision instead of making it.
#
# **The bare name `python3` is never a candidate** (#572). On Windows it can
# resolve to the App Execution Alias stub, which *blocks* rather than erroring;
# on a stock macOS `/usr/bin/python3` is the Xcode Command Line Tools stub,
# which opens an install dialog. Neither fails - both hang, one before every
# Bash call and one at session start, where the symptom is a slow startup and
# no supertool output rather than an error anyone can act on. Versioned names,
# an activated venv and `py -3` are tried instead, and each must *execute*,
# not merely resolve.
#
# **`py -3` is the last rung, and it is the only one Windows usually has**
# (#1402). Neither python.org's installer nor GitHub's `hostedtoolcache`
# creates `python3.9.exe`-`python3.14.exe`; both create `python.exe` and
# `python3.exe`. So the versioned ladder finds nothing on a standard Windows
# install. The launcher is a real executable rather than an alias stub, it
# takes a version selector, and it is tried **after** every versioned name so
# a host with a real `python3.12` keeps using it. Graded **reasoned, not
# observed** (the #627 convention): nobody here has a Windows box. The
# load-bearing claim is that Windows ships no default App Execution Alias for
# `py.exe` - the stubs that block, and that got `python3` banned in #572, are
# `python.exe` and `python3.exe`. If that is wrong, the cost is #572 again,
# which is why the rung is last: any host with a versioned interpreter never
# reaches it.
#
# **#572 considered `py -3` and dropped it, and this reverses that** for the
# hooks only. Its reason, from the v0.15.0 CHANGELOG entry, was "this is a
# bash script that only ever runs under Git Bash or WSL, where a Windows
# launcher shim is the wrong layer to reach for" - a preference about layering,
# argued without the fact #1402 supplies: on Windows the versioned names it
# chose instead **do not exist**. #572 checked that versioned names are not
# *aliased*, which is true, and not that they are present. Under WSL `py` is
# simply absent and the rung costs a `command -v`. `.githooks/pre-push` keeps
# the shorter ladder deliberately and does not source this file: it refuses the
# push and names `PYTHON=` as the way through, and a loud refusal with an
# escape hatch does not need the extra rung a disclosed degrade does.
#
# **`SUPERTOOL_PYTHON` is deliberately not read here** (#1390). It selected the
# interpreter, the only test was `-c pass`, and every binary that exits 0
# passes that - so `SUPERTOOL_PYTHON=/usr/bin/true` was rc 0, empty stdout and
# no disclosure. The variable exists for supertool's own spawns; a gate
# deciding whether a command may run is a different trust context and does not
# inherit it.
#
# `VIRTUAL_ENV` stays, because on Windows it is often the only interpreter
# there is, and it is required to look like a venv (`pyvenv.cfg`) rather than
# merely to be set. That narrows the same primitive without closing it: an
# attacker who can write two files and set one variable still gets an exec.
# Said plainly rather than implied, because the ladder cannot be made PATH-free
# - PATH is itself an environment variable, and anyone who controls it already
# controls every command in the session.
#
# **What the caller decides is what happens when nothing answers**, and the two
# hooks answer differently on purpose: the guard declines in words and lets the
# command through, the session hook prints its disclosure and still leaves the
# `./supertool` symlink it made without any interpreter at all. This file
# resolves; it never decides.

#: One name every rung has to print exactly, so a candidate is chosen by what
#: it does rather than by what it is called. Not `-c pass`: exiting 0 is a
#: property of `/usr/bin/true`, of `/bin/ls` and of every other binary on the
#: box (#1390). Equality rather than substring, which is also what rejects a
#: launcher that writes a preamble of its own (#1402).
#: Named `SUPERTOOL_LADDER_*` rather than `SUPERTOOL_PYTHON_*` on purpose:
#: `SUPERTOOL_PYTHON` is the variable #1390 removed from this trust context,
#: and `tests/test_guard_interpreter_ladder_1390.py` pins its absence by
#: substring. A prefix that happens to contain it would have to weaken that
#: test to ship, and the test is right.
SUPERTOOL_LADDER_PROBE='import sys; sys.stdout.write("supertool-python-" + str(sys.version_info[0]))'

#: The rungs, in words, for a caller disclosing that none of them answered. A
#: reader told only about the versioned names looks for the wrong absence.
# shellcheck disable=SC2034  # read by the sourcing hook, not by this file
SUPERTOOL_LADDER_RUNGS="python3.9-python3.14, an activated virtualenv's own interpreter, or the Windows launcher py -3"

# supertool_python_identifies INTERPRETER [ARG...] - is this argv a Python 3?
#
# The event JSON arrives on stdin for the guard and only the real run may
# consume it, so the probe reads from /dev/null.
supertool_python_identifies() {
    _said=$("$@" -c "$SUPERTOOL_LADDER_PROBE" 2>/dev/null </dev/null) || return 1
    [ "$_said" = "supertool-python-3" ]
}

# supertool_python_each CALLBACK - call CALLBACK with each candidate argv.
#
# Takes a callback rather than printing a list because a candidate is an argv,
# not a word: `py -3` is two of them and a `$VIRTUAL_ENV` path can contain
# spaces, so any string-splitting rendezvous between this file and its callers
# would break one or the other. A callback that succeeds is expected to exit
# the script; returning simply advances to the next rung, and falling out of
# the loop is how a caller learns nothing answered.
supertool_python_each() {
    _callback="$1"
    # shellcheck disable=SC2178,SC2128  # _candidates is an array throughout
    _candidates=(python3.14 python3.13 python3.12 python3.11 python3.10 python3.9)
    if [ -n "${VIRTUAL_ENV:-}" ] && [ -f "$VIRTUAL_ENV/pyvenv.cfg" ]; then
        _candidates=("$VIRTUAL_ENV/bin/python3" "$VIRTUAL_ENV/Scripts/python.exe" "${_candidates[@]}")
    fi

    for _candidate in "${_candidates[@]}"; do
        command -v "$_candidate" >/dev/null 2>&1 || continue
        "$_callback" "$_candidate"
    done

    # The Windows Python launcher, last (#1402). `command -v` first, so a host
    # without it never execs anything: a missing `py` costs a builtin lookup,
    # not a spawn.
    if command -v py >/dev/null 2>&1; then
        "$_callback" py -3
    fi
}
