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
| `read` | `max_bytes`   | 20000            | Max bytes per read (truncates at cap)                                                    |
| `grep` | `max_results` | 10               | Default result limit when not specified in the op                                        |
| `grep` | `max_line_chars` | 500           | Max chars per output line (match or context); remainder shown as `… (+N chars)`           |
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

## Parallel execution

Read-only ops in a batch can run concurrently. Output order is preserved (matches input order, not completion order).

Enable in `.supertool.json`:

```json
{ "parallel": 4 }
```

`parallel: N` runs up to N ops concurrently via a thread pool. `0` (default) = sequential. Boolean `true` is accepted as `4` for back-compat.

Override via env: `SUPERTOOL_PARALLEL=4 ./supertool 'read:a' 'grep:x:b/' 'glob:c/**'`. Env wins over JSON. Set `0` to force off for one call.

**Safe ops** (parallelized): `read`, `grep`, `glob`, `ls`, `head`, `tail`, `wc`, `stat`, `map`, `tree`, `around`, `around_line`, `between`, `diff`, `blame`, `version`.

**Unsafe** — batch falls back to sequential whenever any op is mutating (`edit`, `replace`, `replace_dry`, `replace_lines`) or custom (anything in `ops:` — could shell out to anything). All-or-nothing per call: no partial parallelism.

Speedup: I/O-bound ops on different files. ~3-5× faster on cold filesystem; modest gain on warm cache.

## Excluding paths from traversal ops

`glob`, `grep`, `tree`, and `map` walk the filesystem recursively. On large repos this can be slow and noisy — `.git/objects/`, `node_modules/`, `vendor/`, and similar dirs rarely contain what you're looking for.

Supertool prunes these at the **directory boundary** (never opens them), not after the fact.

**Built-in defaults** — always active unless overridden:

```
.git/  node_modules/  .svn/  .hg/  .idea/  .vscode/
__pycache__/  .venv/  venv/  dist/  build/
```

**Project-level additions** — add to `.supertool.json` under `ops.<op-name>.exclude-paths`. These are **merged additively** with the defaults (not replacing):

```json
{
  "ops": {
    "glob": { "exclude-paths": ["vendor/", "Dvsi/dvsi-private/libs/"] },
    "grep": { "exclude-paths": ["vendor/", "Dvsi/dvsi-private/libs/"] }
  }
}
```

**Per-call escape hatch** — append `:::no-exclude` to bypass all excludes for one call:

```bash
./supertool 'grep:somePattern:vendor/:10:::no-exclude'
./supertool 'glob:**/*.php:::no-exclude'
```

Ops that take explicit paths and don't traverse (`ls`, `read`, `head`, `tail`, `wc`, `stat`, `around`, `around_line`, `between`, `diff`, `blame`) are not affected — they always work on exactly the path you give them.

See [issue #4](https://github.com/Digital-Process-Tools/claude-supertool/issues/4) for the full design rationale.
