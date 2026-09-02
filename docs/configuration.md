# `.supertool.json` — project configuration

Supertool works with no configuration. The `.supertool.json` is optional — it enables self-documenting ops for LLM onboarding via `./supertool 'introduction' 'ops'`.

Create a `.supertool.json` in your project root. Supertool walks up from cwd to find it. A starter template ships with the plugin as `.supertool.example.json`.

```json
{
  "introduction": "This project uses supertool for batched file reads and static analysis. Invoke with: ./supertool 'read:src/app/Module.py' 'grep:pattern:src/'",

  "output-format": "Each operation returns a header followed by its output:\n\n--- read:src/app/Module.py ---\n(45 lines, 1230 bytes)\n     1→import os\n     2→import sys\n\n--- grep:class:src/app/:5 ---\n(2 results, limit 5)\nsrc/app/Module.py\n  4:class Module:\nsrc/app/Config.py\n  8:class Config:",

  "builtin-ops": {
    "read": {
      "syntax": "read:PATH[:OFFSET:LIMIT]",
      "description": "Read file (300 lines, 20KB cap)",
      "example": "read:src/app/Module.py:1:50"
    },
    "read-grep": {
      "form": "read",
      "syntax": "read:PATH:::grep=PATTERN",
      "description": "Inline filter — matching lines, line nums kept",
      "example": "read:src/app/Module.py:::grep=class"
    },
    "grep": {
      "syntax": "grep:PATTERN:PATH[:LIMIT[:CONTEXT]][:no-auto-read]",
      "description": "Search (10 results def). CONTEXT=N lines around match. :no-auto-read suppresses single-file auto-read",
      "example": "grep:def handle:src/:20:2"
    },
    "map": {
      "syntax": "map:PATH",
      "description": "Symbol tree. tree-sitter>ctags>regex",
      "example": "map:src/app/"
    }
  },

  "ops": {
    "mypy": {
      "cmd": "python -m mypy --no-error-summary {file}",
      "timeout": 60,
      "description": "Type-check a Python file with mypy.",
      "example": "mypy:src/app/Module.py"
    },
    "pytest": {
      "cmd": "python -m pytest --no-header -q {file}",
      "timeout": 120,
      "description": "Run pytest on a test file.",
      "example": "pytest:tests/test_module.py"
    },
    "lint": {
      "cmd": "ruff check {file}",
      "timeout": 30,
      "description": "Lint a file with ruff.",
      "example": "lint:src/app/Module.py"
    }
  },

  "aliases": {
    "verify": {
      "ops": ["mypy:{file}", "lint:{file}"],
      "description": "Type-check + lint in one round-trip.",
      "example": "verify:src/app/Module.py"
    },
    "qa": {
      "ops": ["mypy:{file}", "lint:{file}", "pytest:tests/"],
      "description": "Full quality check: types, lint, tests.",
      "example": "qa:src/app/Module.py"
    }
  }
}
```

## `introduction` and `output-format`

User-controlled strings output by meta-ops:

```bash
./supertool 'introduction'        # prints the introduction string
./supertool 'output-format'       # prints the output-format string
./supertool 'introduction' 'output-format' 'ops'   # full LLM onboarding in one call
```

Use this in session-start hooks or agent prompts to onboard LLMs to your project's supertool setup without reading config files manually.

## `builtin-ops`

Entries document built-in operations (`syntax`, `description`, `example`). Set `"status": 0` to hide an entry from `./supertool 'ops'` output (works on `builtin-ops`, `ops`, and `aliases`).

**A preset manifest may carry this section too**, and two shipped ones do — [`lsp`](presets/lsp.md) and [`vim`](presets/vim.md), which document built-ins rather than defining ops ([#2025](https://github.com/Digital-Process-Tools/claude-supertool/issues/2025), [#2026](https://github.com/Digital-Process-Tools/claude-supertool/issues/2026)). Preset entries merge into this section, an entry written here winning key-for-key, so an op documented only by a preset the project does not list is missing from the *listing* rather than absent: it still dispatches, `ops` reports it under "Also accepted, no reference in .supertool.json", and `help:OP` still answers it from the shipped reference, which folds in the shipped presets' own `builtin-ops` for that purpose ([#1773](https://github.com/Digital-Process-Tools/claude-supertool/issues/1773)). The behaviour-overriding keys below are read from the merged section, so a preset can set them and a project can override them.

**A key here is an op name unless it declares `"form": "<parent>"`.** This section is what a machine reads to learn what the dispatcher accepts, so a key that resolves to nothing is a name handed out that answers `unknown operation`. Three did across the two configs this repo ships: `read-grep`, `grep-count` and `grep-no-exclude` document *forms* of `read` and `grep` — `read:PATH:::grep=PATTERN`, `grep:P:PATH:L:C:count`, `grep:P:PATH:L:::no-exclude` — and none is dispatchable. They are kept, because each carries `"hint": true` and its example is what survives `ops-compact` into the SessionStart listing, and they are now declared: `form` names the op the entry is a spelling of, that op's `syntax` must begin with it, and enumerations of op names skip the entry. `tests/test_registry_matches_dispatcher_1245.py` holds both directions ([#1245](https://github.com/Digital-Process-Tools/claude-supertool/issues/1245)).

Besides documentation, `builtin-ops` entries can also override default behavior:

| Op     | Key           | Default          | Effect                                                                                   |
| ------ | ------------- | ---------------- | ---------------------------------------------------------------------------------------- |
| `read` | `max_lines`   | 300              | Max lines per read                                                                       |
| `read` | `max_bytes`   | 20000            | Max bytes per read (truncates at cap). Also the yardstick the abstract read has to beat  |
| `read` | `abstract`    | 0 (switch)       | `1` → a read of a file over the threshold returns its tree-sitter symbol map instead of source, for every language in supertool's table. Falls back to source, with the reason, when the map is empty or no smaller. See [operations/reads.md](operations/reads.md#abstract-read) |
| `read` | `php_abstract` | 0 (switch)      | Former name of `abstract`, from when the gate was `.php`. Still enables it; either key set to `1` is enough |
| `read` | `abstract_threshold_bytes` | `max_bytes` | File size above which the abstract read applies. Env: `SUPERTOOL_READ_ABSTRACT_THRESHOLD_BYTES` |
| `read` | `elide` | 1 (switch) | `0` → never elide a repeat read of an unchanged file. `SUPERTOOL_READ_NO_ELIDE=1` does the same for one call. Only fires when both reads share the same parent process, so under Claude Code it never fires between Bash tool calls ([#1352](https://github.com/Digital-Process-Tools/claude-supertool/issues/1352)). See [operations/reads.md](operations/reads.md#eliding-a-repeat-read) |
| `read` | `elide_window_seconds` | 900 | How long after content was last **returned** a byte-identical repeat may be elided. Measured from the last read that actually handed over bytes, never from the last elision. Env: `SUPERTOOL_READ_ELIDE_WINDOW_SECONDS` |
| `grep` | `max_results` | 10               | Default result limit when not specified in the op                                        |
| `grep` | `max_line_chars` | 500           | Max chars per output line (match or context); remainder shown as `… (+N chars)`           |
| `grep` | `count_ceiling` | 1000           | How far past `LIMIT` a truncated grep keeps counting so it can state `N matches total`. Past the ceiling it says `N+ … (count capped at N)` rather than a number it did not reach. Never applied below `LIMIT`. Env: `SUPERTOOL_GREP_COUNT_CEILING` |
| `grep` | `count_truncated` | 1 (switch)  | `0` → a truncated **rtk-delegated** grep skips its `grep -rc` census pass and reports `total unknown (the delegated count pass returned no total)` instead of an exact total. The census is a second full scan of the tree, bought only on truncation ([#1771](https://github.com/Digital-Process-Tools/claude-supertool/issues/1771)). No effect on the native walker, which is governed by `count_ceiling`. Env: `SUPERTOOL_GREP_COUNT_TRUNCATED` |
| `grep` | `extensions`  | `[]` (all files) | Restrict grep to these file patterns (e.g. `["*.py", "*.js"]`). Empty = search all files |
| `around` | `max_bytes` | 16000            | Max bytes for an `around:` context window (truncates at a line boundary)                 |
| `grep_around` | `max_bytes` | 16000       | Max bytes for a `grep_around:` (and `grep:`-with-context) window                          |
| `glob` | `max_results` | 50               | Max files returned                                                                       |

**Switches and thresholds are read by two different helpers, and the difference decides what `0` means** ([#1332](https://github.com/Digital-Process-Tools/claude-supertool/issues/1332)).

- **A switch** — `read.elide`, `read.abstract`, `read.php_abstract` — accepts `0`/`1`, `true`/`false`, or the strings `yes`/`no`/`on`/`off`. **`0` means off**, including for a switch whose default is on. `read.elide: 0` was documented in three places and inert for exactly as long as it was read as a threshold.
- **A threshold** — every other key in the table above — takes a positive whole number, and a `0` or a negative there is refused, not honoured: `"max_lines": 0` meaning "return no lines" would turn a misconfiguration into a silently empty read. The refusal is now printed, naming the key, the value it discarded and the limit actually in force. A `true` in a threshold is refused the same way — Python reads it as `1`, so it used to give you a one-line `read` without a word.

Every key of both kinds also takes an env override, `SUPERTOOL_<OP>_<KEY>` — `SUPERTOOL_READ_ELIDE=0`, `SUPERTOOL_READ_MAX_LINES=500`. An override that cannot be read is announced rather than silently dropped ([#654](https://github.com/Digital-Process-Tools/claude-supertool/issues/654)).

Example — increase read cap and restrict grep to PHP/XML:

```json
{
  "builtin-ops": {
    "read": { "max_lines": 500, "max_bytes": 40000 },
    "grep": { "extensions": ["*.php", "*.xml"] }
  }
}
```

## `ops` — custom commands (argv-form, no shell)

Called directly by name:

```bash
./supertool 'mypy:src/app/Module.py' 'pytest:tests/test_module.py'
```

Each op has `cmd`, `timeout`, `description`, `example`, and optional `status`. Ops accept `{file}` and `{dir}` (dirname of file) placeholders. Shorthand string ops (`"lint": "ruff check {file}"`) still work with a 60s default timeout.

**`cmd` runs argv-form (`shell=False`), not in a shell.** Templates are tokenized via `shlex.split` and dispatched to `subprocess.run([...])`. This means:

- ✅ **Works**: `python3 tool.py {file}`, `glab issue view {arg}`, `ruff check {file}`
- ❌ **Does NOT work**: pipes (`|`), redirects (`>`, `>>`), chains (`;`, `&&`, `||`), command substitution (`$()`, backticks), globs (`*`, `?`)
- ✅ **Still works**: `$VAR` / `${VAR}` expansion from the op's env (see [Extra config keys as environment variables](#extra-config-keys-as-environment-variables) below) — performed by supertool, no shell involved

**Expansion reads the template only, never an argument.** `$VAR` is resolved in the `cmd` text *you* wrote. A `$VAR` that arrives through `{file}`, `{dir}`, `{arg}`, `{args}` or `{argjoin}` is caller data and reaches the op byte-exact — so `git-commit:::chore: about $HOME:::f.txt` commits the word `$HOME`, not your home directory. It did not, until [#1734](https://github.com/Digital-Process-Tools/claude-supertool/issues/1734): expansion ran over the assembled command, after the placeholders were interpolated, so it could not tell your template from the caller's text. That disclosed an environment variable's *value* into a commit object because its *name* appeared in a message, and a commit gets pushed. Placeholder values are now substituted as inert per-call tokens and restored *after* expansion — not reordered around it, which would have made an env value that happens to read `{args}` into a second interpolation site. The same treatment covers validator, formatter and `resolve` templates, where the caller-named data is the `{file}` path.

An **undefined** variable is left literal rather than emptied — deliberately, and the one place it bites: a template naming `$FOO` on a machine without `FOO` passes `$FOO` through as text, so the difference between a working template and a broken one only shows up where the variable exists.

If you need shell features, write a small wrapper script and reference it: `"cmd": "bash scripts/my-wrapper.sh {file}"`.

This closes the [#145](https://github.com/Digital-Process-Tools/claude-supertool/issues/145) RCE vector where a malicious `.supertool.json` could chain shell metachars in `cmd` templates to execute arbitrary commands on the next edit op.

## `aliases` — one name to multiple ops

Format is an object (legacy array format deprecated):

```bash
./supertool 'verify:src/app/Module.py'   # runs mypy + lint in one round-trip
```

Each alias has `ops` (array), `description`, `example`, and optional `status`. Aliases don't recurse.

## Dispatch order

built-in ops → custom ops (including preset ops) → aliases. Built-ins always win. Project ops override preset ops on name conflict.

## Placeholders in custom ops and aliases

| Placeholder | Expands to                                        | Example                               |
| ----------- | ------------------------------------------------- | ------------------------------------- |
| `{file}`    | First argument (one argv token after shlex.split) | `cat {file}`                          |
| `{dir}`     | Directory of `{file}`                             | `ls {dir}`                            |
| `{arg}`     | First argument (one argv token)                   | `glab issue view {arg}`               |
| `{args}`    | All arguments, expanded to N argv tokens          | `python3 tool.py {args}`              |
| `{argjoin}` | All arguments rejoined with `:::`, one argv token  | `python3 tool.py {argjoin}`           |
| `{path}`    | Preset directory with trailing `/` (presets only) | `python3 {path}gitlab/issue.py {arg}` |

Use `{file}`/`{dir}` for file operations, `{arg}`/`{args}` for non-file arguments (issue numbers, job IDs, etc.).

**`{file}`, `{dir}` and `{arg}` take exactly one token, and a token they cannot reach is now refused** ([#873](https://github.com/Digital-Process-Tools/claude-supertool/issues/873)). `op:all:dry` against `"cmd": "tool.py {arg}"` used to run as `argv == ["all"]` — the `:dry` vanished silently, which in the filed case meant a dry-run flag was dropped and the op pushed for real. The op is now declined before it runs, with the dropped text named. If your op takes more than one `:`-separated argument, write `{args}` (each token its own argv word) or `{argjoin}` (all tokens rejoined with `:::` as one word).

## Extra config keys as environment variables

Any key in a custom op config that isn't a reserved key (`cmd`, `timeout`, `description`, `syntax`, `example`, `status`, `restartMcp`, `replaces`, `paths`, `exitStatus`) is passed to the subprocess as a `SUPERTOOL_` prefixed environment variable:

```json
{
  "ops": {
    "job": {
      "cmd": "python3 job.py {arg}",
      "lines": 80,
      "error_patterns": "ERROR,FAIL,Fatal"
    }
  }
}
```

The script receives `SUPERTOOL_LINES=80` and `SUPERTOOL_ERROR_PATTERNS=ERROR,FAIL,Fatal` in its environment. This lets users tune op behavior from JSON without modifying scripts.

## `raw_command_guard` — the shipped raw-command block

Default on. A `PreToolUse` hook shipped with the plugin (`hooks/pre-bash-guard.sh`) refuses any `Bash` command an op declares it replaces, quoting the op's own description. It matches `Bash|PowerShell` since [#1413](https://github.com/Digital-Process-Tools/claude-supertool/issues/1413); **the registry half** never refuses a PowerShell command, because a POSIX tokeniser reading PowerShell quoting would deny the wrong commands. It is disclosed as undecided only when its text names a binary some op supersedes; a PowerShell call naming nothing mapped gets no line at all, since a disclosure printed under every call is one nobody reads. The shipped rule layer below **does** refuse one, and the difference is not an inconsistency: a rule is a regex over the command's text, so it reads that shell exactly as well as it reads Bash, where a tokeniser does not ([#1698](https://github.com/Digital-Process-Tools/claude-supertool/issues/1698)). **That does not reach a native-Windows host with no Git Bash**, where the `bash` in the hook command resolves to the WSL launcher stub and the hook never executes at all — [#1378](https://github.com/Digital-Process-Tools/claude-supertool/issues/1378) is that gap and is unaffected by this. The mapping is the `replaces` key on each op's registry entry — see the Op schema in [contributing.md](contributing.md).

```json
{
  "raw_command_guard": false
}
```

Set it to `false` to turn the gate off for a project. It is the **only** way off: there is no environment variable and no per-command flag, because an escape hatch that can be prepended to a command is not a block. A raw call nobody replaced is not blocked in the first place — `gh release create`, `gh api -X DELETE`, `git tag` have no `replaces` entry and never will unless an op supersedes them.

**That is the coverage limit on one axis; there is a second, and it is wider.** Everything above is about *which commands* are refused. The gate also only ever sees *which tool* they arrived through: the matcher is `Bash|PowerShell`, so Claude Code's own `Edit`, `Write`, `MultiEdit` and `NotebookEdit` write to disk without passing the hook, without an op, and therefore without the post-edit validator chain and its rollback-on-syntax-failure. The same one-key change to the same file is denied as a `python3 - <<EOF` heredoc and unremarkable through `Edit`, minutes apart ([#1671](https://github.com/Digital-Process-Tools/claude-supertool/issues/1671)). So a refusal naming a path means *this route is protected*, not *this file is protected* — read it as the latter and you are inside a boundary that is one tool call wide. Nothing in the plugin can close it, because no hook it ships can see those calls; closing it is the operator's deny list, under [Hard-block native tools](../README.md#hard-block-native-tools-optional) in the README. This section, the README recipe and the SessionStart op roster all name those tools, and the refusal text states the scope without naming them — that split is [#1706](https://github.com/Digital-Process-Tools/claude-supertool/issues/1706). The refusal used to close "a harness Edit/Write reaches this same path with no op, no validator and no rollback", which is a working route past the gate written into the one sentence a blocked agent is guaranteed to read: an agent that takes it loses the validator chain and the write rollback the refusal exists to route it into. Inert here, because `harness-tools-blocked` denies those tools in this checkout; live for every plugin user. It now says the gate hooks Bash only, that the denial is about the route and not the path, and that **any** other route to the file gets no op, no validator and no rollback — the deterrent without the direction. A reader deciding what to put in a deny list is not being denied, so the tool names stay on the three surfaces above; `tests/test_guard_refusal_names_no_bypass_1706.py` holds both halves.

**A repository can wire the guard itself instead of waiting for a plugin release.** The hook ships in the plugin's `hooks.json`, so it becomes active for a checkout only when the installed plugin carries it — a claude-supertool clone whose plugin install predates the hook has the mapping and no enforcement. Adding it as a `PreToolUse` hook in the project's own `.claude/settings.json` closes that gap, and is how this repository dogfoods its own guard ([#1376](https://github.com/Digital-Process-Tools/claude-supertool/issues/1376)) — the Bash half of it. **This checkout cannot dogfood the tool half**, and that is worth stating where the dogfooding is claimed: `Edit`/`Write` are blocked here by `.claude/jit-context/tools/00-manual/harness-tools-blocked.md`, a hand-written rule that does not ship, so the maintainers of this repository do not experience the gap every plugin user has ([#1671](https://github.com/Digital-Process-Tools/claude-supertool/issues/1671)).

```json
{
  "matcher": "Bash|PowerShell",
  "hooks": [
    {
      "type": "command",
      "command": "S=\"$CLAUDE_PROJECT_DIR\"/hooks/pre-bash-guard.sh; if [ -f \"$S\" ]; then CLAUDE_PLUGIN_ROOT=\"$CLAUDE_PROJECT_DIR\" bash \"$S\"; fi",
      "timeout": 15
    }
  ]
}
```

Three things about that snippet are load-bearing. `CLAUDE_PLUGIN_ROOT` is set explicitly, because the script defaults to it and a value inherited from an unrelated plugin install would point the hook at a different tree. It is its **own entry** rather than a second command inside an existing one, so one script's failure is not another's silence — every matching `PreToolUse` hook runs and any `deny` stops the call. And the missing-file branch should print a decline envelope rather than nothing: empty output is indistinguishable from a clean verdict, which is the failure the guard's own third state exists for.

**What earns a hand-written registration is a script that is in the checkout.** `hooks/pre-bash-guard.sh` is, so `$CLAUDE_PROJECT_DIR` resolves it in every clone and the absent branch is a real fallback rather than the normal case. Until [#1726](https://github.com/Digital-Process-Tools/claude-supertool/issues/1726) the same file also registered four `claude-jit-context` hooks — `session-start`, `pre-prompt`, `pre-tool`, `pre-path` — through `$HOME/Documents/claude-jit-context/scripts/`, a clone that exists on one maintainer's disk, behind `[ -f "$S" ] && bash "$S" || true`. Those scripts are in no checkout of this repository at all, so `$CLAUDE_PROJECT_DIR` could not have rewritten them; what the tracked file shipped to everyone else was four registrations that ran and did nothing, and `|| true` made that identical to four rules that matched nothing.

They were **deleted rather than repointed**, because the plugin already owns them: `claude-jit-context` 0.3.5 registers all four in its own `hooks/hooks.json` as `bash ${CLAUDE_PLUGIN_ROOT}/scripts/…`, which resolves for every install and reads this repository's `.claude/jit-context/` rules the same way. The measurement the issue asked for came back with a second finding: on the one machine where both paths existed, both fired — the same JIT note was injected twice per match, so the hand-written copy was not a fallback for the plugin but a duplicate of it. `.claude/jit-context/` entries are unaffected; they are data the plugin reads, not wiring. **The rule this leaves is one line: a hook whose script ships in a plugin belongs in that plugin's `hooks.json`, and the tracked `.claude/settings.json` registers only scripts this repository itself contains.** `tests/test_settings_hooks_portable_1726.py` holds both halves — no tracked hook command may name `$HOME`, and any command guarding on a script's existence must announce the absent branch instead of returning `true`.

**Both of those fixed the file's content, and the file has a writer that is not a person.** On 2026-08-15 six lines nobody typed appeared in the tracked file during a session in which `/reload-plugins` was run — an `enabledPlugins` block naming one machine's plugin roster, the same block deleted a day earlier as local state ([#1747](https://github.com/Digital-Process-Tools/claude-supertool/issues/1747)). The harness maintains that key and writes it into whichever settings file it finds, so a deletion is undone by ordinary use and the only thing between the block and a commit is whoever reads `git status` carefully that day.

So the tracked file is guarded by **an allowlist of top-level keys**, in `tests/test_settings_no_machine_state_1747.py`: today that set is `{"hooks"}`, and any other top-level key fails CI by name. The direction is the decision. A denylist of keys the harness is known to write is green the first time it invents one — it fails open, silently, which is the defect class this repository keeps having. The allowlist fails closed, and its cost is that **somebody adding a legitimate setting is interrupted**: the red leg asks whether a human typed the key or the harness wrote it, and widening the set is a line in a pull request rather than a shrug. It reads the top-level surface only; machine state nested inside an allowed key is not caught, and the `$HOME` rule above narrows that gap rather than closing it — an absolute `/Users/someone/…` inside a hook command passes both guards. Any future allowed key is unguarded until whoever adds it writes one.

**Where the per-machine copy goes is `.claude/settings.local.json`**, which `.gitignore` now names. It was previously ignored on the maintainer's disk only through `~/.config/git/ignore` — a file no other clone has — so everywhere else the harness's per-machine settings sat untracked and one `git add -A` from being committed, which is the same disclosure through a different filename. Deleting the tracked file instead is not the answer: it is what carries the `pre-bash-guard.sh` registration to every clone, and that is the thing [#1698](https://github.com/Digital-Process-Tools/claude-supertool/issues/1698) argues must not wait on a plugin release. Untracking would trade a drift that `git status` renders for an absence that renders as nothing at all.

`supertool 'guard:SHELL COMMAND'` answers the same question without running anything, in four states: `BLOCKED` naming the op, `OK`, `NOT COVERED` when an entry claims the verb and declines this invocation of it ([#1684](https://github.com/Digital-Process-Tools/claude-supertool/issues/1684) — `git push origin v0.2.0` carries a refspec `git-push` does not take, so the command runs and the guard says no op covers this form rather than naming one that pushes a different ref), and `UNDECIDED` when the command did not tokenise, hid a substitution inside double quotes, handed a string to `eval` / `sh -c`, carried a pre-subcommand global option the matcher has no grammar for ([#1421](https://github.com/Digital-Process-Tools/claude-supertool/issues/1421)), or the registry could not be enumerated. The hook adds one more of its own: a command routed through the **PowerShell** tool, whose quoting a POSIX tokeniser cannot read ([#1413](https://github.com/Digital-Process-Tools/claude-supertool/issues/1413)). The hook allows on `UNDECIDED` and says so in the transcript — a gate that quietly did not run is indistinguishable from a command that complied, which is the whole reason it exists.

### Which interpreter the hooks run, and what each does when there is none

`hooks/python-ladder.sh` tries, in order: an activated virtualenv's own interpreter when `VIRTUAL_ENV` points at a directory containing `pyvenv.cfg`; then `python3.14` down to `python3.9` on `PATH`; then `py -3`, the Windows Python launcher. The bare name `python3` is never tried: on Windows it can resolve to the App Execution Alias stub and on a stock macOS to the Xcode Command Line Tools stub, and both block rather than erroring ([#572](https://github.com/Digital-Process-Tools/claude-supertool/issues/572)).

**Both shipped hooks source that one file** ([#1382](https://github.com/Digital-Process-Tools/claude-supertool/issues/1382)). Until then the ladder lived inside `pre-bash-guard.sh` and `session-start.sh` ran the bare `python3` a few lines away — two scripts in one directory disagreeing about the repo's own convention, which is what happens when a decision has no home rather than a typo. The helper resolves and never decides; what to do when no rung answers is each caller's own, and the three callers pick differently on purpose:

| Script | Runs | When nothing resolves |
| --- | --- | --- |
| `hooks/pre-bash-guard.sh` | before every `Bash` call | declines in words, allows the command — the gate is off and says so |
| `hooks/session-start.sh` | once per session | prints one line naming the rungs it tried, exits 0, and keeps the `./supertool` symlink it already made without any interpreter |
| `.githooks/pre-push` | on `git push`, opt-in | refuses the push, lists every name it tried, documents `PYTHON=` as the way through. Keeps the shorter ladder and does not source the helper: a loud refusal with an escape hatch does not need the extra rung a disclosed degrade does |

**The session hook's floor is a degrade, not a bare `python3` and not a failure.** A last-rung `python3` would preserve the hang for exactly the hosts that have no alternative; a non-zero `SessionStart` hook is a broken session on every platform in order to report a missing interpreter on one. What is actually lost when the ladder comes up empty is the op roster, and the symlink — the half that never needed an interpreter — survives.

**`py -3` is last, and it is the rung Windows usually needs** ([#1402](https://github.com/Digital-Process-Tools/claude-supertool/issues/1402)). Neither python.org's installer nor GitHub's `hostedtoolcache` creates `python3.9.exe`–`python3.14.exe`; both create `python.exe` and `python3.exe`. So the versioned ladder found nothing on a standard Windows install and the guard reached its third state — disclosed, allowed — on every `Bash` call, while `raw_command_guard` defaulted to on. The launcher is a real executable rather than an alias stub and takes a version selector, so it answers the probe; it sits after the versioned names so a host with a real `python3.12` keeps using that. This is graded **reasoned, not observed**: nobody on this project has a Windows box, and the load-bearing claim is that Windows ships no default App Execution Alias for `py.exe` — the stubs that block, and that got `python3` banned, are `python.exe` and `python3.exe`. Putting the rung last bounds the cost if that is wrong: any host with a versioned interpreter never reaches it.

This reverses a decision [#572](https://github.com/Digital-Process-Tools/claude-supertool/issues/572) made deliberately, and only for the shared ladder — so for both plugin hooks since [#1382](https://github.com/Digital-Process-Tools/claude-supertool/issues/1382), and for neither `.githooks/` hook. `py -3` was then "considered and dropped: this is a bash script that only ever runs under Git Bash or WSL, where a Windows launcher shim is the wrong layer to reach for". That established versioned names are not *aliased* on Windows — true, and why they are safe to try — but not that they are *present*, and they are not. `.githooks/pre-push` keeps the shorter ladder on purpose: it refuses the push and names `PYTHON=` as the way through, so a loud refusal with an escape hatch does not need the rung the way a disclosed degrade does.

**A candidate has to print the probe's exact token**, not merely contain it, which is also what rejects a launcher that writes a preamble of its own. A launcher that announces itself is skipped and the calling hook declines in words, rather than a verdict — or an op roster — being built on an interpreter that could not be identified.

**`SUPERTOOL_PYTHON` is not read here**, and that is the "only way off" sentence above made true rather than merely written down ([#1390](https://github.com/Digital-Process-Tools/claude-supertool/issues/1390)). It was on the ladder, the only test applied to a candidate was that `-c pass` exited 0, and every binary on the box passes that — so `SUPERTOOL_PYTHON=/usr/bin/true` turned the gate off with no output and no disclosure. A candidate now has to identify itself as a Python 3 before it is run, and the variable does not participate: it exists for supertool's own op subprocesses, and a gate deciding whether a command may run is a different trust context.

**A candidate that runs and answers nothing reaches the third state**, not the clean one. The hook writes an answer on every path including "nothing is replaced", so empty output from the interpreter means the guard did not answer, and the wrapper says so in the transcript and allows the command. Before this only an *empty ladder* produced that disclosure, so an interpreter that started and said nothing rendered exactly like a clean verdict.

**The wrapper writes the envelope; the rung writes a verb and a reason** ([#1625](https://github.com/Digital-Process-Tools/claude-supertool/issues/1625)). `hooks/pre_bash_guard.py` prints `supertool-guard-v1 ` and one of exactly three words — `silent`, `note`, `deny` — then a newline, then the text if there is any. `hooks/pre-bash-guard.sh` matches that prefix to recognise an answer, and then builds the `hookSpecificOutput` document itself: every field name, `permissionDecision` and its only possible value `deny`, are literals in the shell script, and the rung's text reaches JSON only through the escaper [#1613](https://github.com/Digital-Process-Tools/claude-supertool/issues/1613) added.

Until then the rung wrote the whole envelope and the wrapper forwarded it verbatim, so a `$VIRTUAL_ENV/bin/python3` that printed a well-formed document carrying `"permissionDecision":"allow"` and exited 0 had written the harness's own verdict — well-formed JSON, nothing for the escaper to catch, and no way for the wrapper to tell that document from its own. Authenticating the channel cannot close it: any token proving *my rung wrote this* has to be handed to the rung, and the interpreter is what executes the script, so no secret separates "ran it" from "read it and lied". What is left is to stop forwarding structure.

**A verb the wrapper does not know is declined, not dropped.** Two installs disagreeing about the vocabulary would otherwise turn every `Bash` call into a gate that silently said nothing. It takes the same disclosed-allow the ladder's own failures take, and names the dialect it could not read. The cost is stated rather than discovered later: a field the `PreToolUse` protocol gains has to be added to both files before it can be emitted at all — an open channel into someone else's schema, exchanged for a closed one this repository owns both ends of.

That bounds the forgery rather than ending it. A rung that is already lying can still say `deny` with a misleading reason, or say `silent` and suppress a real one — though it could always suppress one by not answering. What it can no longer write is `allow`, the one value that removes the user's own permission prompt, or any other field of the hook protocol. Reaching that sink at all still requires setting `$VIRTUAL_ENV` to a directory whose `bin/python3` the attacker controls, which is already an execution primitive; this is a fabricated verdict, not an escalation to code execution.

### How the harness resolves the hook command, and why a bare `bash` is right

Both shipped hooks are registered as `bash "${CLAUDE_PLUGIN_ROOT}/hooks/…"` with no `args`, which is Claude Code's **shell form**: the string is handed to a shell — `sh -c` on macOS and Linux, Git Bash on Windows, or PowerShell when Git Bash is not installed. It is *not* tokenised and handed to a direct spawn, so `bash` is not resolved by `CreateProcess` searching `PATH` — which on Windows would find System32's `bash.exe`, the WSL launcher, a program that is not a shell and exits 1 with UTF-16 output without ever opening the script ([#1401](https://github.com/Digital-Process-Tools/claude-supertool/issues/1401)).

Adding `args` would switch these hooks to **exec form**, where `command` *is* resolved as an executable on `PATH` and spawned directly — so the obvious-looking repair introduces the defect rather than removing it. `tests/test_hook_interpreter_windows_1401_1402.py` refuses it.

The remaining branch is a native Windows host with no Git for Windows, where the string reaches PowerShell and `bash` there can find the WSL launcher. That is also the host where Claude Code has **no `Bash` tool at all** — it routes shell commands through the PowerShell tool instead. This paragraph used to conclude that a `PreToolUse` hook therefore never fires there, which was wrong by this repo's own [#1413](https://github.com/Digital-Process-Tools/claude-supertool/issues/1413): the matcher has been `Bash|PowerShell` since, precisely because Claude treats PowerShell as the primary shell where that tool is enabled. So the guard hook *is* asked on that host and is merely **inert** — [#1378](https://github.com/Digital-Process-Tools/claude-supertool/issues/1378)'s silent state, which `hooks/guard-selftest.py` exists to break. `SessionStart` is gated by nothing at all, fires under any tool configuration, and loses its `./supertool` symlink and its op roster; that is a real gap, and it is a gap in what a `.sh` hook can do on a host with no POSIX shell, not something a different command string fixes.

**A Python rewrite does not escape that**, and it is worth writing down because it is the repair the issue names first. Exec form would still have to name an interpreter, and no name is portable: `python` and `python3` are the App Execution Alias stubs [#572](https://github.com/Digital-Process-Tools/claude-supertool/issues/572) banned for blocking rather than erroring, `python3.9`–`python3.14` do not exist on a standard Windows install ([#1402](https://github.com/Digital-Process-Tools/claude-supertool/issues/1402)), and `py -3` exists nowhere else. Resolving that is exactly what `hooks/python-ladder.sh` does, and it is a shell script — so it cannot bootstrap a host with no shell.

**The disclosure has a channel on the affected host, and it covers both hooks** ([#1401](https://github.com/Digital-Process-Tools/claude-supertool/issues/1401)). `hooks/guard-selftest.py` needs no shell, and its `could not run` state is one fact about two dead hooks: it names `session-start.sh` alongside the guard — whose failure is the silent one, where the session hook's is a wrapper and a roster you can see are missing — says the session gets no `./supertool` wrapper and no op roster, and gives the way back — `py -3 supertool.py 'ops:roster'`, and reading `./supertool` in the docs as that path. Reporting only the guard left the more visible loss with no account of itself, which is the same absence-read-as-absence one layer up.

**That gap is disclosed rather than fixed, and the grades are these** ([#1401](https://github.com/Digital-Process-Tools/claude-supertool/issues/1401)). *Reasoned from Claude Code's own hook and setup documentation, not observed:* that shell form reaches PowerShell where Git Bash is absent, and that the `Bash` tool is absent on the same host. *Observed:* a bare `bash` under `CreateProcess` on the Windows runners is the WSL launcher, in this repository's own pytest. Nobody on this project has a Windows box, so every candidate repair was reasoning about an untestable host: `args` introduces the exec-form PATH search this section refuses, a second PowerShell entry breaks every POSIX session to serve one Windows one, and a shell string valid under both `sh -c` and PowerShell is a polyglot shipped to every user of the plugin on the strength of a Mac. The disclosure is the honest outcome, and it is what CI's twelve legs can never tell you either.

### The shipped rule layer sits under the registry, and says what it is not

The registry answers "which raw command does an op supersede". A second, much smaller layer answers what no `replaces` entry can reach at any spelling — piping an *op's own output* through `head`/`tail` is the one that ships ([#1698](https://github.com/Digital-Process-Tools/claude-supertool/issues/1698)). It is `hooks/shipped_rules.py`, read by the same PreToolUse hook, and it is consulted **only where `guard_command` returned no block**: a hand-written regex cannot express `unless_flag`, so it must never outrank the tokeniser that can.

Two things it does not do, both deliberate. It does not fire in a repository that carries its own copy of the rule file at `.claude/jit-context/tools/00-manual/<name>.md` — layers are the ownership boundary, per rule, so a repo that wrote its own version is not refused twice with two different messages. And it does not ship the four sibling rules that would be a wrong block somewhere else: one encodes a squash-merge workflow, one names ops a caller's presets may not load, one fires on tools no shipped matcher covers, and one wants a once-per-session mode a PreToolUse hook cannot provide.

`raw_command_guard: false` turns this layer off with the rest of the guard, because it is the same hook. **What it is not is silent about its own gaps:** `python3 hooks/guard-selftest.py` prints each shipped rule's state and each unshipped rule's reason, so "this repo does not have that rule" is something a reader can discover rather than something they find out by doing the thing the rule exists to stop.

## Compact mode

Set `"compact": true` in `.supertool.json` to enable compact reads. When enabled, `read` ops skip blank lines and comment-only lines (`//`, `#`, `/* */`, `<!-- -->`, PHPDoc `*` lines), preserving original line numbers. Reduces token cost for exploration without losing structure.

Compact is disabled when using `grep=` filter or `offset` (editing needs exact lines).

## Advice — config-driven post-edit hints

```json
{
  "advice": {
    "newTest": {
      "hooks_into": ["paste"], "match": "*.php", "when": "new-file",
      "resolveFromValidator": true, "message": "new class without test"
    },
    "newComponent": {
      "hooks_into": ["edit", "paste"], "match": "*.php",
      "contains": "extends \\w*ComponentBase|implements \\w*IComponent",
      "message": "XSD/cache regen likely (dvsi_xsd + dvsi_clearcache)"
    }
  }
}
```

Each rule appends a non-blocking `[advice]` line after a mutating op when it matches. Gates: `hooks_into` (ops, default all mutating), `match` (path glob), `when` (`new-file`|`existing-file`|`always`), `contains` (regex over the content the op *added*), and `resolve`/`resolveFromValidator` (a subprocess emitting a would-be target). Full field reference and the resolve contract in [validators.md → advice](validators.md#advice--config-driven-post-op-hints).

## Two supertool trees in one call

Config, presets and the scripts they point at are resolved from the **cwd's**
project root; the core that parses the ops comes from the `supertool.py` that
was invoked. Those are normally the same tree. When they are not — running a
branch worktree's `supertool.py` from a different checkout — every config op is
answered by the *other* tree, and the receipt used to say `PASS` regardless
([#678](https://github.com/Digital-Process-Tools/claude-supertool/issues/678)).

Supertool now detects the case and **declines the config ops** rather than
reporting a verdict for a build it cannot name:

```
$ cd ~/checkout-a
$ python3 ~/checkout-b/supertool.py 'git-status'
supertool: mixed supertool trees: core=~/checkout-b/supertool.py presets=~/checkout-a
--- git-status ---
SKIPPED: 'git-status' comes from a different supertool tree than the core that is running.
  ...
Fix: run from ~/checkout-b, or make the first op 'cwd:~/checkout-b'.
```

The call exits non-zero (the decline is counted as a skip, per
[#680](https://github.com/Digital-Process-Tools/claude-supertool/issues/680)).

| | |
| --- | --- |
| **What triggers it** | the resolved project root is *itself a different supertool checkout* — it holds a `supertool.py` that resolves into a directory other than the invoked install's. Compared by directory rather than by file since [#931](https://github.com/Digital-Process-Tools/claude-supertool/issues/931), which moved the code out of `supertool.py` and into `_supertool.py` beside it. |
| **What does not** | the ordinary install: a clone symlinked onto `$PATH` and used from any project root. Those roots are not supertool checkouts, so nothing is mixed. A project shipping its own `presets/` override is not a mix either. |
| **Built-in ops, read-only** | still run — `read`, `grep`, `ls` and the rest of `_PARALLEL_SAFE_OPS` come from the core that was invoked. A one-line disclosure goes to stderr so the operator knows whose validators and formatters are loaded. |
| **Built-in ops, write-class** | decline exactly like a preset or custom op ([#1942](https://github.com/Digital-Process-Tools/claude-supertool/issues/1942)) — `paste`, `edit`, `append`, `replace`, `replace_lines`, `vim`, `format`, `format_staged`, `gc`, `rename`, `batch`: every op `_OP_SAFETY_BUILTIN` marks `"writes"`. The write itself always lands on the right file — `_safe_path` resolves against `os.getcwd()`, never against either core's install directory — but the *code* answering (validators, formatters, hooks) would have been the other tree's, silently, which is the risk this guard exists for. Before this, the stderr-only disclosure above was the only signal, and it is easy to miss on a receipt that otherwise reads as an ordinary success. |
| **The remedy** | run from the checkout you meant, or make `cwd:<that checkout>` the first op |
| **The session hook** | does not manufacture the case: starting a session inside a supertool checkout used to leave a `./supertool` pointing at the plugin install, i.e. a wrapper every custom op declined. It now creates none and names `python3 supertool.py` ([#711](https://github.com/Digital-Process-Tools/claude-supertool/issues/711)). |
| **Deliberate mixing** | `SUPERTOOL_ALLOW_MIXED_TREE=1`. Config ops then run, and the verdict line carries the pairing — `PASS (0.42s) [mixed supertool trees: core=… presets=…]` — so it is never a bare `PASS`. |
| **A second, earlier case** | the entry point checks the same thing about *itself*. `supertool.py` loads the `_supertool.py` sitting beside it; if the name resolves to some other tree first — a `sys.meta_path` finder from an editable install outranks `sys.path` — it says so on stderr and runs on. If there is no `_supertool.py` beside it at all, it refuses with exit 2 rather than importing whichever copy the environment offers. |

## Numeric environment knobs, and what happens when one is wrong

Every `SUPERTOOL_*` knob that takes a number — `SUPERTOOL_MAX_COMMITS`,
`SUPERTOOL_DEFAULT_LIMIT`, `SUPERTOOL_PARALLEL`, `SUPERTOOL_LINT_TIMEOUT`, the
`SUPERTOOL_<OP>_<KEY>` overrides, and the rest — is read through one helper with
one contract. Three outcomes, never two:

| You set | What happens |
| --- | --- |
| a usable value | it is used, silently |
| nothing | the default is used, silently |
| something unusable | the default is used, **and a `note:` line says so** |

An unusable value is announced on **stdout**, naming the variable, the value it
saw, and the number actually in force:

```
$ SUPERTOOL_MAX_COMMITS=x ./supertool 'git-trail:dispatch:supertool.py'
note: SUPERTOOL_MAX_COMMITS='x' is not a whole number - ignoring it and using 20.
## Timeline (20 commits) [CAPPED: newest 20 by count, more exist — …]
```

This used to be an uncaught `ValueError` and a Python traceback
([#654](https://github.com/Digital-Process-Tools/claude-supertool/issues/654)).
The traceback is gone, but the run is **not** made quiet in exchange: a knob
that was set and then ignored without a word is worse than a crash, because a
cap you believe is in force and isn't will not announce itself later either.

**Out of range is refused, not clamped.** `SUPERTOOL_MAX_COMMITS=-5` does not
mean "none" and does not quietly become `1`. It is treated exactly like `x` —
reported, and the documented default is used instead. Rounding a negative up to
the nearest legal value would invent an intention you never expressed, and do it
silently, which is the same failure in a different shape:

```
note: SUPERTOOL_ENRICH_WORKERS='0' is below the minimum of 1 - ignoring it and using 8.
```

If you want the minimum, set the minimum.

**The note goes to stdout on purpose.** A preset that warns on stderr and then
succeeds has that warning discarded before it reaches you — supertool returns a
successful subprocess's stdout and only appends its stderr on a non-zero exit.

## Parallel execution

Read-only ops in a batch can run concurrently. Output order is preserved (matches input order, not completion order).

Enable in `.supertool.json`:

```json
{ "parallel": 4 }
```

`parallel: N` runs up to N ops concurrently via a thread pool. `0` (default) = sequential. Boolean `true` is accepted as `4` for back-compat.

Override via env: `SUPERTOOL_PARALLEL=4 ./supertool 'read:a' 'grep:x:b/' 'glob:c/**'`. Env wins over JSON. Set `0` to force off for one call.

A value that is neither a number nor `true`/`false` — or a negative one — leaves parallelism off *and says so*, rather than looking identical to never having set it. See [Numeric environment knobs](#numeric-environment-knobs-and-what-happens-when-one-is-wrong).

**Safe ops** (parallelized): `read`, `grep`, `glob`, `ls`, `head`, `tail`, `wc`, `stat`, `map`, `tree`, `around`, `around_line`, `between`, `diff`, `version`, `validate`, `validate_staged`, `workspace`, `resolve`, `diag`, `hover`, `help`. The list in `_supertool.py` is `_PARALLEL_SAFE_OPS`; this paragraph had been missing seven of them since they were added. It also carried `blame` until #1285 — there is no such op and has not been since it moved to the `git` preset as `git-blame`.

**Unsafe** — batch falls back to sequential whenever any op is mutating (`edit`, `replace`, `replace_dry`, `replace_lines`, `format`, `format_staged`) or custom (anything in `ops:` — could shell out to anything). All-or-nothing per call: no partial parallelism.

**Read-only is the membership rule, and it is not a formality.** `format_staged` sat in the safe set until [#1244](https://github.com/Digital-Process-Tools/claude-supertool/issues/1244); it shells formatters over every staged file and rewrites them, so `supertool 'format_staged' 'read:f.txt'` with `parallel` on rendered the *pre*-format bytes — under `[complete file — no more lines]` — while the post-format bytes were already on disk. The same call at `parallel: 0` was right, which is the part that makes it hard to see: a performance switch changed an answer.

There is no weaker "safe to run concurrently" reading available. Parallel safety is a property of the whole set of ops in one call, and a writer is unsafe beside any reader of the same path.

Speedup: I/O-bound ops on different files. ~3-5× faster on cold filesystem; modest gain on warm cache.

## Excluding paths from traversal ops

`glob`, `grep`, `tree`, and `map` walk the filesystem recursively. On large repos this can be slow and noisy — `.git/objects/`, `node_modules/`, `vendor/`, and similar dirs rarely contain what you're looking for. And some files are worse than noise: a `grep` that happens to cross a `.env` puts a live token in an LLM's context.

Excluded **directories** are pruned at the walk boundary — never opened. Excluded **files** are dropped from the result, and a file dropped for **credential** reasons is **counted**, so the report line says how many were hidden rather than simply not mentioning them.

Noise entries (`.git`, `node_modules`, `__pycache__`, `dist/`, the caches) are dropped without being counted — see [Hidden files](operations/search.md#hidden-files-are-counted-not-silently-dropped) for why a counter that is never zero stops being a signal.

**Built-in defaults** — always active unless overridden:

```
# Noise
.git/  node_modules/  .svn/  .hg/  .idea/  .vscode/
__pycache__/  .venv/  venv/  dist/  build/
phpstan-result-cache/  .phpunit.cache/  .rector/

# Credential directories
.max/  .ssh/  .aws/  .gnupg/  .kube/  .docker/
.terraform/  .chef/  .npm/  secrets/  credentials/

# Credential files
.env/  .env.*  .netrc/  _netrc/  .npmrc/  .pypirc/
.git-credentials/  .pgpass/  .my.cnf/  .htpasswd/  .dockercfg/
id_rsa*  id_dsa*  id_ecdsa*  id_ed25519*
*.pem  *.key  *.p12  *.pfx  *.jks  *.keystore  *.ppk
.hashnode-token/  .devto-token/  .bluesky-app-password/

# Kept visible on purpose
!.env.example  !.env.sample  !.env.template
!.env.dist  !.env.defaults  !.env.schema
```

**Three entry shapes:**

| Shape | Example | Matches |
|---|---|---|
| Literal | `.env/`, `node_modules/` | a dir **or a file** of that name. One segment matches at any depth; a multi-segment path (`src/legacy/`) is anchored to the project root. The trailing `/` is normalisation, not a directory assertion. |
| Glob | `*.pem`, `id_rsa*` | fnmatched against the basename. Needed for shapes that are not a fixed name. |
| Negation | `!.env.example` | un-excludes what it matches, and wins over every other entry whatever the order. |

**Where the credential-file boundary sits.** A file is on the list only when holding a credential is its entire purpose: an exact name (`.netrc`) or an unambiguous key-file shape (`*.pem`). There are deliberately no name-fragment heuristics — `*secret*`, `*token*`, `*password*` would hit source and test files constantly, and a search that silently skips your own code is a worse failure than the one the list exists to prevent. `.env.example` and its siblings are committed placeholders people legitimately read to learn which keys exist, so they are negated back in.

**Project-level additions** — add to `.supertool.json` under `ops.<op-name>.exclude-paths`. These are **merged additively** with the defaults (not replacing), and take all three shapes:

```json
{
  "ops": {
    "glob": { "exclude-paths": ["vendor/", "Dvsi/dvsi-private/libs/", "*.p8"] },
    "grep": { "exclude-paths": ["vendor/", "*.p8", "!config/*.key"] }
  }
}
```

**Per-call escape hatch** — append `:::no-exclude` to bypass all excludes for one call:

```bash
./supertool 'grep:somePattern:vendor/:10:::no-exclude'
./supertool 'glob:**/*.php:::no-exclude'
```

**A path you name yourself is never excluded.** `grep:PATTERN:.env` searches `.env`, and `read:.env` prints it. Naming the file is a deliberate act; gating it would buy nothing (`read` was never gated) and would break the case someone meant. The list guards the *side effect* — a search aimed elsewhere that walks over a credential on the way.

Ops that take explicit paths and don't traverse (`ls`, `read`, `head`, `tail`, `wc`, `stat`, `around`, `around_line`, `between`, `diff`) are not affected — they always work on exactly the path you give them.

See [issue #4](https://github.com/Digital-Process-Tools/claude-supertool/issues/4) for the original design rationale, [#691](https://github.com/Digital-Process-Tools/claude-supertool/issues/691) for the file-level wiring and the hidden-file count, and [#764](https://github.com/Digital-Process-Tools/claude-supertool/issues/764) for why that count survives the rtk-delegated `grep` — see [operations/search.md](operations/search.md#delegated-to-rtk).

---

## `gc` — cache retention

Controls the cache pruning described in [operations/meta.md](operations/meta.md#gc--cache-retention). Every key is optional; the block below is the default.

```json
{
  "gc": {
    "enabled": true,
    "interval_seconds": 3600,
    "retention_days": {
      "vim-cursor": 7,
      "vim-undo": 7,
      "vi-cursor": 7,
      "validators": 30
    }
  }
}
```

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | `false` disables the **automatic** sweep. The explicit `gc` op still works — this is not a way to make `gc:run` a no-op. |
| `interval_seconds` | `3600` | Minimum seconds between automatic sweeps, measured from the mtime of `~/.cache/supertool/.gc-stamp`. |
| `retention_days.<kind>` | see table | Age, in days, beyond which an entry of that kind is stale. Fractional values are allowed. **`0` or negative means never prune this kind** — a window of zero would delete a cache wholesale, which is never what someone typing `0` means. An unparseable value falls back to the default rather than to zero. |

Recognised kinds are `vim-cursor`, `vim-undo`, `vi-cursor` and `validators`. Unknown keys under `retention_days` are ignored; `gc` only ever touches directories it owns by name.

**The per-kind split is load-bearing, not decoration.** The vim caches hold per-file state whose value decays with the file's last edit, and 99% of the measured population was older than a week. `validators` is keyed by a content hash plus a tool fingerprint — it is invalidated by change, not by time, and was measured with *zero* entries older than 7 days. Giving all four the same window would evict a hot, correctly-sized cache to reclaim nothing. If you set them all to one number, set it to the largest one you are comfortable with, not the smallest.

`SUPERTOOL_GC_DISABLE=1` in the environment disables the automatic sweep for a single invocation, without touching config — that is what the test suite uses so a test run never reaps a developer's real cache. Since [#1656](https://github.com/Digital-Process-Tools/claude-supertool/issues/1656) the suite also points `XDG_CACHE_HOME` at a directory it owns for the whole session, so no cache supertool writes reaches the operator's `~/.cache` in the first place; the switch above stays because turning the sweep off and moving what it would sweep are different claims.

## Plain / ASCII output mode (hooks & CI)

Op output uses `⚠` / `✓` glyphs — nice UX for the model, a liability for anything that parses the output without UTF-8/locale guarantees (git hooks, `grep`, CI on a non-UTF-8 console). Pass `--plain` (or set `SUPERTOOL_PLAIN=1`) to emit ASCII-only output: `[WARN]` / `[OK]` / `[FAIL]` / `[INFO]` in place of the glyphs, with the stable section keys (`Red flags in added lines`, `Forbidden paths`, …) intact for grepping.

```bash
./supertool --plain 'git-diff:staged'        # flag
SUPERTOOL_PLAIN=1 ./supertool 'git-diff:staged'   # env (propagates to preset subprocesses)
```

The flag exports `SUPERTOOL_PLAIN=1` so preset ops (run as subprocesses) inherit it. Stdout/stderr are also reconfigured to UTF-8 at startup as cheap insurance, so a stray glyph in diffed content never crashes the process on a cp1252 console. Default (rich) output is unchanged.

## The wrapper — `./supertool`, and when it is not there

Moved from `README.md` by [#2142](https://github.com/Digital-Process-Tools/claude-supertool/issues/2142).

### A `cd` breaks `./supertool`

`./supertool` is a relative path. It resolves only from the directory holding the symlink, so a shell that has `cd`'d deeper into the repo — a test run in `tests/e2e`, or a `cd` that persists between an agent's tool calls — gets `no such file or directory: ./supertool` and no op runs at all. Nothing inside the tool can fix this: the wrapper has to be *found* before a single op is parsed, so even `cwd:PATH` as the first op of the call cannot help — that op is read by a process that already started.

```bash
supertool 'read:src/foo.py'                    # on $PATH (see Install in the README) — works from any directory
python3 /abs/path/to/supertool.py 'read:...'   # absolute path to the script
./supertool 'cwd:~/repo' 'read:...'            # only when ./supertool itself is reachable
```

Watch out for filtering the failure away: `./supertool '...' | grep -E 'state:'` from a directory with no wrapper prints **nothing**, which reads like an empty answer rather than a tool that never ran.

### A git worktree starts without one — and inside a supertool checkout it stays that way on purpose

The wrapper is a gitignored symlink that the session-start hook creates in the directory a session *starts* in. `git worktree add` makes a new directory in the middle of a session, so nothing ever creates one there. This is the same layer as the `cd` above and unfixable for the same reason: the wrapper has to be found before a single op is parsed, so no op — and no hook that already ran — can produce it.

The invocation that needs no wrapper at all is the one to reach for. It is what `git-push:watch` already falls back to when it finds no wrapper to spawn:

```bash
python3 /abs/path/to/claude-supertool/supertool.py 'read:...'   # worktree of any project
python3 supertool.py 'read:...'                                 # worktree of claude-supertool itself
```

**Inside a checkout of this repo, a session that does start there gets no wrapper either — deliberately.** Pointing a supertool checkout's wrapper at the plugin install runs **the plugin's core against this tree's config and presets**, and since the mixed-tree check every custom op through it answers `SKIPPED: ... comes from a different supertool tree` and exits 1; before that check, they answered `PASS` for code that never ran. So the session-start hook creates nothing here and says why, naming `python3 supertool.py` instead ([#711](https://github.com/Digital-Process-Tools/claude-supertool/issues/711)). An absent `./supertool` in a supertool checkout is the designed state, not a gap to fill.

That is a refusal, not a judgement about the local file. The hook never reads, verifies or links the `supertool.py` sitting next to it — treating "there is a file with that name here" as "this is a genuine checkout" is how [#688](https://github.com/Digital-Process-Tools/claude-supertool/issues/688) comes back. It decides only that a wrapper created *here* would be a broken one. In any other project the absolute link is correct and is *not* a mix, so nothing changes: the check fires only when the resolved project root holds a `supertool.py` of its own, which an ordinary repo does not.

If you want a wrapper anyway, **the target depends on whether the directory is a checkout of supertool** — and in a checkout only the relative link is correct:

```bash
ln -s "$CLAUDE_PLUGIN_ROOT/supertool.py" supertool   # worktree of any other project — absolute, outside the worktree
ln -s supertool.py supertool                         # worktree of claude-supertool — its own file, relative
```

**Path arguments are a separate question**, and that one is handled inside the tool. They resolve against the process cwd; when a call's paths only make sense from the project root, supertool chdirs there itself and says so (`[cwd auto-resolved to project root: ...]`) — provided an ancestor carries a `.supertool.json` and nothing in the call resolves locally. Where that evidence is ambiguous it does not guess: the `not found` error names the absolute path it tried and, if the file does exist under the project root, the exact `cwd:` prefix that would reach it.

## Windows and macOS/Linux platform notes

**Paths with spaces:** fine. Arguments arrive via `sys.argv` pre-tokenized by the shell, so `supertool "'read:/home/jo bob/file.py'"` works unchanged.

**Windows drive letters:** the tool recognizes `C:\...` and `D:/...` automatically and reassembles them after colon-splitting. So `supertool 'read:C:\Users\file.py'` and `supertool 'grep:needle:C:/src:20'` both parse correctly. If you hit edge cases, forward slashes (`C:/path`) work everywhere on Windows too.

**Temp/log location:** the call log uses `tempfile.gettempdir()` — macOS: `/var/folders/.../T/supertool-calls.log`, Linux: `/tmp/supertool-calls.log`, Windows: `%TEMP%\supertool-calls.log`.

**Windows and the raw-command guard, when there is no bash at all** ([#1378](https://github.com/Digital-Process-Tools/claude-supertool/issues/1378)): the guard is then *inert*, and a session where it never ran looks exactly like one where it ran and found nothing. This used to be read as "nothing for the raw-command guard to gate" on the grounds that Claude Code would have no `Bash` tool there — wrong by this repo's own [#1413](https://github.com/Digital-Process-Tools/claude-supertool/issues/1413): where the PowerShell tool is enabled Claude treats PowerShell as the primary shell and routes shell commands through it, which is why `hooks.json` matches `Bash|PowerShell`. There are commands to gate and no bash to gate them with.

No hook can disclose this — every hook here is a bash script, so a line inside one does not run on the host it would be describing. The check is one you run, and it needs no shell:

```
py -3 hooks/guard-selftest.py        # Windows
python3 hooks/guard-selftest.py      # anywhere else
```

It reports `enforcing`, `could not run` (naming what it tried) or `nothing to test`, and it says plainly that it cannot tell whether Claude Code invokes the hook — only whether this host can run it. Everything stated here about native `cmd.exe`/PowerShell is **reasoned, not observed**: nobody on this project has that host.

**Windows and the session-start hook, when there is no bash at all** ([#1401](https://github.com/Digital-Process-Tools/claude-supertool/issues/1401)): this is the half you notice. The guard's failure above is *silent* — it is asked, it cannot run, and a session where the gate never ran looks like one where it ran and found nothing. `SessionStart` is gated by nothing at all: it fires under any tool configuration, once per session, and its failure costs you something you can see. What you lose is the `./supertool` wrapper and the op roster the session normally opens with, so the model is not told which ops exist.

Nothing needs installing to get both back — call the tool by path, which needs no shell:

```
py -3 supertool.py 'ops:roster'        # Windows
python3 supertool.py 'ops:roster'      # anywhere else
```

Wherever these docs write `./supertool`, use that path instead. `hooks/guard-selftest.py` says all of this too, on the host itself.

**This gap is accepted and disclosed rather than fixed, deliberately.** Every candidate repair is a change to a host nobody here can run, shipped to every plugin user: adding `args` switches the hooks to exec form, where `command` *is* resolved on `PATH` — the `CreateProcess` search that finds System32's WSL launcher under the name `bash`, so the obvious repair introduces the defect. A second PowerShell entry is a non-zero hook on every POSIX session to serve one Windows one. A command string valid under both `sh -c` and PowerShell is a polyglot. And rewriting the hooks in Python does not help, because exec form would still have to name an interpreter: `python`/`python3` are the App Execution Alias stubs that block rather than error, the versioned names are absent on Windows, and `py -3` is absent everywhere else — which is exactly why the interpreter ladder exists, and the ladder is itself a shell script. Graded **reasoned, not observed** throughout; what is observed is only that both `hooks.json` entries name `bash`.

**Windows and the raw-command guard's interpreter:** `hooks/pre-bash-guard.sh` needs a Python it can name. Neither python.org's installer nor GitHub's `hostedtoolcache` creates `python3.9`–`python3.14`, so the guard falls back to `py -3`, the Windows Python launcher, after every versioned name and after an activated virtualenv. With no interpreter at all the guard does not silently pass: it says in the transcript that it could not run, and allows the command.
