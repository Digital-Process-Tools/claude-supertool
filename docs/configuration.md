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
| `read` | `abstract`    | 0                | `1` → a read of a file over the threshold returns its tree-sitter symbol map instead of source, for every language in supertool's table. Falls back to source, with the reason, when the map is empty or no smaller. See [operations/reads.md](operations/reads.md#abstract-read) |
| `read` | `php_abstract` | 0               | Former name of `abstract`, from when the gate was `.php`. Still enables it; either key set to `1` is enough |
| `read` | `abstract_threshold_bytes` | `max_bytes` | File size above which the abstract read applies. Env: `SUPERTOOL_READ_ABSTRACT_THRESHOLD_BYTES` |
| `read` | `elide` | 1 | `0` → never elide a repeat read of an unchanged file. `SUPERTOOL_READ_NO_ELIDE=1` does the same for one call. Only fires when both reads share the same parent process, so under Claude Code it never fires between Bash tool calls ([#1352](https://github.com/Digital-Process-Tools/claude-supertool/issues/1352)). See [operations/reads.md](operations/reads.md#eliding-a-repeat-read) |
| `read` | `elide_window_seconds` | 900 | How long after content was last **returned** a byte-identical repeat may be elided. Measured from the last read that actually handed over bytes, never from the last elision. Env: `SUPERTOOL_READ_ELIDE_WINDOW_SECONDS` |
| `grep` | `max_results` | 10               | Default result limit when not specified in the op                                        |
| `grep` | `max_line_chars` | 500           | Max chars per output line (match or context); remainder shown as `… (+N chars)`           |
| `grep` | `count_ceiling` | 1000           | How far past `LIMIT` a truncated grep keeps counting so it can state `N matches total`. Past the ceiling it says `N+ … (count capped at N)` rather than a number it did not reach. Never applied below `LIMIT`. Env: `SUPERTOOL_GREP_COUNT_CEILING` |
| `grep` | `extensions`  | `[]` (all files) | Restrict grep to these file patterns (e.g. `["*.py", "*.js"]`). Empty = search all files |
| `around` | `max_bytes` | 16000            | Max bytes for an `around:` context window (truncates at a line boundary)                 |
| `grep_around` | `max_bytes` | 16000       | Max bytes for a `grep_around:` (and `grep:`-with-context) window                          |
| `glob` | `max_results` | 50               | Max files returned                                                                       |

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
| `{path}`    | Preset directory with trailing `/` (presets only) | `python3 {path}gitlab/issue.py {arg}` |

Use `{file}`/`{dir}` for file operations, `{arg}`/`{args}` for non-file arguments (issue numbers, job IDs, etc.).

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

Default on. A `PreToolUse` hook shipped with the plugin (`hooks/pre-bash-guard.sh`) refuses any `Bash` command an op declares it replaces, quoting the op's own description. The mapping is the `replaces` key on each op's registry entry — see the Op schema in [contributing.md](contributing.md).

```json
{
  "raw_command_guard": false
}
```

Set it to `false` to turn the gate off for a project. It is the **only** way off: there is no environment variable and no per-command flag, because an escape hatch that can be prepended to a command is not a block. A raw call nobody replaced is not blocked in the first place — `gh release create`, `gh api -X DELETE`, `git tag` have no `replaces` entry and never will unless an op supersedes them.

`supertool 'guard:SHELL COMMAND'` answers the same question without running anything, in three states: `BLOCKED` naming the op, `OK`, and `UNDECIDED` when the command did not tokenise, hid a substitution inside double quotes, handed a string to `eval` / `sh -c`, or the registry could not be enumerated. The hook allows on `UNDECIDED` and says so in the transcript — a gate that quietly did not run is indistinguishable from a command that complied, which is the whole reason it exists.

### Which interpreter the hook runs, and what it does when there is none

`hooks/pre-bash-guard.sh` tries `python3.14` down to `python3.9` on `PATH`, plus an activated virtualenv's own interpreter when `VIRTUAL_ENV` points at a directory containing `pyvenv.cfg`. The bare name `python3` is never tried: on Windows it can resolve to the App Execution Alias stub, which blocks rather than erroring, and this hook runs before every `Bash` call ([#572](https://github.com/Digital-Process-Tools/claude-supertool/issues/572)).

**`SUPERTOOL_PYTHON` is not read here**, and that is the "only way off" sentence above made true rather than merely written down ([#1390](https://github.com/Digital-Process-Tools/claude-supertool/issues/1390)). It was on the ladder, the only test applied to a candidate was that `-c pass` exited 0, and every binary on the box passes that — so `SUPERTOOL_PYTHON=/usr/bin/true` turned the gate off with no output and no disclosure. A candidate now has to identify itself as a Python 3 before it is run, and the variable does not participate: it exists for supertool's own op subprocesses, and a gate deciding whether a command may run is a different trust context.

**A candidate that runs and answers nothing reaches the third state**, not the clean one. The hook writes an envelope on every path including "nothing is replaced", so empty output from the interpreter means the guard did not answer, and the wrapper says so in the transcript and allows the command. Before this only an *empty ladder* produced that disclosure, so an interpreter that started and said nothing rendered exactly like a clean verdict.

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

**Safe ops** (parallelized): `read`, `grep`, `glob`, `ls`, `head`, `tail`, `wc`, `stat`, `map`, `tree`, `around`, `around_line`, `between`, `diff`, `blame`, `version`, `validate`, `validate_staged`, `workspace`, `resolve`, `diag`, `hover`, `help`. The list in `_supertool.py` is `_PARALLEL_SAFE_OPS`; this paragraph had been missing seven of them since they were added.

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

Ops that take explicit paths and don't traverse (`ls`, `read`, `head`, `tail`, `wc`, `stat`, `around`, `around_line`, `between`, `diff`, `blame`) are not affected — they always work on exactly the path you give them.

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
