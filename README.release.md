# supertool

Cuts your Claude Code bill by batching file, git and tracker operations into one
round-trip instead of many. Standard library only, no third-party dependencies.

## What it does

Claude Code's default tools read one file, grep one pattern, or print a bare git
status, and every tool call re-sends the whole conversation cache. supertool
ships operations that pack the next question into the current call: a status
check that already names the suggested next step, a pull request dashboard in
one call instead of several, a batched read of several files and a search in
one round-trip.

Roughly forty operations ship out of the box, covering files, git, GitHub,
GitLab and Claude Code's own session log; presets add more per project.

## Install

```
/plugin marketplace add Digital-Process-Tools/claude-marketplace
/plugin install supertool@dpt-plugins
```

## A few of the operations

- a status check that already names the suggested next step
- a full pull request or merge request dashboard in one call
- several file reads and a search batched into one round-trip
- a worktree census: which one is occupied, by what, and its merge state
- a plain-language check for text that looks like it is trying to steer an
  agent, before anything acts on it

## Full documentation

The complete command reference, every preset and the project's own writing
on why it exists:
[the full README](https://github.com/Digital-Process-Tools/claude-supertool#readme).
