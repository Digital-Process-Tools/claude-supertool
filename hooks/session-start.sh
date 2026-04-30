#!/bin/bash
# SessionStart hook — creates ./supertool symlink and outputs
# self-documentation from .supertool.json for LLM onboarding.

# Create ./supertool symlink so the model can call it from any project
if [ ! -e "./supertool" ]; then
    ln -sf "${CLAUDE_PLUGIN_ROOT}/supertool.py" "./supertool" 2>/dev/null
fi

# Output self-documentation from .supertool.json (fallback if no config).
# Use 'ops-compact' to drop redundant examples and stay closer to the harness's
# hook-stdout cap (~2KB). The compact view prepends a warning if the output
# still exceeds the cap, so the model can detect truncation and fetch the full
# listing on demand via `./supertool 'ops'`.
./supertool 'introduction' 'output-format' 'ops-compact'
