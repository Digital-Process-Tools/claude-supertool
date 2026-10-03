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
    echo "> No ./supertool wrapper created here: this directory is its own supertool tree, so a wrapper pointing at the plugin install would run the plugin core against this trees config and presets — the mix every custom op declines (#678)."
    echo "> Use: python3 supertool.py op:args — core, config and presets from one tree."
    if [ -e "./supertool" ] || [ -L "./supertool" ]; then
        echo "> Something is already at ./supertool and is left untouched. If it points at the plugin install, it is the broken wrapper described above."
    fi
elif [ -L "./supertool" ] && [ "$(readlink "./supertool")" = "$BIN" ]; then
    :
elif [ -L "./supertool" ] && OLD_VERSION="$(own_stale_symlink_version "$(readlink "./supertool")")"; then
    NEW_VERSION="$(basename "$(dirname "$BIN")")"
    if ln -sf "$BIN" "./supertool" 2>/dev/null; then
        echo "> ./supertool pointed at this plugins own $OLD_VERSION — its own symlink from an earlier release, not a strangers file. The plugin is now $NEW_VERSION; repointed it so calls are answered by the current version."
    else
        echo "> ./supertool pointed at this plugins own $OLD_VERSION — its own symlink from an earlier release, not a strangers file. The plugin is now $NEW_VERSION, but repointing it failed; calls will still be answered by $OLD_VERSION until this is fixed by hand."
    fi
elif [ -e "./supertool" ] || [ -L "./supertool" ]; then
    echo "> ./supertool already exists here and is not the plugin symlink — leaving it untouched."
else
    ln -sf "$BIN" "./supertool" 2>/dev/null
fi


exit 0
