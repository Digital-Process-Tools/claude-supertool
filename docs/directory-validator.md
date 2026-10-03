# Where supertool stands with the Anthropic directory validator

What the validator shapes are, how each was proven and how to probe without getting
rate-limited is shared by every DPT plugin and lives in one place:
`~/Documents/claude-directory-publishing` (`triggers.md`, `method.md`, `tools/`). Read
it before probing, and write a new finding there, not here. This page keeps only
supertool's own state, measured 2026-10-03.

## Goal

Zero policy holds. The submission form has no field for a note to the reviewer, so a
hold means weeks of manual review rather than an explanation.

## Holds, and what handles each

| Hold | State | Where |
| --- | --- | --- |
| validation timeout (size) | cleared: Python comments and docstrings stripped at build time, 6.9 MB to 3.5 MB | #2731 |
| `HOOK_GRANTS_PERMISSION` | cleared: comment text in `hooks/pre-bash-guard.sh` | #2732 |
| `UNREAD_ASSET_REFERENCED` | cleared: banner and VS Code extension icon out of the release tree | #2732 |
| `MCP_FORWARDS_CREDENTIAL_ENV`, per preset | cleared: token-sending presets out of the directory build | #2734 |
| `MCP_FORWARDS_CREDENTIAL_ENV`, `plugin.json` aggregate | in progress: the read side walks file by file (`_supertool.py` cleared, `_supertool_catalog.py` next) | #2734 |
| `COMMAND_SCRIPT_NOT_FOLLOWED` (`supertool.py`) | cause found, fix not chosen (below) | -- |
| `NAME_CONFUSABLE` | fix chosen: rename to `supertool-cli` | #2736 |

## The open decision: how hooks start Python

A hook shell script that runs any Python file holds (`COMMAND_SCRIPT_NOT_FOLLOWED`);
`hooks.json` pointing straight at the Python clears it. But `hooks.json` cannot run the
interpreter ladder (`hooks/python-ladder.sh`) that #572 exists for: a bare `python3` can
resolve to a stub that hangs (Windows App Execution Alias, stock macOS Xcode tools).
Candidates: bare `python3`; one fixed `python3.X`; one hook entry per candidate version;
or keep the scripts and accept the hold.

`hooks/pre-bash-guard.sh` runs `pre_bash_guard.py` the same way and is not cited only
because the scanner cannot resolve its path. That is not a fix, and the same trick must
not be used to clear `session-start.sh`: hiding a file from the scanner to avoid the
review the rule asks for is the wrong way to clear a hold.

## Warnings left (they do not hold)

`HOOK_OUTPUT_UNINSPECTED` (any PreToolUse hook that decides at run time),
`RUNTIME_FETCH_EXEC` on `_supertool_mcp.py` and `presets/mcp/daemon.py` (cause unknown),
`ICON_MISSING`.
