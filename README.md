<p align="center">
  <img src="supertool-banner.webp" alt="SuperTool — cut your Claude Code bill by 50%" width="900">
</p>

# supertool

> **Cut your Claude Code bill by 50%.**
> `git-status`, but it tells you what to do next.

Saves tokens. Saves money. Saves turns. Works the same in interactive sessions and autonomous runs — humans pair-programming with Claude Code use it every day, not just Kevin-style headless agents. One Python file, zero deps, Python 3.9+.

[Why](#why) • [Four pillars](#four-pillars) • [Receipt](#receipt--the-bill-math) • [Batching](#batch-multiple-ops-in-one-call) • [Parallel](#parallel-execution) • [Input forms](#input-forms) • [Validators](#validators--squiggle-on-save-for-the-llm) • [Expand it](#supertooljson--project-configuration) • [Install](#install)

```bash
# 7 ops, 1 round-trip, parallel where safe
supertool 'read:src/Module.py' 'read:src/Auth.py' 'grep:TODO:src/:20' 'map:src/'
```

---

## Why

**Hammer in 2026.** Claude Code's default toolbelt is 1995 unix: `cat` one file, `grep` one pattern, `git status` returns 200 bytes of porcelain. Every tool call re-sends the entire conversation cache — system prompt, CLAUDE.md, rules, every prior turn — at 10% of input price. Read 7 files? Pay that prefix 7 times. Run `git status` then realize you needed `ahead/behind` too? Pay it twice for one decision. The bill compounds turn over turn.

**Drill in 2026.** supertool gives the agent variants that pack the *next question* into the *current call*:

- **`git-status`** — branch + tracking + ahead/behind + dirty files + open MR/PR + suggested next step. One call, decision ready.
- **`gl-mr:NUMBER`** / **`gh-pr:NUMBER`** — full MR/PR dashboard: branch, pipeline, reviewer, approval, diff stat, comments. Replaces 4-5 `glab`/`gh` calls.
- **`claude-log-summary:UUID`** — model, duration, tool calls, tokens, cache hit %, errors-by-tool. Audit your own runs.

That's a sample. supertool ships ~40 ops out of the box (built-ins + `gitlab` / `github` / `git` / `claude-log` presets) — add your own and you're past 60 fast.

The variant *is* the lever. A turn saved isn't free time — it's a cached prefix you didn't re-pay.

## Four pillars

| Pillar           | What it does                                                                     |
| ---------------- | -------------------------------------------------------------------------------- |
| **Right tool**   | Variants pack state + guards + next-step into one call. Less to remember.        |
| **Batched**      | 7 ops, 1 round-trip. The cached prefix gets re-paid once, not seven times.       |
| **Parallel**     | Read-only ops in a batch run concurrently — ~3-5× faster on cold I/O.            |
| **Expandable**   | Add a custom op in 4 lines of JSON. Presets ship gitlab, github, git, claude-log. |

## Receipt — the bulldozer math

| Mode                     | Cache reads | Output | Turns |    Savings |
| ------------------------ | ----------: | -----: | ----: | ---------: |
| Hammer (no batching)     |        436K |  1,400 |    10 |          — |
| supertool                |        133K |    750 |     3 |    **50%** |
| Pre-computed + supertool |       85.5K |    600 |     2 |    **56%** |

**50% fewer tokens, 3-4× faster wall time.** Fewer turns = fewer prefix re-reads. Multiply by task count and team size — the bill cut is real.

## What this means in practice

Three things happen once you ship variants instead of raw shell:

**1. You build your own ops.** [Digital Process Tools](https://digital-process-tools.com) built a stack on top — none ship with supertool, all written in 5-15 lines of JSON: `git-commit` (stage + commit + receipt), `mr` (push + MR + reviewer), `mysql_read`/`mysql_write`, `verify_staged` (phpstan + phpmd + phplint on the staged diff). Every project has its own "what's the next question I always ask" — bake the answer in, save the round-trip forever.

**2. The op holds the guards.** `mysql_write` refuses `UPDATE`/`DELETE` without `WHERE`. `mysql_read` auto-`LIMIT 50`s. `mr` can enforce branch policy and reviewer. Every guard is a class of mistake the agent *can't* make. Tokens saved, yes — but the session that didn't get derailed cleaning up "oops, emptied the user table" is the expensive one.

**3. The agent thinks less.** A variant that returns everything in one shot is a variant the agent doesn't have to *think through*. Thinking tokens bill at output rate. Every "let me also check..." that becomes "the op already told me" is output cost saved on top of round-trip cost.

---

## Install

From the DPT marketplace:

```
/plugin marketplace add Digital-Process-Tools/claude-marketplace
/plugin install supertool@dpt-plugins
```

This auto-registers both hooks (`SessionStart` + `PreToolUse`) via the plugin's `hooks/hooks.json` — no manual `settings.json` editing.

Or directly — clone the repo and symlink `supertool.py` onto your `$PATH` as `supertool`:

```bash
git clone https://github.com/Digital-Process-Tools/claude-supertool.git
ln -s "$(pwd)/claude-supertool/supertool.py" /usr/local/bin/supertool
chmod +x /usr/local/bin/supertool
```

Verify:

```bash
supertool 'read:README.md'
```

Standalone install doesn't wire up the hooks (no plugin system). You get the binary; the enforcement mode and session-start prompt come with the marketplace install.

---

## How to use

### Interactive (permissive mode — default)

Just install. The session-start hook runs `./supertool 'introduction' 'output-format' 'ops-compact'` to output the project-specific operations reference from `.supertool.json`. The model learns what's available and how to batch. Falls back to native `Grep`/`Read` when those are better.

> **Heads-up — hook output cap.** Claude Code truncates hook stdout around 7KB; over that, only a ~2KB preview reaches the model and the rest is silently saved to disk. With many ops, the tail of the listing gets hidden until rediscovered mid-task.
>
> The session-start hook uses `ops-compact` to stay under the cap: examples are dropped on self-explanatory ops, and only kept on ops marked `"hint": true` in `.supertool.json`. If the body still exceeds the cap, `ops-compact` prepends a warning telling the model to fetch the full listing via `./supertool 'ops'`. Plain `'ops'` always returns everything.

### Autonomous / headless (enforced mode)

For Kevin-style runs where you want the model to **always** batch via SuperTool:

```
/supertool on
```

This writes `~/.claude/supertool-enforced`, which the PreToolUse hook reads to block:

- `Grep`, `Glob`, `LS` (native builtins)
- `Bash(cat ...)`, `Bash(find ...)`, `Bash(grep ...)`, `Bash(ls ...)`
- `Bash(sed ...)`, `Bash(awk ...)`, `Bash(tail ...)`, `Bash(head ...)`

Blocked calls receive a redirect message ("Use `./supertool` instead: ..."). Model learns to batch.

**Read stays allowed** — Claude Code's Edit tool needs the built-in Read for state-based file checks. Don't try to disable it.

Turn off when you're done:

```
/supertool off
```

Check state:

```
/supertool status
```

### `--disallowedTools` alternative (CLI)

If you're running `claude -p` in bypass mode, you can use the CLI flag directly (plugin not required):

```bash
claude -p "..." --permission-mode bypassPermissions \
  --disallowedTools "Grep,Glob,LS,Bash(find:*),Bash(cat:*),Bash(grep:*),Bash(ls:*),Bash(sed:*),Bash(awk:*),Bash(tail:*),Bash(head:*)"
```

`--allowedTools` is [ignored in bypass mode](https://github.com/anthropics/claude-code/issues/12232) — always use `--disallowedTools` when bypassing.

---

## Operations

| Op | Syntax | Notes |
|----|--------|-------|
| `read` | `read:PATH` or `read:PATH:OFFSET:LIMIT` | 300 lines / 20KB cap |
| `read` (filter) | `read:PATH:OFFSET:LIMIT:grep=PATTERN` | Only show lines matching PATTERN (original line numbers preserved). Use `read:PATH:::grep=PATTERN` for defaults. |
| `grep` | `grep:PATTERN:PATH` or `grep:PATTERN:PATH:LIMIT` | 10 results default, code + doc extensions only. **Auto-reads** full file if PATH is a concrete file < 20KB with a match. |
| `grep` (context) | `grep:PATTERN:PATH:LIMIT:CONTEXT` | Show CONTEXT lines before/after each match (like `grep -C`). Match lines: `path:lineno:content`. Context lines: `path-lineno-content`. Non-adjacent groups separated by `--`. |
| `grep` (count) | `grep:PATTERN:PATH:LIMIT:CONTEXT:count` | Return match counts per file instead of content. Output: `filepath:COUNT` per line. |
| `glob` | `glob:PATTERN` | `**` supported. **Auto-reads** if PATTERN is a concrete file path (no wildcards). |
| `ls` | `ls:PATH` | Trailing `/` on subdirs |
| `tail` | `tail:PATH:N` | Last N lines (default 20) |
| `head` | `head:PATH:N` | First N lines (default 20) |
| `wc` | `wc:PATH` | Line/word/char count (like unix `wc`). Output: `LINES WORDS CHARS PATH`. |
| `around` | `around:PATTERN:PATH` or `around:PATTERN:PATH:N` | Show N lines (default 10) before and after the **first** match of PATTERN in a single file. Uses line-numbered output like `read`. |
| `grep_around` | `grep_around:PATTERN:PATH` or `grep_around:PATTERN:PATH:N:LIMIT` | Every match across files with N lines context (default N=3, LIMIT=10). Alias for `grep:PATTERN:PATH:LIMIT:CONTEXT` with sane defaults — useful for "show me how everyone uses this". |
| `map` | `map:PATH` | Symbol map of a file or directory. Shows classes, functions, methods, constants as an indented tree with line numbers. Three-tier: tree-sitter → ctags → regex. Supports PHP, Python, JS, TS, Go, Rust, Java, Ruby. |
| `introduction` | `introduction` | Output the project introduction text from `.supertool.json`. No `---` dispatch header — clean markdown. |
| `output-format` | `output-format` | Output format examples from `.supertool.json`. Shows what responses look like. |
| `ops` | `ops` | Full operations reference from `.supertool.json` — built-in ops, custom ops, and aliases with descriptions and examples. |
| `diff` | `diff:PATH1:PATH2` | Unified diff between two files. |
| `stat` | `stat:PATH` | File/directory metadata: size (bytes), last modified (ISO datetime), type (file/dir). |
| `around_line` | `around_line:PATH:LINE` or `around_line:PATH:LINE:N` | Show N lines (default 10) of context around a specific line number. Target line marked with `→`. |
| `between` | `between:SYMBOL:PATH` or `between:re:START:END:PATH` | Return a chunk of a file. **Symbol mode (default):** full body of a named function/method/class via tree-sitter (PHP, Python, JS, TS, Go, Rust, Java, Ruby — symbols with `::` like PHP `Foo::bar` work). **Pattern mode (`re:` prefix):** inclusive line slice from first line matching START regex to first line after matching END regex (language-agnostic). |
| `tree` | `tree:PATH` or `tree:PATH:DEPTH` | Directory structure with depth limit (default 3). Hides dotfiles. Files listed before subdirectories. |
| `blame` | `blame:PATH:LINE` or `blame:PATH:LINE:N` | Git blame for N lines (default 5) around a specific line number. Requires git repo. |
| `version` | `version` | Show supertool version. |
| `edit` | `edit:::OLD:::NEW:::PATH` | Single-file, single-occurrence edit (mirrors native Edit). Errors if 0 or >1 matches. **Bypasses native Edit must-Read state** — saves a round-trip when you already know the unique snippet. Use `:::` separator so content with `:` works. |
| `replace_lines` | `replace_lines:::PATH:::START:::END:::CONTENT` | Swap lines `[START, END]` (1-indexed, inclusive) with CONTENT. `END < START` = pure insert before line START. Empty CONTENT = delete. Receipt shows new line numbers + ±2 context. |
| `paste` | `paste:::PATH:::CONTENT` | **NARROW USE:** replace ENTIRE file. Only for creating a new file or fully rewriting one. NOT for partial edits — `vim` is the default for those. Atomic, creates file + parent dirs if missing. CONTENT via triple-colon → holds any chars (`:`, quotes, braces, newlines). |
| `vim` | `vim:::PATH:::SCRIPT` | vim-flavored cursor-based multi-action edit. SCRIPT is parsed like a real vim macro: chars are verbs in NORMAL mode; insert verbs (`i`/`a`/`A`/`I`/`o`/`O`), search (`/`/`?`), and ex (`:...`) enter "greedy" modes where all following chars are TEXT/PAT until `\e` (ESC, U+001B) returns to NORMAL. **No separator chars** — `;`, `{`, `}`, newlines etc. are literal data, never special. **Cursor:** `gg`/`G` (BOF/EOF), `nG` (goto line), bare `:N`/`:$`/`:.` (line goto), `0`/`$` (BOL/EOL), `/PAT`/`?PAT` (search fwd/bwd), `nh`/`nl`/`nj`/`nk` (move). **Inserts** (TEXT runs until `\e` or EOS, `\n`/`\t` decoded): `iTEXT`/`aTEXT`/`ITEXT`/`ATEXT`/`oTEXT`/`OTEXT`. `o`/`O` AUTO-INDENT first line to current line's indent. **Deletes:** `x`/`nx`, `dd`/`ndd`, `D`. **Ex:** `:s/PAT/REPL/[gid]` (literal-fallback on unescaped parens), `:%s`, `:Nd`/`:N,Md`/`:.,/PAT/d`, `:g/PAT/d`/`:v/PAT/d` (bare `g/PAT/d` and `%g/PAT/d` autoprefixed with `:`), `:r FILE`/`:r -`/`:Nr FILE`, `:Na\nBODY\n.` (ex append after line N), `:w`/`:wq` (no-op — supertool writes atomically). **Autocorrects:** `oi<ws>TEXT` strips redundant verb-bleed, `:%%d` → `:%d`, `V<N>Gd` → `:.,Nd`, abandoned range `64,` silently dropped. **DEFAULT EDIT OP** for any pattern-based edit. Only fall back to `paste`/`edit`/`replace_lines` when vim doesn't fit. |
| `replace` / `replace_dry` | `replace:::OLD:::NEW:::PATH` | Recursive find/replace across PATH (`replace_dry` = preview). Use `:::` separator when content has `:`. |

**LLM onboarding in one call:** `./supertool 'introduction' 'output-format' 'ops'` — outputs everything an LLM needs to use supertool.

---

## Input forms

### Colon-CLI (default)

Most ops take arguments via `:` — `read:PATH:OFFSET:LIMIT`, `grep:PATTERN:PATH:LIMIT`. When content itself contains colons (code, SQL, timestamps), switch to `:::` triple-colon separators: `edit:::OLD:::NEW:::PATH`.

### `@file` route — long or structured payloads

Edit ops (`edit`, `replace_lines`, `paste`, `vim`) accept a JSON file instead of inline args. Pass the path with an `@` prefix:

```bash
./supertool 'edit:@.max/my-edit.json'
```

The file holds the fields that would otherwise go after `:::`:

```json
{ "old": "return false;", "new": "return true;", "path": "src/app/Foo.py" }
```

Use `@-` to read the payload from stdin instead of a file.

This route shines when `old` or `new` spans multiple lines or contains characters that clash with shell quoting. Write the JSON, invoke once — no escaping gymnastics.

### `batch:@file` — mixed ops in one round-trip

`batch` runs any combination of read and write ops from a single JSON file:

```bash
./supertool 'batch:@.max/ops.json'
```

Payload — a bare array of op objects, or a wrapper with options:

```json
[
  { "op": "read", "path": "src/app/Config.py" },
  { "op": "edit", "old": "DEBUG = False", "new": "DEBUG = True", "path": "src/app/Config.py" },
  { "op": "read", "path": "src/app/Config.py" }
]
```

Or with explicit options:

```json
{
  "continue_on_error": true,
  "ops": [
    { "op": "read", "path": "src/app/Config.py" },
    { "op": "edit", "old": "DEBUG = False", "new": "DEBUG = True", "path": "src/app/Config.py" }
  ]
}
```

`continue_on_error` defaults to `true` — a failed op is reported but the rest of the batch continues. Set to `false` to abort on first error. Validators (phplint, xmllint, etc.) fire per mutating op, same as inline edits. Use `@-` to pipe the payload from stdin.

---

## Validators — squiggle-on-save for the LLM

Every mutating op (`edit`, `replace`, `replace_lines`, `paste`, `vim`) runs matching validators on the result. Syntax fail → atomic rollback. The model gets an immediate error receipt and retries cleanly.

Example: edit a `.json` file with a missing comma → `jsonlint` catches it → file reverts → receipt shows the parse error with line/col.

17 validators bundled out of the box (PHP, XML, JSON, YAML, INI, Python, Bash, JS, TS, SCSS, Markdown, Ruby, Dockerfile, Go, Terraform, Rust, TOML). Graceful skip when toolchain missing.

Full reference: [docs/validators.md](docs/validators.md) — bundled list, how they hook in, adding your own.

---

### `.supertool.json` — project configuration

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
      "syntax": "grep:PATTERN:PATH[:LIMIT[:CONTEXT]]",
      "description": "Search (10 results def). CONTEXT=N lines around match",
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

**`introduction` and `output-format`** are user-controlled strings output by meta-ops:

```bash
./supertool 'introduction'        # prints the introduction string
./supertool 'output-format'       # prints the output-format string
./supertool 'introduction' 'output-format' 'ops'   # full LLM onboarding in one call
```

Use this in session-start hooks or agent prompts to onboard LLMs to your project's supertool setup without reading config files manually.

**`builtin-ops`** entries document built-in operations (`syntax`, `description`, `example`). Set `"status": 0` to hide an entry from `./supertool 'ops'` output (works on `builtin-ops`, `ops`, and `aliases`). Besides documentation, `builtin-ops` entries can also override default behavior:

| Op | Key | Default | Effect |
|----|-----|---------|--------|
| `read` | `max_lines` | 300 | Max lines per read |
| `read` | `max_bytes` | 20000 | Max bytes per read (truncates at cap) |
| `grep` | `max_results` | 10 | Default result limit when not specified in the op |
| `grep` | `extensions` | `[]` (all files) | Restrict grep to these file patterns (e.g. `["*.py", "*.js"]`). Empty = search all files |
| `glob` | `max_results` | 50 | Max files returned |

Example — increase read cap and restrict grep to PHP/XML:

```json
{
  "builtin-ops": {
    "read": { "max_lines": 500, "max_bytes": 40000 },
    "grep": { "extensions": ["*.php", "*.xml"] }
  }
}
```

**`ops`** are custom shell commands called directly by name:

```bash
./supertool 'mypy:src/app/Module.py' 'pytest:tests/test_module.py'
```

Each op has `cmd`, `timeout`, `description`, `example`, and optional `status`. Ops accept `{file}` and `{dir}` (dirname of file) placeholders. Shorthand string ops (`"lint": "ruff check {file}"`) still work with a 60s default timeout.

**`aliases`** expand one name to multiple ops. Format changed from array to object:

```bash
./supertool 'verify:src/app/Module.py'   # runs mypy + lint in one round-trip
```

Each alias has `ops` (array), `description`, `example`, and optional `status`. Aliases don't recurse.

**Dispatch order:** built-in ops → custom ops (including preset ops) → aliases. Built-ins always win. Project ops override preset ops on name conflict.

#### Placeholders in custom ops and aliases

| Placeholder | Expands to | Example |
|-------------|-----------|---------|
| `{file}` | First argument, shell-quoted, treated as file path | `cat {file}` |
| `{dir}` | Directory of `{file}` | `ls {dir}` |
| `{arg}` | First argument, shell-quoted, no path validation | `glab issue view {arg}` |
| `{args}` | All arguments, each shell-quoted | `python3 tool.py {args}` |
| `{path}` | Preset directory with trailing `/` (presets only) | `python3 {path}gitlab/issue.py {arg}` |

Use `{file}`/`{dir}` for file operations, `{arg}`/`{args}` for non-file arguments (issue numbers, job IDs, etc.).

#### Extra config keys as environment variables

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

### Presets — reusable op packs

Presets are JSON files that declare custom ops for a specific tool or platform. Enable them in `.supertool.json`:

```json
{
  "presets": ["gitlab"]
}
```

Supertool looks for each preset in three locations (first found wins):

1. `./presets/{name}.json` — project-level (team-specific ops)
2. `~/.config/supertool/presets/{name}.json` — user-level (personal ops)
3. `{supertool install dir}/presets/{name}.json` — shipped with supertool

Preset ops merge into your config. Project-level ops always override preset ops on name conflict.

#### Shipped presets

Supertool ships with presets for `gitlab`, `github`, `git`, `claude-log`, `xml`, `hashnode`, `devto`, and `bluesky`. Each preset has a dedicated reference page in [`docs/presets/`](docs/presets/index.md) covering ops, syntax, common workflows, required env vars, and authoring notes.

#### Writing your own preset

Create `./presets/mytools.json` in your project (or `~/.config/supertool/presets/mytools.json` for personal use):

```json
{
  "description": "My team's deployment tools",
  "requires": "kubectl",
  "ops": {
    "deploy-status": {
      "cmd": "python3 {path}mytools/status.py {arg}",
      "timeout": 15,
      "description": "Check deployment status for a service.",
      "syntax": "deploy-status:SERVICE"
    }
  }
}
```

The `{path}` placeholder resolves to the preset JSON's directory, so scripts can live alongside the manifest. The `requires` field is documentation only (not enforced).

Then enable it:

```json
{
  "presets": ["mytools"]
}
```

#### Legacy `check:` syntax

The `check:PRESET:PATH` op still works — it reads from the `ops` section first, then falls back to `.supertool-checks.json` for backward compatibility. New projects should use direct ops (`mypy:file`) instead of `check:mypy:file`.

### `map` — symbol extraction

`map:PATH` generates a symbol tree (classes, functions, methods, constants) for a file or directory. Three-tier extraction — uses the best available tool:

| Tier | Detection | What you get |
|------|-----------|-------------|
| 1. tree-sitter | `tree_sitter_language_pack` or `tree_sitter_languages` importable | Full AST: accurate nesting, signatures, all node types |
| 2. ctags | `ctags` on PATH (universal-ctags) | JSON tags: class/method/function/constant with scope |
| 3. regex | Always available | Pattern matching: `class`, `function`, `def`, `interface`, `trait`, `enum`, `const`, `struct`, `impl` |

Supported languages: PHP, Python, JavaScript, TypeScript (+ JSX/TSX), Go, Rust, Java, Ruby.

```bash
# Single file
supertool 'map:src/Module.php'

# Directory (recursive, skips vendor/.git/Generated/node_modules)
supertool 'map:src/SiProject/'
```

Output:

```
src/SiProject/SiProjectModule.class.php (55 lines)
  class SiProjectModule  [31]
    const TYPE_PRIMARY  [39]
    const MENU_ITEM  [42]
    method init  [48]
```

Install optional deps for richer output:

```bash
# tree-sitter (best — full AST, Python 3.10+)
pip install tree-sitter-language-pack

# OR ctags (good — works everywhere)
brew install universal-ctags   # macOS
apt install universal-ctags    # Linux
```

Without either, regex fallback works for all supported languages — just no nesting detection (except Python indentation).

### Compact mode

Set `"compact": true` in `.supertool.json` to enable compact reads. When enabled, `read` ops skip blank lines and comment-only lines (`//`, `#`, `/* */`, `<!-- -->`, PHPDoc `*` lines), preserving original line numbers. Reduces token cost for exploration without losing structure.

Compact is disabled when using `grep=` filter or `offset` (editing needs exact lines).

### Parallel execution

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

### Excluding paths from traversal ops

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

### RTK integration

When [rtk](https://github.com/reachingforthejack/rtk) is installed, supertool automatically delegates `read`, `grep`, and `wc` to RTK for compressed output. No configuration needed — detected via `which rtk` at first use.

- With RTK + compact: uses `rtk read --level aggressive` (maximum compression)
- With RTK, no compact: uses `rtk read` (RTK formatting, no stripping)
- Without RTK + compact: native regex-based blank/comment stripping
- Without RTK, no compact: supertool's own output (default)

RTK is optional. Supertool works identically without it — RTK is just an accelerator.

### tree-sitter integration

When [`tree-sitter-language-pack`](https://pypi.org/project/tree-sitter-language-pack/) (Python 3.10+) or [`tree-sitter-languages`](https://pypi.org/project/tree-sitter-languages/) (Python 3.8–3.12) is installed, `map` uses tree-sitter for AST-based symbol extraction instead of ctags or regex.

- Detects installed package at first `map` call (cached for session)
- Prefers `tree-sitter-language-pack` over `tree-sitter-languages` when both are present
- Falls back to ctags → regex when neither is installed
- No configuration needed — pure detection

tree-sitter is optional. The `map` op works without it — tree-sitter just gives more accurate nesting and signature details.

### Batch multiple ops in one call

**Six or seven ops per call is routine; two is too few.**

```bash
supertool \
    'read:src/Module.py' \
    'read:src/Permissions.py' \
    'read:src/Options.py' \
    'grep:extends:src/:20' \
    'grep:@related:src/:10' \
    'glob:src/Components/**/*.xml' \
    'glob:src/EventsManagers/*.py'
```

One round-trip. Seven ops worth of output. The session-start hook reminds the model of this each session.

---

## Anti-patterns the tool catches

The tool **auto-promotes** these wasted patterns silently, but you should still recognize them and batch up front:

- `glob:concrete/path.xml` followed by `read:concrete/path.xml` — glob on a path with no wildcards is useless; just `read:`. SuperTool auto-reads it.
- `grep:FOO:single_file.py` followed by `read:single_file.py` — same file, two turns. SuperTool auto-reads if the file is < 20KB with a match.
- A second SuperTool call whose ops could have fit in the first.

**Self-check:** if the output contains `[auto-read: ...]`, SuperTool just salvaged a wasted turn you asked for. Tighten your next prompt to batch up front.

---

## Measuring adoption

Every SuperTool call is logged to `/tmp/supertool-calls.log` with this format:

```
2026-04-16 21:05:42 | user=alice ppid=74394 entry=cli | ops=3 out=12400b | read:a.py read:b.py grep:X:src/:20
```

Fields:

- `user=` — the shell user
- `ppid=` — parent process (stable within one Claude Code session, useful for grouping)
- `entry=` — how Claude Code was invoked (`cli`, `sdk`, etc.)
- `ops=N` — number of ops in this call
- `out=Nb` — output bytes emitted to the model

### Single-op rate (adoption signal)

```bash
awk -F'|' '{ for (i=1;i<=NF;i++) if ($i ~ /ops=/) print $i }' /tmp/supertool-calls.log \
  | sort | uniq -c | sort -rn
```

A healthy run has most calls at `ops=3+`. A run dominated by `ops=1` means the model is using SuperTool but not batching — tighten the system prompt.

### Estimated savings vs. no-batching baseline

```bash
awk -F'|' '
  { for (i=1;i<=NF;i++) if ($i ~ /ops=/) { gsub(/[^0-9]/,"",$i); t+=$i; n++ } }
  END { printf "%d ops in %d calls → %d round-trips saved vs all-single\n", t, n, t-n }
' /tmp/supertool-calls.log
```

Each saved round-trip avoids one prefix cache re-read. The bigger your prefix, the bigger the saving per trip.

---

## Contributing

Run the suite:

```bash
python3 -m pytest tests/
```

293 tests, 80% minimum coverage (enforced by pytest-cov). Current: 94%.

Enable the pre-push hook (runs pytest + enforces 80% coverage before every push):

```bash
git config core.hooksPath .githooks
```

The hook is in `.githooks/pre-push`, committed to the repo. Bypass with `git push --no-verify` (discouraged).

---

## Platform compatibility

**Linux/macOS:** works out of the box.

**Windows:** works via Git Bash or WSL (the plugin's `hooks/session-start.sh` + `.githooks/pre-push` are bash scripts; the Python tool itself is cross-platform). Native `cmd.exe` / PowerShell without bash won't fire the hooks.

**Paths with spaces:** fine. Arguments arrive via `sys.argv` pre-tokenized by the shell, so `supertool "'read:/home/jo bob/file.py'"` works unchanged.

**Windows drive letters:** the tool recognizes `C:\...` and `D:/...` automatically and reassembles them after colon-splitting. So `supertool 'read:C:\Users\file.py'` and `supertool 'grep:needle:C:/src:20'` both parse correctly. If you hit edge cases, forward slashes (`C:/path`) work everywhere on Windows too.

**Temp/log location:** the call log uses `tempfile.gettempdir()` — macOS: `/var/folders/.../T/supertool-calls.log`, Linux: `/tmp/supertool-calls.log`, Windows: `%TEMP%\supertool-calls.log`.

---

## Design decisions

- **One file.** `supertool.py` is ~980 LoC (16 ops, 3 integration tiers). No package, no `setup.py`, no required deps. Drop in and use.
- **Python 3.9+.** macOS ships 3.9 via CommandLineTools; we don't force upgrades.
- **No MCP server.** MCP is server-process-and-JSON-RPC ceremony for what's literally "run a script, get output." A Bash-invoked binary is simpler, faster, and plugs into Claude Code's existing `--allowedTools`/`--disallowedTools` flow.
- **Enforcement via PreToolUse hook, not config mutation.** The plugin doesn't edit your `settings.json`. Toggling is a state file (`~/.claude/supertool-enforced`) read by the hook. Your config stays yours.
- **Trade Python work for LLM tokens.** LLM compute is expensive; local CPU is cheap. Any time the model would spend tokens computing, parsing, formatting, or finding — supertool should spend milliseconds instead. Richer op output (state hints, guards, semantic anchors, auto-formatting, syntax checks) is not feature creep — it's the whole thesis. Heavy Python is fine if it shaves tokens off the model side.

---

## License

[Community License](LICENSE) — free for personal, educational, and internal business use. © 2026 Digital Process Tools.
