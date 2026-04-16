---
name: supertool
description: "Toggle SuperTool enforcement for the current user. When enforced, Grep/Glob/LS and Bash fallbacks (cat/find/grep/ls/sed/awk/tail/head) are blocked and redirected to ./SuperTool — forcing batched file ops and one-round-trip behavior. Use /supertool on for autonomous/Kevin-style runs, /supertool off for interactive sessions where ad-hoc tool use is fine."
---

# SuperTool Toggle

Controls whether SuperTool enforcement is active. Enforcement makes the
pre-tool-block hook reject competing tools (Grep, Glob, LS, Bash fallbacks),
forcing the model to batch via `./SuperTool`.

State lives in `~/.claude/supertool-enforced` (empty file = enforced; absent
= permissive).

## Usage

- `/supertool on`     — enable enforcement (block Grep/Glob/LS + Bash fallbacks)
- `/supertool off`    — disable enforcement (all tools allowed again)
- `/supertool status` — show current state

## Steps

### `on`

```bash
mkdir -p "$HOME/.claude"
touch "$HOME/.claude/supertool-enforced"
echo "SuperTool enforcement: ON"
echo "Blocking: Grep, Glob, LS, Bash(cat|find|grep|ls|sed|awk|tail|head)"
echo "Use ./SuperTool for all reads and searches. Disable with /supertool off"
```

### `off`

```bash
rm -f "$HOME/.claude/supertool-enforced"
echo "SuperTool enforcement: OFF"
echo "All tools allowed. SuperTool still available (./SuperTool ...)"
```

### `status`

```bash
if [ -f "$HOME/.claude/supertool-enforced" ]; then
    echo "SuperTool enforcement: ON"
    echo "State file: $HOME/.claude/supertool-enforced (present)"
else
    echo "SuperTool enforcement: OFF"
    echo "State file: $HOME/.claude/supertool-enforced (absent)"
fi
```

## When to use each mode

**`on` (enforced) — for autonomous / headless / Kevin-style work**

Every file op goes through SuperTool. Model can't fall back to `find`
or `grep` via Bash. Maximizes batching, saves round-trips, lowers
Max-plan quota consumption.

**`off` (permissive) — default for interactive sessions**

All tools available. SuperTool still works if you want it, but nothing
blocks `grep -r` or `find . -name` when those genuinely fit. Less
friction for ad-hoc exploration.

## Related

- `./SuperTool` — the batched-ops binary (stays available in both modes)
- `/tmp/supertool-calls.log` — per-call log for adoption analysis
- `hooks/session-start.sh` — injects batching prompt at session start
- `hooks/pre-tool-block.sh` — the enforcement hook itself
