#!/bin/bash

BIN="${CLAUDE_PLUGIN_ROOT}/supertool.py"

in_foreign_supertool_tree() {
    local d prev
    d="$(pwd -P)"
    while [ -n "$d" ]; do
        if [ -f "$d/.supertool.json" ]; then
            [ -f "$d/supertool.py" ] && ! [ "$d/supertool.py" -ef "$BIN" ]
            return
        fi
        prev="$d"
        d="$(dirname "$d")"
        [ "$d" = "$prev" ] && break
    done
    return 1
}

own_stale_symlink_version() {
    local target="$1" plugin_dir target_dir
    [ -e "$target" ] || return 1
    plugin_dir="$(dirname "$(dirname "$BIN")")"
    target_dir="$(dirname "$(dirname "$target")")"
    [ "$(basename "$target")" = "$(basename "$BIN")" ] || return 1
    [ -n "$target_dir" ] && [ "$target_dir" = "$plugin_dir" ] || return 1
    basename "$(dirname "$target")"
}

if in_foreign_supertool_tree; then
    echo "> No ./supertool wrapper created here: this directory is its own supertool tree, so a wrapper pointing at the plugin install would run the plugin core against this tree's config and presets — the mix every custom op declines (#678)."
    echo "> Use: python3 supertool.py 'op:args' — core, config and presets from one tree."
    if [ -e "./supertool" ] || [ -L "./supertool" ]; then
        echo "> Something is already at ./supertool and is left untouched. If it points at the plugin install, it is the broken wrapper described above."
    fi
elif [ -L "./supertool" ] && [ "$(readlink "./supertool")" = "$BIN" ]; then
    :
elif [ -L "./supertool" ] && OLD_VERSION="$(own_stale_symlink_version "$(readlink "./supertool")")"; then
    NEW_VERSION="$(basename "$(dirname "$BIN")")"
    if ln -sf "$BIN" "./supertool" 2>/dev/null; then
        echo "> ./supertool pointed at this plugin's own $OLD_VERSION — its own symlink from an earlier release, not a stranger's file. The plugin is now $NEW_VERSION; repointed it so calls are answered by the current version."
    else
        echo "> ./supertool pointed at this plugin's own $OLD_VERSION — its own symlink from an earlier release, not a stranger's file. The plugin is now $NEW_VERSION, but repointing it failed; calls will still be answered by $OLD_VERSION until this is fixed by hand."
    fi
elif [ -e "./supertool" ] || [ -L "./supertool" ]; then
    echo "> ./supertool already exists here and is not the plugin symlink — leaving it untouched."
else
    ln -sf "$BIN" "./supertool" 2>/dev/null
fi


SUPERTOOL_LADDER_PROBE='import sys; sys.stdout.write("supertool-python-" + str(sys.version_info[0]))'

SUPERTOOL_LADDER_RUNGS="python3.9-python3.14, an activated virtualenv's own interpreter, or the Windows launcher py -3"

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
        echo "> supertool's op listing is incomplete: the interpreter ran and supertool exited non-zero. The ./supertool wrapper still works; 'ops' prints the listing."
    fi
    "$@" "${CLAUDE_PLUGIN_ROOT}/supertool.py" 'channel:stranded'
    exit 0
}

    supertool_python_each onboard
    echo "> supertool's op listing is not shown: nothing on PATH identified itself as a Python 3. Tried $SUPERTOOL_LADDER_RUNGS. The bare name python3 is never run, because on Windows and on a stock macOS it can resolve to a stub that blocks instead of erroring (#572, #1382)."

exit 0
