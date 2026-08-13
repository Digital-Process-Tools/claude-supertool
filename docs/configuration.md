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

Entries document built-in operations (`syntax`, `description`, `example`). Set `"status": 0` to hide an entry from `./supertool 'ops'` output (works on `builtin-ops`, `ops`, and `aliases`). Besides documentation, `builtin-ops` entries can also override default behavior:

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

Any key in a custom op config that isn't a reserved key (`cmd`, `timeout`, `description`, `syntax`, `example`, `status`) is passed to the subprocess as a `SUPERTOOL_` prefixed environment variable:

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

Default on. A `PreToolUse` hook shipped with the plugin (`hooks/pre-bash-guard.sh`) refuses any `Bash` command an op declares it replaces, quoting the op's own description. It matches `Bash|PowerShell` since [#1413](https://github.com/Digital-Process-Tools/claude-supertool/issues/1413); a PowerShell command is never refused, because a POSIX tokeniser reading PowerShell quoting would deny the wrong commands. It is disclosed as undecided only when its text names a binary some op supersedes; a PowerShell call naming nothing mapped gets no line at all, since a disclosure printed under every call is one nobody reads. **That does not reach a native-Windows host with no Git Bash**, where the `bash` in the hook command resolves to the WSL launcher stub and the hook never executes at all — [#1378](https://github.com/Digital-Process-Tools/claude-supertool/issues/1378) is that gap and is unaffected by this. The mapping is the `replaces` key on each op's registry entry — see the Op schema in [contributing.md](contributing.md).

```json
{
  "raw_command_guard": false
}
```

Set it to `false` to turn the gate off for a project. It is the **only** way off: there is no environment variable and no per-command flag, because an escape hatch that can be prepended to a command is not a block. A raw call nobody replaced is not blocked in the first place — `gh release create`, `gh api -X DELETE`, `git tag` have no `replaces` entry and never will unless an op supersedes them.

**A repository can wire the guard itself instead of waiting for a plugin release.** The hook ships in the plugin's `hooks.json`, so it becomes active for a checkout only when the installed plugin carries it — a claude-supertool clone whose plugin install predates the hook has the mapping and no enforcement. Adding it as a `PreToolUse` hook in the project's own `.claude/settings.json` closes that gap, and is how this repository dogfoods its own guard ([#1376](https://github.com/Digital-Process-Tools/claude-supertool/issues/1376)):

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

`supertool 'guard:SHELL COMMAND'` answers the same question without running anything, in three states: `BLOCKED` naming the op, `OK`, and `UNDECIDED` when the command did not tokenise, hid a substitution inside double quotes, handed a string to `eval` / `sh -c`, carried a pre-subcommand global option the matcher has no grammar for ([#1421](https://github.com/Digital-Process-Tools/claude-supertool/issues/1421)), or the registry could not be enumerated. The hook adds one more of its own: a command routed through the **PowerShell** tool, whose quoting a POSIX tokeniser cannot read ([#1413](https://github.com/Digital-Process-Tools/claude-supertool/issues/1413)). The hook allows on `UNDECIDED` and says so in the transcript — a gate that quietly did not run is indistinguishable from a command that complied, which is the whole reason it exists.

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

**A candidate that runs and answers nothing reaches the third state**, not the clean one. The hook writes an envelope on every path including "nothing is replaced", so empty output from the interpreter means the guard did not answer, and the wrapper says so in the transcript and allows the command. Before this only an *empty ladder* produced that disclosure, so an interpreter that started and said nothing rendered exactly like a clean verdict.

### How the harness resolves the hook command, and why a bare `bash` is right

Both shipped hooks are registered as `bash "${CLAUDE_PLUGIN_ROOT}/hooks/…"` with no `args`, which is Claude Code's **shell form**: the string is handed to a shell — `sh -c` on macOS and Linux, Git Bash on Windows, or PowerShell when Git Bash is not installed. It is *not* tokenised and handed to a direct spawn, so `bash` is not resolved by `CreateProcess` searching `PATH` — which on Windows would find System32's `bash.exe`, the WSL launcher, a program that is not a shell and exits 1 with UTF-16 output without ever opening the script ([#1401](https://github.com/Digital-Process-Tools/claude-supertool/issues/1401)).

Adding `args` would switch these hooks to **exec form**, where `command` *is* resolved as an executable on `PATH` and spawned directly — so the obvious-looking repair introduces the defect rather than removing it. `tests/test_hook_interpreter_windows_1401_1402.py` refuses it.

The remaining branch is a native Windows host with no Git for Windows, where the string reaches PowerShell and `bash` there can find the WSL launcher. That is also the host where Claude Code has **no `Bash` tool at all** — it routes shell commands through the PowerShell tool instead. This paragraph used to conclude that a `PreToolUse` hook therefore never fires there, which was wrong by this repo's own [#1413](https://github.com/Digital-Process-Tools/claude-supertool/issues/1413): the matcher has been `Bash|PowerShell` since, precisely because Claude treats PowerShell as the primary shell where that tool is enabled. So the guard hook *is* asked on that host and is merely **inert** — [#1378](https://github.com/Digital-Process-Tools/claude-supertool/issues/1378)'s silent state, which `hooks/guard-selftest.py` exists to break. `SessionStart` is gated by nothing at all, fires under any tool configuration, and loses its `./supertool` symlink and its op roster; that is a real gap, and it is a gap in what a `.sh` hook can do on a host with no POSIX shell, not something a different command string fixes.

**A Python rewrite does not escape that**, and it is worth writing down because it is the repair the issue names first. Exec form would still have to name an interpreter, and no name is portable: `python` and `python3` are the App Execution Alias stubs [#572](https://github.com/Digital-Process-Tools/claude-supertool/issues/572) banned for blocking rather than erroring, `python3.9`–`python3.14` do not exist on a standard Windows install ([#1402](https://github.com/Digital-Process-Tools/claude-supertool/issues/1402)), and `py -3` exists nowhere else. Resolving that is exactly what `hooks/python-ladder.sh` does, and it is a shell script — so it cannot bootstrap a host with no shell.

**The disclosure has a channel on the affected host, and it covers both hooks** ([#1401](https://github.com/Digital-Process-Tools/claude-supertool/issues/1401)). `hooks/guard-selftest.py` needs no shell, and its `could not run` state is one fact about two dead hooks: it names `session-start.sh` alongside the guard — whose failure is the silent one, where the session hook's is a wrapper and a roster you can see are missing — says the session gets no `./supertool` wrapper and no op roster, and gives the way back — `py -3 supertool.py 'ops:roster'`, and reading `./supertool` in the docs as that path. Reporting only the guard left the more visible loss with no account of itself, which is the same absence-read-as-absence one layer up.

**That gap is disclosed rather than fixed, and the grades are these** ([#1401](https://github.com/Digital-Process-Tools/claude-supertool/issues/1401)). *Reasoned from Claude Code's own hook and setup documentation, not observed:* that shell form reaches PowerShell where Git Bash is absent, and that the `Bash` tool is absent on the same host. *Observed:* a bare `bash` under `CreateProcess` on the Windows runners is the WSL launcher, in this repository's own pytest. Nobody on this project has a Windows box, so every candidate repair was reasoning about an untestable host: `args` introduces the exec-form PATH search this section refuses, a second PowerShell entry breaks every POSIX session to serve one Windows one, and a shell string valid under both `sh -c` and PowerShell is a polyglot shipped to every user of the plugin on the strength of a Mac. The disclosure is the honest outcome, and it is what CI's twelve legs can never tell you either.

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
| **Built-in ops** | still run — `read`, `grep`, `edit` come from the core that was invoked. A one-line disclosure goes to stderr so the operator knows whose validators and formatters are loaded. |
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

`SUPERTOOL_GC_DISABLE=1` in the environment disables the automatic sweep for a single invocation, without touching config — that is what the test suite uses so a test run never reaps a developer's real cache.
