<p align="center">
  <img src="supertool-banner.jpg" alt="SuperTool — batch file operations" width="600">
</p>

# supertool

**Cut your Claude Code file operation costs in half.**

Every time Claude reads a file, it re-sends the entire conversation — system prompt, CLAUDE.md, rules, every prior message. Read 7 files? Pay that prefix 7 times. SuperTool batches all 7 into **one call**. Same files, same output, half the tokens.

One Python file. Zero deps. Python 3.9+.

```bash
# 7 operations, 1 round-trip
supertool 'read:src/Module.py' 'read:src/Auth.py' 'grep:TODO:src/:20' 'map:src/'
```

---

## The problem

Claude Code's tool system charges you per round-trip, not per file. Each tool call re-transmits the cached conversation prefix. Anthropic caches it at 10% of input price — cheaper than full re-read, but not free. And it adds up fast.

An autonomous agent documenting a component needs ~5 files and 2 greps. Without batching, that's 10 round-trips. With SuperTool, it's 2-3.

| Mode                       | Cache reads | Output | Turns | Cost savings |
| -------------------------- | ----------: | -----: | ----: | -----------: |
| No batching                |        436K |  1,400 |    10 |            — |
| SuperTool                  |        133K |    750 |     3 |     **50%** |
| Pre-computed + SuperTool   |       85.5K |    600 |     2 |     **56%** |

**50% fewer tokens on read operations.** At scale (200 tasks/run), that's real money — and **3-4x faster** wall time.

The savings come from fewer prefix re-reads, not from reading files faster. The bytes still land in context either way. **Fewer turns = fewer re-reads = the only lever that works.**

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

**LLM onboarding in one call:** `./supertool 'introduction' 'output-format' 'ops'` — outputs everything an LLM needs to use supertool.

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

**`gitlab`** — GitLab ops via [glab CLI](https://gitlab.com/gitlab-org/cli). Requires `glab` installed and authenticated.

| Op | Syntax | What it does |
|----|--------|-------------|
| `gl-issue` | `gl-issue:NUMBER` | Issue metadata, description, human comments, related MRs, image download |
| `gl-mr` | `gl-mr:NUMBER_OR_BRANCH` | MR dashboard: branch, pipeline, reviewer/approval, linked issue, diff stat, comments |
| `gl-pipeline` | `gl-pipeline:NUMBER` | Pipeline job list grouped by stage with pass/fail |
| `gl-job` | `gl-job:NUMBER` | Job log with MR context, error pattern search + configurable tail |

All ops are namespaced with `gl-` to avoid collisions with other presets.

`gl-mr` accepts either an MR number (`gl-mr:42`) or a branch name (`gl-mr:feature/my-branch`) — it resolves branches to MRs automatically.

`gl-job` searches logs for error patterns before falling back to tail. Configure via JSON:

```json
{
  "presets": ["gitlab"],
  "ops": {
    "gl-job": {
      "cmd": "python3 {path}gitlab/job.py {arg}",
      "lines": 120,
      "error_patterns": "ERROR,FAILURES!,Fatal,Failed asserting",
      "error_context": 10
    }
  }
}
```

**`github`** — GitHub ops via [gh CLI](https://cli.github.com). Requires `gh` installed and authenticated (`gh auth login`).

| Op | Syntax | What it does |
|----|--------|-------------|
| `gh-issue` | `gh-issue:NUMBER` | Issue metadata, description, comments, linked PRs, image download |
| `gh-pr` | `gh-pr:NUMBER_OR_BRANCH` | PR dashboard: branch, checks, reviews/approval, linked issue, diff stat, comments |
| `gh-run` | `gh-run:NUMBER` | GitHub Actions workflow run: job list with statuses and failed step names |
| `gh-job` | `gh-job:NUMBER` | Job log with PR context, error pattern search (`##[error]`) + configurable tail |

All ops are namespaced with `gh-` to avoid collisions with other presets.

`gh-pr` accepts either a PR number (`gh-pr:42`) or a branch name (`gh-pr:feature/my-branch`) — it resolves branches to PRs automatically.

Both forge presets include **actionable error messages** — when something fails (404, auth, permissions, rate limit), the error tells the LLM exactly what went wrong and what command to run to fix it.

**`git`** — Git investigation ops. No auth needed — works on any git repo.

| Op | Syntax | What it does |
|----|--------|-------------|
| `git-status` | `git-status` | Dashboard: branch, ahead/behind, last 5 commits, staged/unstaged/untracked, stashes, open MR/PR |
| `git-investigate` | `git-investigate:PATH` | File investigation: recent commits, uncommitted changes, blame hotspots |
| `git-trail` | `git-trail:PATTERN:PATH` | Trace a symbol through history via pickaxe search — when added, modified, removed |
| `git-blame` | `git-blame:PATH:LINE[:N]` | Blame N lines around a line number (moved from builtin) |

`git-status` tries `glab` then `gh` to show the open MR/PR for the current branch — skips gracefully if neither is installed. All other ops are pure git.

`git-investigate` combines 3-5 git commands into one report: log, diff, and blame hotspots (most recently changed lines). Configurable via `SUPERTOOL_COMMITS` and `SUPERTOOL_BLAME_RECENT`.

`git-trail` answers "when was this added/changed/removed?" using `git log -S` (pickaxe), with regex fallback. Shows timeline + contextual diffs filtered to relevant hunks.

**`claude-log`** — Inspect Claude Code session logs (`~/.claude/projects/<encoded-cwd>/*.jsonl`). No auth, no deps — pure stdlib Python.

| Op | Syntax | What it does |
|----|--------|-------------|
| `claude-log-list` | `claude-log-list[:N]` | N most recent sessions for the current project: UUID, mtime, turn count, line count, first user-message excerpt |
| `claude-log-tail` | `claude-log-tail:UUID[:N]` | Last N events in compact form: `[role] TOOL name(input)` / `[result] output` / `[result/ERR] msg` / `[bootstrap] preview` |
| `claude-log-summary` | `claude-log-summary:UUID` | Full digest: model, duration, turn counts, tool calls + errors-by-tool, tokens (input/output/cache read/cache create) + cache hit %, final assistant text |

Useful for measuring autonomous-run efficiency — spotting wasted round-trips, validating that a skill change reduced tool calls, comparing model performance across runs. Windows-friendly cwd encoding (handles `\` and drive colons), with closest-prefix sibling fallback when the encoded directory doesn't exist.

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

---

## License

[Community License](LICENSE) — free for personal, educational, and internal business use. © 2026 Digital Process Tools.
