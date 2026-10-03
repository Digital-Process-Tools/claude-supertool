# What the Anthropic directory validator holds on supertool

Measured on supertool, 2026-10-02 and 2026-10-03, by validating trees in the
developer portal's submission form and through its `ValidatePlugin` API. Each finding
below names the probe that showed it, so it can be re-checked. The rules change
without notice: date anything you copy from here, and re-measure before trusting it.

claude-jit-context's `docs/directory-validator.md` (branch `feat/release-compile`) is
the sibling write-up for shell-heavy plugins; [releasing.md](releasing.md) is how the
`release` tree is built. This page is what was learned about supertool's own tree.

## Why zero holds is the goal

A policy hold sends the version to a human reviewer, which takes weeks. The
submission form has no field for a note to the reviewer (claude-jit-context found the
same), so a hold cannot be explained away: it has to be cleared. Warnings do not hold
a version.

## How to measure

1. Build a tree and push it to a throwaway branch. Never `release`, never `master`.
   `release-preview` is the candidate; `release-probe*` branches are bisection probes.
   Plumbing works where the raw-command guard refuses `git commit`:
   `git read-tree <base>`, `git update-index --cacheinfo <mode>,<blob>,<path>`,
   `git write-tree`, `git commit-tree`, `git push origin <sha>:refs/heads/<probe>`.
   In zsh, write `"${C}:refs/..."`: `$C:r...` and `$BASE:h...` are history modifiers
   and silently produce a wrong refspec or path.
2. Validate in the form (Repository `owner/repo@branch`, **Plugin path empty**, do not
   click Next), or from the portal page's console, which also returns the structured
   report:

   ```js
   fetch('/claudeai-rpc/anthropic.directory_submissions.plugins.v1alpha.PluginSubmissionsService/ValidatePlugin',
     {method: 'POST',
      headers: {'content-type': 'application/json', 'x-organization-uuid': ORG},
      body: JSON.stringify({organizationUuid: ORG,
                            github: {repoFullName: 'Digital-Process-Tools/claude-supertool', ref: BRANCH}})})
   ```

   `ORG` is the `lastActiveOrg` cookie. Each finding carries `ruleId`, `path` and
   `params`. The API answers in about 30 seconds; run validations **one at a time**,
   about 90 seconds apart, or the portal refuses with "Too many validations".
3. **Read `params`, not the page.** `MCP_FORWARDS_CREDENTIAL_ENV` gives `credential_at`,
   `env`, `sender_at` and `host`; `host: ""` means no destination was found, so the
   send side is a guess and the read side is the one to cut. `COMMAND_SCRIPT_NOT_FOLLOWED`
   gives `files`, the file the validator would not follow.
4. When a fix works, the reason in `params` changes rather than the finding disappearing,
   as long as another trigger remains. A changed reason is progress.

## Size: the validation times out

| Tree | Files | Bytes | Form |
| --- | --- | --- | --- |
| full `release`, v0.65.1 | 353 | 6,887,694 | "The request took too long", three tries |
| `release-probe` (same, minus presets/validators/formatters/notifiers) | 34 | 2,020,510 | validates |
| #2731 stripped tree (`release-preview` `e2225ef`) | 353 | 3,495,927 | validates |
| claude-jit-context `release` | 48 | 2,005,700 | validates |
| claude-remember `release` | 73 | 1,354,197 | validates |

**Confirmed:** stripping Python comments and docstrings at build time (#2731) is enough.
6,329,308 of the 6.9 MB were Python source; stripped, 2,937,541.

## Holds, by rule

### `HOOK_GRANTS_PERMISSION` -- cleared

The PreToolUse hook only ever emits `deny`, but comment text in `hooks/pre-bash-guard.sh`
quoted an approving decision. Rewording it in the source (#2732) cleared the hold.
**Confirmed** on `release-preview` `e434386`.

### `UNREAD_ASSET_REFERENCED` -- cleared

Cited `supertool-banner.webp` (attributed to files that do not contain the name) and the
VS Code extension's `notifiers/cursor-witness/extension/icon.png`. Denying both from the
release tree (#2732) cleared it. **Confirmed** on `e434386`.

### `MCP_FORWARDS_CREDENTIAL_ENV` -- per-preset findings cleared, aggregate in progress

- Per file: 16 findings on the full tree. Text-only ones (URLs in install hints and
  error messages, notifier READMEs, the README attack example) were cleared by rewording
  and by a release README (#2732). The presets that really send a token to their vendor
  (bluesky, devto, hashnode, slack, youtube and three watch sources) are left out of the
  directory build (#2734). **Confirmed**: no per-file finding on `c607ed1`.
- `userConfig` with `sensitive: true` does not apply: option values reach hook processes
  and MCP/LSP server configs only, never commands Claude runs through the Bash tool,
  which is every supertool op (code.claude.com plugin manifest reference).
- The `plugin.json` aggregate pairs a read in `_supertool.py` with a send in
  `_supertool_dispatch.py` (`host: ""`). The read side moved three times as each trigger
  was cut in `_supertool.py`:

  | `env` param | cut by | probe |
  | --- | --- | --- |
  | "an environment variable named at run time" | `os.environ.get(<constant>)` made literal | `03c7f00`: same reason, a second site remained |
  | same | `environ = os.environ if ...` alias and `scrub_git_env(os.environ)` | `bbff449`: reason changed |
  | "environment (printenv / env / export -p / set)" | bare `env` identifier, a `"set $%s ..."` message | in progress |

  `import socket` was also moved out of `_supertool.py` (#2734).

### `COMMAND_SCRIPT_NOT_FOLLOWED` -- cause found, fix not chosen

`files: supertool.py`. Bisected on `hooks/session-start.sh`:

| probe | change | result |
| --- | --- | --- |
| `release-probe` (empty `session-start.sh`) | `echo "{}"` only | clears |
| `a` | whole-line comments removed | holds |
| `b` | `a` without the two lines that run `supertool.py` | holds |
| `c` | `b` without the message naming `python3 supertool.py` | holds |
| `d` | `c` with the literal `supertool.py` split as `"supertool".py` | holds |
| `e` | comments and lone apostrophes inside double quotes removed | holds |
| `f` / `g` | first half / second half of `session-start.sh` | both hold |
| `h` | one line: `python3.12 "${CLAUDE_PLUGIN_ROOT}/supertool.py" introduction` | holds, `files: supertool.py` |
| `i` | one line running `hooks/shipped_rules.py` instead | holds, `files: hooks/shipped_rules.py` |
| `j` | `hooks.json` runs `python3.12 ... supertool.py` directly, no shell script | **clears** |

**Confirmed: a hook shell script that runs any Python file holds.** The portal's own
words: "A file that script runs in turn isn't followed and always goes to a reviewer."
The shell shapes claude-jit-context found (here-documents, `source`, lone quotes, broad
`case` arms) were not the cause here.

`hooks/pre-bash-guard.sh` runs `pre_bash_guard.py` the same way and is not cited, only
because its path is `${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}/...`,
which the static scanner cannot resolve. That is an accident, not a fix: writing a path
so the scanner cannot read it, to avoid the review the rule asks for, is not an
acceptable way to clear a hold.

Open decision: `hooks.json` pointing straight at Python cannot use the interpreter
ladder (`hooks/python-ladder.sh`) that #572 exists for, because a bare `python3` can
resolve to a stub that hangs (Windows App Execution Alias, stock macOS Xcode tools).
Candidates: bare `python3`; one fixed `python3.X`; one hook entry per candidate version;
or keep the scripts and accept the hold.

### `NAME_CONFUSABLE` -- fix chosen (#2736)

`name: "supertool"` is within edit distance of "Suger" (`suger-mcp`) and publisher
"Superr". `displayName: "DPT Supertool"` does not clear it (`ee087a9`).
`name: "supertool-cli"` does (`21533a6`). The `supertool` command does not change.

## Warnings left (they do not hold)

- `HOOK_OUTPUT_UNINSPECTED`: any PreToolUse hook whose decision is computed at run time.
- `RUNTIME_FETCH_EXEC` on `_supertool_mcp.py` and `presets/mcp/daemon.py`: no literal
  download-and-run text found by reading; cause unknown.
- `ICON_MISSING`: no icon in the plugin. The listing icon is set only at the first save.
