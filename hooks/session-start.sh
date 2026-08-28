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
#
# Through the shared ladder (#1382), never the bare name `python3`. That name
# used to be right here, and #572 bans it from every spawn position in this
# repo: on Windows it can resolve to the App Execution Alias stub and on a
# stock macOS to the Xcode Command Line Tools stub, and both *block* rather
# than error. At session start that is not an error message, it is Claude Code
# taking a hook timeout to come up and then saying nothing about supertool —
# an absence produced by the tool, read as an absence in the world.
#
# **Three states, and the floor is this hook's own decision** (#1382 asks which
# it should be). Not a bare `python3` last rung: it would keep the hang for
# exactly the hosts that have no alternative. Not a loud failure either — a
# non-zero SessionStart hook is a broken session on every platform to report a
# missing interpreter on one. So: say it once, degrade, keep going. The
# `./supertool` symlink above never needed an interpreter and is already made;
# what is lost is the roster, and the line below names what was tried so the
# reader looks for the right absence.
#
# 'ops:session' — signatures, with the names-only roster as a measured fallback
# (#2028). Not 'ops-compact': `ops-compact` is ~16.5KB and `ops:full` ~76.0KB# against a 10,000-byte cap, so the compact listing was truncated every session
# everything alphabetically after `grep` was hidden — the whole gh-*/git-*
# families, radar, watch, read, paste, tree. It disclosed the truncation
# honestly and that did not help, because what was hidden was existence and a
# reader cannot miss what they never learned about.
#
# Not the roster either, which is what this line said for fifteen releases.
# `ops:roster` is ~2.0KB of names plus a safety class, and it answers "does
# this op exist" (#614) but not "is this op the answer" — an error teaches a
# signature only after the decision to call has been made, and a name a reader
# cannot interpret is a capability never reached for. Nothing fails when that
# happens, so the cost was invisible.
#
# Bare `ops` is signatures-only since #1774 and fits at ~4.1KB.
# Whole hook: ~5.1KB against 10,000. That was true all along and this comment
# said so in passing while choosing the roster anyway: the numbers it reasoned
# from were wrong — `ops` was stated at 47,254 (it is 4,126) and the cap at
# 7,168 (it is 10,000, read out of the harness in #2029). #1877 corrected the
# first pair here and the copy in _supertool.py kept them another fifteen
# releases; both are now graded.
#
# The fallback is `ops:session`'s decision, not this script's, because the cap
# constant lives in Python: a shell script measuring a payload it must then
# regenerate is a second place for one decision to go stale.
#
# (Not exact figures: the disclosure names the absolute config path, so what a
# session receives is these plus the length of that path — 30 to 130 bytes,
# depending on where the checkout sits. tests/test_render_size_claims_1877.py
# normalises that path away before grading, so the figures above are
# checkout-independent and the tolerance is rounding room only. It read "they
# move with the checkout, which is why the test grades them with a tolerance
# rather than to the byte" until a 129-char clone made four of those rows red
# with nothing wrong; the tolerance was the first place everyone looked, and it
# was the wrong one.) Descriptions are one call away and richer there:
# `help:OP` carries the full contract, the semantics and a worked example,
# where the listing row carried one line.
LADDER="$(cd "$(dirname "$0")" && pwd)/python-ladder.sh"

# shellcheck disable=SC2329  # invoked indirectly, as supertool_python_each's callback
onboard() {
    supertool_python_identifies "$@" || return 1
    if ! "$@" "$BIN" 'introduction' 'output-format' 'ops:session'; then
        echo "> supertool's op listing is incomplete: the interpreter ran and supertool exited non-zero. The ./supertool wrapper still works; 'ops' prints the listing."
    fi
    exit 0
}

# shellcheck source=hooks/python-ladder.sh
if . "$LADDER" 2>/dev/null; then
    supertool_python_each onboard
    echo "> supertool's op listing is not shown: nothing on PATH identified itself as a Python 3. Tried $SUPERTOOL_LADDER_RUNGS. The bare name python3 is never run, because on Windows and on a stock macOS it can resolve to a stub that blocks instead of erroring (#572, #1382)."
else
    echo "> supertool's op listing is not shown: hooks/python-ladder.sh could not be sourced, so no interpreter was resolved."
fi

# A SessionStart hook that exits non-zero is a broken session, and every path
# above this line has already said what it could not do.
exit 0
