# `oss` preset (#1985)

```
supertool 'oss-tick'
```

Requires `git`, and Claude Code's own `oss` plugin (`Digital-Process-Tools/claude-oss`) installed. Runs `git fetch` and, if that succeeds, `git pull --ff-only`. Runs from the current working directory: the git-excluded `.oss.local.json` beside `.oss.json` there names the state file the state-file rows read -- `.oss.json` itself is never read directly by this shim.

## The question

`claude-oss` declares supertool as a dependency and blocks its own rule layer's Read/Edit/Write/Glob/Grep in favour of ops -- so the dependency is already paid for, and a plain helper script would make the maintainer loop's own tick entry the one thing in that plugin that does not go through supertool. The board read a tick opens with already is `supertool 'gh-prs' 'gh-issues' 'gh-branch' 'git-worktrees'`; `oss-tick` joins that pattern as one more op rather than a second `Bash` call.

## Why a shim, not a port

`_find_preset_file` only ever searches the project's own `presets/`, `~/.config/supertool/presets/` or supertool's own install directory -- none of them is `${CLAUDE_PLUGIN_ROOT}`, so "the oss plugin ships this preset itself" has no spelling that avoids a copy. Copying the tick's logic into this repository instead would mean two places for one decision to drift, the same class `claude-oss` has ten consecutive recorded instances of for its own `.oss/statusline.py` template.

So `oss-tick` is a **shim**: `presets/oss/shim.py` locates the installed `oss` plugin the same way `claude-oss`'s own `doctor.py:dependency_install_roots` resolves *its* dependencies -- Claude Code's install record (`~/.claude/plugins/installed_plugins.json`) for the active version, then that record's own `installPath`, falling back to a cache-directory glob for a record that predates the field. `presets/oss/tick.py` then execs that plugin's own `oss_state.py` for the state-file rows. No maintainer-loop judgment lives in this repository; a plugin that cannot be resolved is reported as `could-not-resolve` rather than silently skipped.

## What it returns

One receipt, every row three-state, never two -- a step that is skipped and a step that found nothing must never render alike:

| Row | States |
| --- | --- |
| plugin identity | `resolved <version>` / `resolved, but its install carries no scripts/ directory` / `could-not-resolve` |
| last state entry | entry / `no entries yet` / `FAIL` naming what is wrong |
| pending wait | `cleared` / `holds` / `could-not-evaluate` |
| plugin identity vs last recorded | `unchanged` / `changed` / `could-not-tell` / `route-mismatch` |
| `git fetch && git pull --ff-only` | result / `could-not-run` |
| board (gh-prs, gh-issues, gh-branch, git-worktrees) | `read` / `unread`, per op |
| radar tier resolution | `not-configured` / `registered` / `probe-did-not-answer` |

Then a `NEXT:` line chosen by those findings rather than by prose.

## What this does not do

It enforces nothing. The raw-command guard hooks `Bash` only, a harness `Edit`/`Write` bypasses every op, and the `Bash` grant handed to agents is total. An op is a name, a timeout and a declared safety class -- not a boundary.

A `.oss.local.json` missing from the current worktree (it is machine-local and git-excluded, so a fresh worktree cut from a clone that has one does not automatically carry a copy) is a real `could-not-evaluate` on the three state-file rows, never a crash and never a silent pass.

An `oss_release` op is *not* proposed here -- see the issue for why the release case is not measured the way the tick case is.
