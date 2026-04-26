#!/bin/bash
# SessionStart hook — creates ./supertool symlink and outputs
# self-documentation from .supertool.json for LLM onboarding.

# Create ./supertool symlink so the model can call it from any project
if [ ! -e "./supertool" ]; then
    ln -sf "${CLAUDE_PLUGIN_ROOT}/supertool.py" "./supertool" 2>/dev/null
fi

# Output self-documentation from .supertool.json (fallback if no config)
./supertool 'introduction' 'output-format' 'ops'
