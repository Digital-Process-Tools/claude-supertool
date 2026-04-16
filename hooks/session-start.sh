#!/bin/bash
# SessionStart hook — injects the batching prompt into context.
# Enable via .claude/settings.json:
#   "hooks": { "SessionStart": [{ "hooks": [{ "type": "command",
#     "command": "$CLAUDE_PLUGIN_ROOT/hooks/session-start.sh" }] }] }

cat <<'EOF'
## SuperTool — batched file operations

`./SuperTool` collapses N file ops into one Bash round-trip. Each saved
round-trip reduces output tokens (not cached) and cuts wall-time latency.
**Six or seven ops per call is routine; two is too few.**

Realistic batch (7 ops, 1 round-trip):

    ./SuperTool \
        read:src/Module.py \
        read:src/Permissions.py \
        read:src/Options.py \
        grep:extends:src/:20 \
        grep:@related:src/:10 \
        glob:src/Components/**/*.xml \
        glob:src/EventsManagers/*.py

Operations: read:PATH[:OFFSET:LIMIT] · grep:PATTERN:PATH[:LIMIT] ·
glob:PATTERN (supports **) · ls:PATH · tail:PATH:N · head:PATH:N

**Anti-patterns — each wastes a round-trip:**

- `glob:concrete/path.xml` then `read:concrete/path.xml` — glob without
  wildcards is useless; just `read:`. (The tool auto-reads here, but you
  still burned a turn thinking about it.)
- `grep:FOO:single_file.py` then `read:single_file.py` — same file, two
  turns. (Auto-read on grep handles it, but again: one turn wasted.)
- Any second SuperTool call whose ops could have fit in the first.

**Self-check:** if you see `[auto-read: ...]` in output, SuperTool just
salvaged a wasted turn you asked for. Batch up front next time.
EOF
