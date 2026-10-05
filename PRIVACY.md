# Privacy

Last updated: 2026-10-05

supertool is a command-line tool that runs on your own computer as a Claude Code plugin. Digital Process Tools does not operate a server for it, and supertool collects no telemetry, analytics or usage statistics. Nothing is sent to Digital Process Tools.

This page describes the build published in the Anthropic plugin directory. The full build installed from the Digital Process Tools marketplace (dpt-plugins) also includes optional publishing presets (Bluesky, dev.to, Hashnode, Slack, YouTube) and a background watcher; those presets call their own vendor's API with your own token, and they are not part of the directory build or of this page.

Everything supertool prints is returned to your Claude Code session. Claude Code sends the conversation, including that output, to Anthropic as it does for any other tool. That is Claude Code's behaviour, covered by Anthropic's own policies, and not something supertool adds.

## What it reads

- **Files in your project**, when you or Claude run an op that reads them (`read`, `grep`, `glob`, `map` and so on).
- **Configuration**: the nearest `.supertool.json` found by walking up from the current directory, any preset files in the project's `presets/` folder, and preset files in `~/.config/supertool/presets/`.
- **Git**: your repository's history, status, branches and remotes, through the `git` command, when you run a git op.
- **The shell command Claude is about to run.** The plugin installs a check that runs before every Bash command in Claude Code. It reads the command text and your supertool configuration to decide whether a supertool op should be used instead. It reads nothing else and sends nothing anywhere.
- **Claude Code session transcripts**, only when you run one of the `claude-log` ops. These read the session files Claude Code keeps for the current project under `~/.claude/projects/`. Values that match known secret patterns are masked in the output unless you ask for the raw form.
- **Claude Code's plugin install record** (`~/.claude/plugins/installed_plugins.json`), only when you run the `oss-tick` op, to find the separately installed `oss` plugin and run its scripts.
- **GitHub and GitLab data**, only when you run a `gh-*` or `gl-*` op (see below).

Ops beyond the built-in file ops are off until a project's `.supertool.json` enables them. The `init` op previews such a file and writes it only when you run `init:write`.

## What it writes

### In your project

- The files you or Claude change through a write op (`paste`, `edit`, `vim`, `replace`, `append`, `rename`, `format` and similar).
- A `supertool` shortcut (a symbolic link to the plugin's own entry point) in the directory where a Claude Code session starts. This is done by the plugin's session-start hook, without asking. If something named `supertool` that is not this plugin's link is already there, it is left untouched.

### In your user cache directory (normally `~/.cache/supertool/`)

- `paste-backup/`: the previous contents of a file that `paste` overwrote completely.
- `vim-undo/` and `vim-cursor/`: the content of a file before its last `vim` edit, and cursor positions, so a later call can undo or continue.
- `validators/`: cached results of validators you have configured, keyed by a hash of the file's content.
- `read-elide/`: small markers (a hash, a time and a size) that let a repeated read of an unchanged file be shortened.
- `statusline/`: the last check summary `gh-pr` saw, for the optional status line.
- `.cache_key`: a random value used to sign the validator cache.
- `.gc-stamp`: when the cache was last pruned.
- If you configure language servers, their sockets, process ids and logs go in a per-user runtime directory (on macOS `~/Library/Caches/supertool/mcp/`, elsewhere usually under `~/.cache/supertool/mcp/`).

### In your system's temporary directory

- `supertool-calls.log`: one line per supertool call, with the time, your login name, a process id, how Claude Code was started (its entrypoint value, such as `cli`), the number of ops, the output size, and **the full command arguments as typed**. Text passed inline to a write op (for example the content of a `paste`) is therefore recorded here. Content passed on standard input (the `@-` form) is not. This file is written on every call, including the calls the session-start hook makes.
- `supertool-images-<user id>-gh/` and `supertool-images-<user id>/`: images attached to GitHub or GitLab issues, downloaded when you read an issue with `gh-issue` or `gl-issue`.
- `supertool-images-<user id>-traces/`: full CI job logs, when you ask the GitLab job or pipeline ops to save them to disk.
- `supertool-classify-cache-<user id>/`: the verdict of the content classifier described below, keyed by a hash of the text. The text itself is not stored.
- Short-lived files that are removed after use: an issue or pull-request body handed to the GitHub CLI, a validator's report, and a scratch directory for the classifier.
- `supertool-before-*` copies of a file before an edit, only if you configure a notifier; removed after an hour.
- A notifier debug log, only if you turn notifier debugging on.

### Elsewhere, only if you ask for it

- A `.supertool.json` in your repository, when you run `init:write`.
- A copy of every GitHub issue and pull request you read, in a directory you name, if you turn on the optional mirror setting in `.supertool.json`.

The session-start hook and the Bash check run without any opt-in. Between them they create the `supertool` link, append to `supertool-calls.log`, write `.gc-stamp`, and may prune old cache files (see below). They make no network calls.

## What leaves your machine

supertool makes network requests only when you, or Claude on your behalf, run an op that needs one. Nothing is sent when a session starts or from the Bash check.

- **GitHub**: the `gh-*` ops, `plugin-marketplace` and `dashboard` run the GitHub CLI (`gh`), which uses your own GitHub credentials and talks to GitHub. Some of these ops change things on GitHub on your account: create or edit issues and pull requests, post comments, merge, follow users, star repositories.
- **GitHub images**: `gh-issue` downloads images linked in an issue directly over HTTPS, without your credentials, and by default only from github.com and githubusercontent.com. At most 8 MB per image.
- **GitLab**: the `gl-*` ops run the GitLab CLI (`glab`), which uses your own GitLab credentials and talks to the GitLab server it is configured for, including issue image downloads.
- **Your git remotes**: `git-push`, `git-merge`, `git-checkout`, `dashboard` and `oss-tick` can run `git push`, `git fetch`, `git pull` or `git ls-remote` against your repository's remotes, with your own git credentials.
- **Anthropic, through your own Claude Code login**: `gh-issue`, `gh-pr`, `gl-issue` and `gl-mr` label each issue or comment body as safe or suspect before showing it to Claude. When a local pattern check finds nothing, the text is sent to a separate `claude -p` run, with a pinned Haiku model, no tools, no project instructions, no saved session, from an empty temporary directory. At most six such runs per op call. This uses your Claude account and is on by default. Set the op's `classify` option to `scanner` (local check only) or `off` in `.supertool.json` to stop it.
- **Tools you configure**: validators, formatters and language servers run programs already installed on your machine. When a JavaScript linter is not installed globally, supertool runs it through `npx` in a mode that does not download packages.

The directory build registers no MCP server with Claude Code and runs no background process. (The optional language-server helper described above runs only if you configure one, and talks only over a local socket.)

## How long it is kept

- The cache directory is pruned automatically, at most once an hour, at the end of a supertool call: `read-elide` after 1 day, `paste-backup`, `vim-undo`, `vim-cursor` and `statusline` after 7 days, `validators` after 30 days. You can change these windows or turn pruning off in `.supertool.json`, and run `gc` to preview or `gc:run` to prune now.
- Files in the temporary directory, including `supertool-calls.log`, downloaded images, CI logs and the classifier cache, are not pruned by supertool and have no size limit. They stay until your operating system clears its temporary directory or you delete them.
- Language-server logs and the optional mirror stay until you delete them.
- Changes to your project files stay, as with any editor.

## Personal data

supertool does not look for names, email addresses or other personal data, and does not build a profile of you.

Some of what it handles contains personal data anyway:

- Git output it shows includes commit author names.
- GitHub and GitLab ops show usernames, issue and comment text written by other people, and, for `gh-find-followable` and `gh-following`, lists of GitHub users.
- `supertool-calls.log` records your login name.
- If a secret or other sensitive text is in a file you edit, it can end up in `paste-backup/` or `vim-undo/`. If you pass it inline in a command, it is written to `supertool-calls.log`. If it is in a GitHub or GitLab issue you read, it is shown to Claude, may be saved with its images, and may be sent to the classifier described above.

All of this stays on your computer, except what goes to GitHub, GitLab, your git remotes or Anthropic as listed under "What leaves your machine".

## Contact

Questions and reports: <https://github.com/Digital-Process-Tools/claude-supertool/issues>

---

This page was written by an AI (Claude) from the supertool source code. Reviewed by the maintainer: TODO (name and date).
