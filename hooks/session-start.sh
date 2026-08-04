#!/bin/bash
# SessionStart hook — creates ./supertool symlink and outputs
# self-documentation from .supertool.json for LLM onboarding.

# Create ./supertool symlink so the model can call it from any project.
# Be specific about which file that is: a project may already have something at
# this name, and replacing it — or later invoking it — would make the hook's
# behaviour a property of the checkout rather than of the plugin.
BIN="${CLAUDE_PLUGIN_ROOT}/supertool.py"

# A session that starts inside a checkout of this repo must not get a wrapper
# at all (#711). The config and presets/ resolve from the checkout while the
# link points at the plugin install, so the wrapper runs the plugin's core
# against this tree's presets — the mix #678 refuses. Every custom op through
# it answers "comes from a different supertool tree" and exits 1: a wrapper
# that is present, looks right, and works for nothing.
#
# This is a refusal, not a trust decision. The hook does not read, verify or
# link the local supertool.py; deciding that a local file is genuine is exactly
# how #688's defect returns. It decides only that linking *here* would produce
# a broken wrapper, and creates none. A false positive costs a convenience
# symlink that would not have worked anyway; the other design's false positive
# links a stranger's file.
#
# Mirrors _mixed_tree_pair(): walk up for the .supertool.json that would be
# loaded, then look for a supertool.py beside it. `-ef` compares device+inode
# through symlinks, so the plugin install running in its own directory — the
# same file on both sides — is correctly not a mix.
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

if in_foreign_supertool_tree; then
    echo "> No ./supertool wrapper created here: this directory is its own supertool tree, so a wrapper pointing at the plugin install would run the plugin core against this tree's config and presets — the mix every custom op declines (#678)."
    echo "> Use: python3 supertool.py 'op:args' — core, config and presets from one tree."
    if [ -e "./supertool" ] || [ -L "./supertool" ]; then
        echo "> Something is already at ./supertool and is left untouched. If it points at the plugin install, it is the broken wrapper described above."
    fi
elif [ -L "./supertool" ] && [ "$(readlink "./supertool")" = "$BIN" ]; then
    :
elif [ -e "./supertool" ] || [ -L "./supertool" ]; then
    echo "> ./supertool already exists here and is not the plugin symlink — leaving it untouched."
else
    ln -sf "$BIN" "./supertool" 2>/dev/null
fi

# Output self-documentation from .supertool.json (fallback if no config).
# Use 'ops-compact' to drop redundant examples and stay closer to the harness's
# hook-stdout cap (~2KB). The compact view prepends a warning if the output
# still exceeds the cap, so the model can detect truncation and fetch the full
# listing on demand via `./supertool 'ops'`.
python3 "$BIN" 'introduction' 'output-format' 'ops-compact'
