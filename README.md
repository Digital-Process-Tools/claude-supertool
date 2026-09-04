<p align="center">
  <img src="supertool-banner.webp" alt="SuperTool — cut your Claude Code bill by 50%" width="900">
</p>

# supertool

> **Cut your Claude Code bill by 50%.**
> `git-status`, but it tells you what to do next.

[![Tests](https://github.com/Digital-Process-Tools/claude-supertool/actions/workflows/tests.yml/badge.svg)](https://github.com/Digital-Process-Tools/claude-supertool/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![OS](https://img.shields.io/badge/tested%20on-Linux%20%7C%20macOS%20%7C%20Windows-blue)](https://github.com/Digital-Process-Tools/claude-supertool/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/license-Community-brightgreen)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.56.0-orange)](.claude-plugin/plugin.json)

Saves tokens. Saves money. Saves turns. Works the same in interactive sessions and autonomous runs — humans pair-programming with Claude Code use it every day, not just Kevin-style headless agents. Stdlib only, zero deps, Python 3.9+ — a thin launcher (`supertool.py`) delegating to one core module (`_supertool.py`) plus the presets, [validators](docs/validators.md), [formatters](docs/formatters.md) and [notifiers](docs/notifiers.md) you enable per repo.

```bash
# 7 ops, 1 round-trip, parallel where safe
supertool 'read:src/Module.py' 'read:src/Auth.py' 'grep:TODO:src/:20' 'map:src/'
```

---

## From the same workshop

Four plugins, one team, each does one thing. This one and three siblings:

- [claude-remember](https://github.com/Digital-Process-Tools/claude-remember): memory across sessions. Saves, compresses through Haiku, reloads at the next start.
- [claude-jit-context](https://github.com/Digital-Process-Tools/claude-jit-context): project knowledge that loads only when the prompt, the file or the tool matches it.
- [claude-oss](https://github.com/Digital-Process-Tools/claude-oss): the maintainer loop that runs these four repos. Triage, build, review, merge, release.

All four install from one marketplace: `/plugin marketplace add Digital-Process-Tools/claude-marketplace`.

## Why supertool

Claude Code's default toolbelt is 1995 unix: `cat` one file, `grep` one pattern, `git status` returns 200 bytes of porcelain, and every tool call re-sends the whole conversation cache at 10% of input price. Supertool ships variants that pack the *next question* into the *current call*, so a round-trip bought once does not get bought again.

- **`git-status`** — branch, ahead/behind, dirty files, open MR/PR, suggested next step, in one call — every untracked path carrying its own write time so a stray file another process dropped never passes for one you made yourself. [Details](docs/presets/git.md#every-untracked-path-carries-its-write-time).
- **`gh-pr` / `gl-mr`** — full PR/MR dashboard: branch, checks, reviews, diff stat, comments, replacing 4-5 raw `gh`/`glab` calls, with a summed check tally that never collapses "not all green" into "clean". [Details](docs/presets/github.md#zero-check-runs-is-four-states-not-one).
- **`git-worktrees`** — is an agent already working in this worktree? Branch, occupancy and merge state for every worktree, in three states rather than a guess. [Details](docs/presets/git.md#occupancy-has-three-states-and-idle-is-the-one-that-must-be-earned).
- **`gh-job` / `gh-run` / `gh-branch`** — a job's failure detail from either GitHub id namespace, a run's job table under a header that sums it, and *is this branch green* answered for the ref that has no PR after a squash merge. [Details](docs/presets/github.md#two-id-namespaces-actions-jobs-and-check-runs).
- **`gh-prs` / `gh-issues` / `gl-mrs`** — triage boards that rank the queue instead of listing it, and say plainly which population is on screen. [Details](docs/presets/github.md#gh-prs-says-whose-board-it-is).
- **`gh-pr-create` / `gh-pr-merge` / `gh-pr-edit`** — open, merge and correct a pull request with a receipt that proves it landed, not just an exit code. [Details](docs/presets/github.md#gh-pr-merge-refuses-more-than-it-merges).
- **`gh-issue-create` / `gh-issue-comment`** — file and comment with the same published-body read-back, falling back to REST on a GraphQL outage rather than silently duplicating a filing. [Details](docs/presets/github.md#a-graphql-outage-falls-back-to-rest-and-the-receipt-names-which-transport-wrote-it).
- **`claims:PATH`** — does a doc's own references — op names, paths, line numbers, cited issues — still hold? [Details](docs/presets/claims.md).
- **`plugin-marketplace`** — did a release actually reach anyone installed through the catalogue, or is the pinned commit stale? [Details](docs/presets/plugin-marketplace.md).
- **`classify:TEXT`** — is this untrusted text trying to steer an agent? [Details](docs/presets/classify.md).

That's a sample — supertool ships ~40 ops out of the box (built-ins plus the `git` / `github` / `gitlab` / `claude-log` presets); add your own and you're past 60 fast. The full pitch, the receipt behind "50%", and why the tool exists at all: [docs/philosophy.md](docs/philosophy.md).

## Install

From the DPT marketplace:

```
/plugin marketplace add Digital-Process-Tools/claude-marketplace
/plugin install supertool@dpt-plugins
```

This auto-registers `hooks/session-start.sh` via the plugin's `hooks/hooks.json` — no manual `settings.json` editing. **Restart your Claude Code session afterwards**: the hook only fires at session start, so a session already running when you install it does not pick it up mid-conversation.

Standalone install (clone + symlink onto `$PATH`), the wrapper's caveats (a `cd` breaks `./supertool`, a git worktree starts without one), and every configuration key: [docs/configuration.md](docs/configuration.md).

## What a call looks like

```
$ supertool 'git-status'
--- git-status ---
PASS (0.29s)
# git-status
Branch: fix/142
vs master: 1 ahead

## Last 5 commits
  2c6b4d8 2026-09-02 A. Dev | wip: add config flag
  a913990 2026-09-02 A. Dev | add module
  fea74b2 2026-09-02 A. Dev | init

## Working tree (2 changes)

### Untracked (2)
  (write time per path — nothing on disk records who wrote a file, so this is a time and not a verdict; #1724)
  .supertool.json  (written 0s ago — inside the 15m activity window)
  scratch_debug.py  (written 4s ago — inside the 15m activity window)
```

One call answers branch, ahead/behind, recent history and every dirty path — with the write time that tells your own edits apart from a stray file another process left behind.

## Ops

~40 built-in and `git`/`github`/`gitlab` ops out of the box. Colon-CLI is the default (`read:PATH:OFFSET:LIMIT`); for content containing `:` use the `@file` payload route instead (`grep:@-`, `edit:@FILE`) — full grammar: [docs/input-forms.md](docs/input-forms.md). Full op reference with syntax and examples: [docs/operations/index.md](docs/operations/index.md) (built-ins), [docs/presets/index.md](docs/presets/index.md) (every preset, including the ones not in this table).

| Op | What it does |
|----|--------------|
| `read` / `grep` / `glob` / `tree` / `map` | Read, search and symbol-map files — batched, with auto-read on a single matching file |
| `edit` / `replace` / `replace_lines` / `paste` / `append` / `vim` / `batch` | Mutating ops, each validated and rolled back on a syntax failure |
| `validate` / `format` / `validate_staged` / `format_staged` | Run the registered validators/formatters for a path, standalone or on the staged diff — three-state (`ok` / finding / `skipped`), and a mutating op rolls back on a validator failure. [Details](docs/validators.md), [docs/formatters.md](docs/formatters.md) |
| `cwd` / `repo` | Set the directory a call resolves against, or name the repo it is *about* |
| `ops` / `ops:roster` / `help:OP` / `registry` / `guard` / `doctor` / `init` / `gc` | Discover, inspect and maintain the tool itself |
| `workspace` / `resolve` / `diag` / `hover` / `rename` | LSP-backed ops via the warm MCP daemon. [Details](docs/presets/lsp.md), [docs/mcp-integration.md](docs/mcp-integration.md) |
| `git-status` | Branch, ahead/behind, dirty files, open PR/MR, suggested next step |
| `git-worktrees` | Occupancy, tracker and merge state for every worktree, none of it guessed |
| `git-commit` / `git-push` / `git-diff` / `git-blame` / `git-conflicts` / `git-resolve` | Commit with a receipt, push with a watcher, diff/blame/resolve without raw `git` |
| `gh-pr` / `gh-pr-create` / `gh-pr-merge` / `gh-pr-edit` | Full PR dashboard, create, merge-with-proof, and correct a published body |
| `gh-issue` / `gh-issue-create` / `gh-issue-comment` | Issue dashboard, file and comment with a read-back |
| `gh-prs` / `gh-issues` | Triage boards, ranked and stating which population is on screen |
| `gh-job` / `gh-run` / `gh-branch` / `gh-check` | Job/run/branch/check-run detail across both GitHub id namespaces |
| `gh-labels` | The repo's label vocabulary and open-issue counts per label |
| `gl-mr` / `gl-mrs` / `gl-pipeline` / `gl-job` / `gl-api` | GitLab's equivalents |
| `watch` / `radar` / `channel` | Background event pollers, tier reconciliation, and the MCP bridge that wakes a session. [Details](docs/presets/watch.md) |
| `claims` | Does a doc's own references — ops, paths, line numbers, cited issues — still hold |
| `classify` | Is this untrusted text trying to steer an agent |
| `plugin-marketplace` | Did a release reach anyone installed through the catalogue |

## Beyond the ops table

Five subsystems the table above only gestures at, each with its own doc:

- **Validators & formatters** — every mutating op runs your project's registered linters/formatters first, three-state (`ok` / finding / `skipped`), and rolls a write back on a validator failure. [docs/validators.md](docs/validators.md), [docs/formatters.md](docs/formatters.md).
- **Notifiers** — fire-and-forget observers that tap the op stream for side effects: an editor diff view, Slack, a desktop notification. [docs/notifiers.md](docs/notifiers.md).
- **`watch` / `radar` / `channel`** — background pollers and async wake for PRs, MRs and pipelines, reconciled into one tier. [docs/presets/watch.md](docs/presets/watch.md). This checkout's own `.claude/settings.json` disables the tracked `.mcp.json` server (`disabledMcpjsonServers`) so it does not race `oss-workspace`'s local-scope one for the same socket — see the doc's "collision" section if `channel:health` reports `CANNOT DETERMINE`.
- **LSP ops via a warm MCP daemon** — `workspace`/`resolve`/`diag`/`hover`/`rename` reach a language server through a process that stays hot across calls. [docs/presets/lsp.md](docs/presets/lsp.md), [docs/mcp-integration.md](docs/mcp-integration.md).
- **Warm-process MCP servers for heavy tools** — the same warm-daemon pattern, applied to PHP toolchains (Rector, PHPUnit) as validator adapters that stay bootstrapped across calls. [docs/mcp-warm-process-servers.md](docs/mcp-warm-process-servers.md).

## Security — cwd containment

Every path argument is checked against the current working directory; `~` is expanded before the check, symlinks crossing the boundary are caught, and a malicious `.supertool.json` or a prompt-injected `paste:~/.ssh/authorized_keys:::pwned` is refused rather than run. Opt out per-call (`SUPERTOOL_ALLOW_OUTSIDE_CWD=1`) or per-project (`"allow_outside_cwd": true`). Vim shell verbs (`:!`, `:%!`, `:r !`) are disabled by default for the same reason. Full threat model: [issue #146](https://github.com/Digital-Process-Tools/claude-supertool/issues/146) and [issue #147](https://github.com/Digital-Process-Tools/claude-supertool/issues/147); config keys and defaults: [docs/configuration.md](docs/configuration.md).

## The raw-command guard

Installed with the plugin, on by default: a `PreToolUse` hook refuses any `Bash` command an op declares it replaces, quoting the op's own description (`gh pr view` → `gh-pr`, `git push` → `git-push`, …). **It governs one route** — the hook matches `Bash|PowerShell` only, so Claude Code's own `Edit`, `Write`, `MultiEdit` and `NotebookEdit` write to disk without passing it, with no op, no validator and no rollback ([#1671](https://github.com/Digital-Process-Tools/claude-supertool/issues/1671)). Full mechanism, the shipped rule layer beneath the registry, and what a command that could not be read does (declines and allows, never blocks blind): [docs/configuration.md](docs/configuration.md#raw_command_guard--the-shipped-raw-command-block).

### Hard-block native tools (optional)

Closing the `Edit`/`Write` route is an operator decision the plugin cannot make for you. If you want to force the model to batch via supertool — typical for autonomous / Kevin-style runs — block the competing tools at the Claude Code layer.

**Settings (interactive sessions):** add a `permissions.deny` block to `.claude/settings.json`:

```json
{
  "permissions": {
    "deny": ["Grep", "Glob", "LS", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash(find:*)", "Bash(cat:*)", "Bash(grep:*)", "Bash(ls:*)", "Bash(sed:*)", "Bash(awk:*)", "Bash(tail:*)", "Bash(head:*)"]
  }
}
```

**CLI flag (`claude -p` bypass mode):**

```bash
claude -p "..." --permission-mode bypassPermissions \
  --disallowedTools "Grep,Glob,LS,Edit,Write,MultiEdit,NotebookEdit,Bash(find:*),Bash(cat:*),Bash(grep:*),Bash(ls:*),Bash(sed:*),Bash(awk:*),Bash(tail:*),Bash(head:*)"
```

`--allowedTools` is [ignored in bypass mode](https://github.com/anthropics/claude-code/issues/12232) — always use `--disallowedTools` when bypassing.

### Ask before you block, and the only way off

Ask what a command will do without running it: `supertool 'guard:COMMAND'`. Turn the whole gate off for a project with `"raw_command_guard": false` in `.supertool.json` — the only way off, since an environment-variable escape hatch is one line an agent learns once and prepends forever.

## Platform compatibility

**Linux/macOS:** works out of the box. **Windows:** works via Git Bash or WSL — native `cmd.exe`/PowerShell without bash won't fire either plugin hook. `hooks/guard-selftest.py` reports `enforcing`, `could not run` or `nothing to test` from the host itself, without needing a shell to check it (`py -3 hooks/guard-selftest.py`). Drive letters, paths with spaces, the raw-command guard on a bash-less host, and the session-start hook's own gap: [docs/configuration.md](docs/configuration.md#windows-and-macoslinux-platform-notes).

## Design decisions

- **Two files, one of them a shim.** `supertool.py` is the entry point everything invokes and is 171 lines; the tool itself is `_supertool.py` beside it. The split exists so CPython caches the bytecode: a script named on the command line is recompiled from source on every run, an imported module is not, and that recompile measured ~145ms per invocation on ubuntu and windows runners ([#931](https://github.com/Digital-Process-Tools/claude-supertool/issues/931)). Still no package layout, no required deps — clone or `pip install`, both work.

More design calls (Python floor, why not an MCP server, trading Python work for LLM tokens): [docs/design-decisions.md](docs/design-decisions.md).

## Contributing

See [docs/contributing.md](docs/contributing.md) — custom ops, presets, validators, running tests, submitting upstream. Who maintains this repo and how: [docs/philosophy.md](docs/philosophy.md#how-this-repo-is-maintained).

## License

[Community License](LICENSE) — free for personal, educational, and internal business use. © 2026 Digital Process Tools.
