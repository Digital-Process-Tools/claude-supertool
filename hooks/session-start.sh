#!/bin/bash
SUPERTOOL_LADDER_PROBE='import sys; sys.stdout.write("supertool-python-" + str(sys.version_info[0]))'

SUPERTOOL_LADDER_RUNGS="python3.9-python3.14, an activated virtualenvs own interpreter, or the Windows launcher py -3"

supertool_python_identifies() {
    _said=$("$@" -c "$SUPERTOOL_LADDER_PROBE" 2>/dev/null </dev/null) || return 1
    [ "$_said" = "supertool-python-3" ]
}

supertool_python_each() {
    _callback="$1"
    _candidates=(python3.14 python3.13 python3.12 python3.11 python3.10 python3.9)
    if [ -n "${VIRTUAL_ENV:-}" ] && [ -f "$VIRTUAL_ENV/pyvenv.cfg" ]; then
        _candidates=("$VIRTUAL_ENV/bin/python3" "$VIRTUAL_ENV/Scripts/python.exe" "${_candidates[@]}")
    fi

    for _candidate in "${_candidates[@]}"; do
        command -v "$_candidate" >/dev/null 2>&1 || continue
        "$_callback" "$_candidate"
    done

    if command -v py >/dev/null 2>&1; then
        "$_callback" py -3
    fi
}

onboard() {
    supertool_python_identifies "$@" || return 1
    if ! "$@" "${CLAUDE_PLUGIN_ROOT}/supertool.py" 'introduction' 'output-format' 'ops:session'; then
        echo "> supertools op listing is incomplete: the interpreter ran and supertool exited non-zero. The ./supertool wrapper still works; ops prints the listing."
    fi
    "$@" "${CLAUDE_PLUGIN_ROOT}/supertool.py" 'channel:stranded'
    exit 0
}

    supertool_python_each onboard
    echo "> supertools op listing is not shown: nothing on PATH identified itself as a Python 3. Tried $SUPERTOOL_LADDER_RUNGS. The bare name python3 is never run, because on Windows and on a stock macOS it can resolve to a stub that blocks instead of erroring (#572, #1382)."

exit 0
