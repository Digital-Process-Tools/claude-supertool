# Where supertool stands with the Anthropic directory validator

What the validator shapes are, how each was proven and how to probe without getting
rate-limited is shared by every DPT plugin and lives in one place:
`~/Documents/claude-directory-publishing` (`triggers.md`, `method.md`, `tools/`). Read
it before probing, and write a new finding there, not here. This page keeps only
supertool's own state.

## Goal

Zero policy holds. The submission form has no field for a note to the reviewer, so a
hold means weeks of manual review rather than an explanation.

## State, 2026-10-05

`release-preview` @ 181177a, built from #2741 with `build_release_tree.py`, validated
through the portal's ValidatePlugin call (directory-lints/2026-09-30.165): **0 policy
holds, 0 blocks.** Warnings: 4 `UNKNOWN_KEY` (the listing URLs in `plugin.json`,
expected), `ASSETS_PASSED_UNREAD` (the icon), `SUBMISSION_EXISTS`.

## Holds, and what cleared each

| Hold | Cleared by | Where |
| --- | --- | --- |
| validation timeout (size) | Python comments and docstrings stripped at build time, 6.9 MB to 3.5 MB | #2731 |
| `HOOK_GRANTS_PERMISSION` | comment text in `hooks/pre-bash-guard.sh` | #2732 |
| `UNREAD_ASSET_REFERENCED` | banner and VS Code extension icon out of the release tree | #2732 |
| `MCP_FORWARDS_CREDENTIAL_ENV`, per preset | token-sending presets, the watch family and the channel notifier out of the directory build | #2734 |
| `MCP_FORWARDS_CREDENTIAL_ENV`, `plugin.json` aggregate | twelve passes over the shipped text: every environment read literal-keyed, no bare `env` word, credential paths spelled apart | #2734 |
| `COMMAND_SCRIPT_NOT_FOLLOWED` | the release build swaps in `hooks/hooks.release.json`: Python run straight from `hooks.json`, no `.sh` | #2734 |
| `RUNTIME_FETCH_EXEC` (warning) | `MCPClient.spawn` renamed `connect` | #2734 |
| `NAME_CONFUSABLE` | plugin renamed `supertool-cli` | #2736 |
| `ICON_MISSING` (warning) | `.claude-plugin/icon.png`; unlike jit-context it raised no `UNREAD_ASSET_REFERENCED` | #2741 |

## How the release hooks start Python

`hooks.release.json` chains `python3.14` down to `python3.9`, then `py -3`, with `||`,
and never runs the bare `python3` (#572: it can be a stub that blocks). Both hook entry
points always exit 0, so a guard that ran is never run again by the next rung. The
committed tree keeps the `.sh` hooks and the ladder for local development.
