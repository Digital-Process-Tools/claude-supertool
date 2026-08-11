# Operations Reference

~40 ops across five categories. Use this page for a quick "all ops at a glance" lookup, then follow the per-category links for patterns and recipes.

## Path arguments

A path argument may be relative to the cwd, absolute, or start with `~` / `~user`. All three resolve the same way and are checked against the same [cwd containment boundary](../../README.md#security--cwd-containment) — `~` is expanded *before* the check, so it grants nothing an absolute path would not (#1300).

`~` that names no such user is left exactly as typed, and the refusal says so instead of blaming the working directory. Every `not found` receipt prints the string the op actually stat-ed under `tried:`, so an absolute path in that line is one the tool really opened.

## Categories

| Category | Ops | Page |
|----------|-----|------|
| **Reads** | `read`, `read-grep`, `head`, `tail`, `wc`, `stat`, `ls`, `glob`, `tree`, `diff` | [reads.md](reads.md) |
| **Search** | `grep`, `grep-count`, `grep_around`, `around`, `around_line`, `between` | [search.md](search.md) |
| **Symbol map** | `map` | [map.md](map.md) |
| **Edits** | `edit`, `replace`, `replace_dry`, `replace_lines`, `paste`, `append`, `vim`, `batch` | [edits.md](edits.md) |
| **Validate / Format** | `validate`, `format`, `validate_staged`, `format_staged` | — |
| **Meta** | `cwd`, `repo`, `introduction`, `output-format`, `ops`, `ops:roster`, `help`, `version`, `gc` | [meta.md](meta.md) |

## Full op table

| Op | Syntax | What it does |
|----|--------|--------------|
| `read` | `read:PATH` or `read:PATH:OFFSET:LIMIT` | 300 lines / 20KB cap. OFFSET is a skip count, not a start line — `:19:1` renders line 20, and the header states the window actually returned |
| `read` (range) | `read:PATH:START-END` | Explicit inclusive line range. Prefer over `:OFFSET:LIMIT` when you know the lines — the offset form reads like a range but is not. Composes with `:full` and `:grep=PATTERN`. A line range handed to `between` is redirected here; `around:PATH:LINE` is answered by `around_line` instead. |
| `read` (filter) | `read:PATH:OFFSET:LIMIT:grep=PATTERN` | Only show lines matching PATTERN (original line numbers preserved). Use `read:PATH:::grep=PATTERN` for defaults. |
| `grep` | `grep:PATTERN:PATH` or `grep:PATTERN:PATH:LIMIT` | 10 results default, code + doc extensions only. **Auto-reads** full file if PATH is a concrete file < 20KB with a match. Append `:no-auto-read` to suppress (parity with `glob`). |
| `grep` (context) | `grep:PATTERN:PATH:LIMIT:CONTEXT` | Show CONTEXT lines before/after each match (like `grep -C`). Match lines: `path:lineno:content`. Context lines: `path-lineno-content`. Non-adjacent groups separated by `--`. |
| `grep` (count) | `grep:PATTERN:PATH:LIMIT:CONTEXT:count` | Return match counts per file instead of content. Output: `filepath: N matches` per line. |
| `glob` | `glob:PATTERN` | `**` supported. Unanchored pattern matching nothing is retried once as `**/PATTERN` (mid-path segments resolve, announced in the output). **Auto-reads** if PATTERN is a concrete file path (no wildcards). |
| `ls` | `ls:PATH` | Trailing `/` on subdirs |
| `tail` | `tail:PATH:N` | Last N lines (default 20). Minified single-line files return a char-window peek; size via `builtin-ops.tail.char_window` (default 1000). |
| `head` | `head:PATH:N` | First N lines (default 20). Minified single-line files return a char-window peek; size via `builtin-ops.head.char_window` (default 1000). |
| `wc` | `wc:PATH` | Line/word/char count (like unix `wc`). Output: `LINES WORDS CHARS PATH`. Flags single-line/minified files. |
| `around` | `around:PATTERN:PATH` or `around:PATTERN:PATH:N` | Show N lines (default 10) before and after the **first** match of PATTERN in a single file. Uses line-numbered output like `read`. |
| `grep_around` | `grep_around:PATTERN:PATH` or `grep_around:PATTERN:PATH:N:LIMIT` | Every match across files with N lines context (default N=3, LIMIT=10). Alias for `grep:PATTERN:PATH:LIMIT:CONTEXT` with sane defaults — useful for "show me how everyone uses this". |
| `map` | `map:PATH` | Symbol map of a file or directory. Shows classes, functions, methods, constants as an indented tree with line numbers. Three-tier: tree-sitter → ctags → regex. Supports PHP, Python, JS, TS, Go, Rust, Java, Ruby. |
| `introduction` | `introduction` | Output the project introduction text from `.supertool.json`. No `---` dispatch header — clean markdown. |
| `output-format` | `output-format` | Output format examples from `.supertool.json`. Shows what responses look like. |
| `ops` | `ops` | Full operations reference from `.supertool.json` — built-in ops, custom ops, and aliases with descriptions and examples. An unrecognised argument is refused, not dropped. |
| `ops:roster` | `ops:roster` | Every op name plus a safety class, nothing else (~1.4KB) — the only listing form that fits the ~7KB SessionStart cap, so it is what the hook prints. Unmarked = read-only, `*` = writes in this tree, `!` = acts outside it or outlives the call. |
| `diff` | `diff:PATH1:PATH2` | Unified diff between two files. |
| `stat` | `stat:PATH` | File/directory metadata: size (bytes), last modified (ISO datetime), type (file/dir). |
| `around_line` | `around_line:PATH:LINE` or `around_line:PATH:LINE:N` | Show N lines (default 10) of context around a specific line number. Target line marked with `→`. |
| `between` | `between:SYMBOL:PATH` or `between:re:START:END:PATH` | Return a chunk of a file. **Symbol mode (default):** full body of a named function/method/class via tree-sitter (PHP, Python, JS, TS, Go, Rust, Java, Ruby — a `::`-qualified query stays one symbol rather than re-reading the call as `re:` mode, but it is matched literally against the definition's own name node — PHP `Foo::bar` does not resolve, pass the bare `bar`). SYMBOL tolerates source-shaped input (`async function foo`, `public static function bar`, `foo(...)`). **Pattern mode (`re:` prefix):** inclusive line slice from first line matching START regex to first line after matching END regex (language-agnostic). |
| `tree` | `tree:PATH` or `tree:PATH:DEPTH` | Directory structure with depth limit (default 3). Hides dotfiles. Files listed before subdirectories. |
| `blame` | `blame:PATH:LINE` or `blame:PATH:LINE:N` | Git blame for N lines (default 5) around a specific line number. Requires git repo. |
| `help` | `help:OP` | Full reference for one op — the same `.supertool.json` metadata `ops` lists, scoped and never compacted. An op the dispatcher accepts but no config describes says so, rather than reporting itself unknown. |
| `version` | `version` | Show supertool version. |
| `gc` | `gc`, `gc:dry`, `gc:run`, `gc:run:KIND` | Prune supertool's own caches under `~/.cache/supertool`. Bare `gc` and `gc:dry` **preview** — per-kind counts and bytes, nothing deleted. `gc:run` deletes. Optional KIND scopes to `vim-cursor`, `vim-undo`, `vi-cursor` or `validators`. See [meta.md](meta.md#gc--cache-retention). |
| `cwd` | `cwd:PATH` | Set the working dir for the whole call. **Must be the first op** — chdir's once before any dispatch (so every following op resolves against `PATH`), then is stripped. Mirrors `cd PATH && …` without the `cd` (which trips the use-supertool hook and risks stale-cwd path poisoning). `~`/`$VAR` expanded; non-directory or non-first → error before any op runs. |
| `repo` | `repo:OWNER/NAME` | Name the repo a call is *about*, when it is not the one the cwd stands *in*. First op, or immediately after `cwd:`, once per call; resolved before the op loop and exported as `SUPERTOOL_REPO`. Honoured by the `gh-*` family only — an op in the same call that cannot honour it refuses the whole call rather than half-applying the target. Not a `cwd:` substitute: presets still resolve from the cwd's project root. |
| `edit` | `edit:::OLD:::NEW:::PATH` | Single-file, single-occurrence edit (mirrors native Edit). Errors if 0 or >1 matches. **Bypasses native Edit must-Read state** — saves a round-trip when you already know the unique snippet. Use `:::` separator so content with `:` works. |
| `replace_lines` | `replace_lines:::PATH:::START:::END:::CONTENT` | Swap lines `[START, END]` (1-indexed, inclusive) with CONTENT. `END < START` = pure insert before line START. Empty CONTENT = delete. Receipt shows new line numbers + ±2 context. |
| `paste` | `paste:::PATH:::CONTENT` | **NARROW USE:** replace ENTIRE file. Only for creating a new file or fully rewriting one. NOT for partial edits — `vim` is the default for those. Atomic, creates file + parent dirs if missing. CONTENT via triple-colon → holds any chars (`:`, quotes, braces, newlines). |
| `append` | `append:::PATH:::CONTENT` | Append CONTENT to the end of a file, creating it if missing. No `wc` round-trip, no inverted-range `replace_lines` trick. Adds a missing trailing newline first so the block starts on its own line. |
| `vim` | `vim:::PATH:::SCRIPT` | vim-flavored cursor-based multi-action edit. SCRIPT is parsed like a real vim macro. **DEFAULT EDIT OP** for any pattern-based edit. See [edits.md](edits.md) for full syntax reference. |
| `replace` / `replace_dry` | `replace:::OLD:::NEW:::PATH` | Recursive find/replace across PATH (`replace_dry` = preview). Use `:::` separator when content has `:`. |
| `batch` | `batch:@FILE` or `batch:@-` | Run N ops from one payload — a TOML `[[ops]]` array or a JSON array of `{"op": …}` objects, each entry taking that op's own `@payload` fields. The only way to put several mutations in one call, since a call carries just one `@-` (#341). Reads and greps mix in freely. Per-op validators, per-op rollback; **not** atomic by default — see [edits.md](edits.md#batchfile--mixed-ops-in-one-round-trip). |
| `validate` | `validate:PATH[:tool1,tool2][:verbose]` | Run registered validators matching PATH (by file extension). Optional `tool_filter` limits to named validators. Append `verbose` for uncapped errors + source context + raw stdout/stderr. Same validators that fire after every mutating op. |
| `format` | `format:PATH[:tool1,tool2][:verbose]` | Run registered formatters matching PATH (writes file in place). Optional `tool_filter`. Append `verbose` for full per-file details. |
| `validate_staged` | `validate_staged[::tool1,tool2][:verbose]` | Run validators on all files in `git diff --cached --name-only`. Optional `tool_filter`. Append `verbose` for full per-file details. Useful as a pre-commit check. |
| `format_staged` | `format_staged[::tool1,tool2][:verbose]` | Run formatters on all staged files. Optional `tool_filter`. Append `verbose` for full per-file details. Pair with `validate_staged` for a full normalize-then-check pass. |
| `workspace` | `workspace:PATH` | One-shot IDE-style view: file + symbols + validators + siblings + git + references + tests. Opt-in (heavy). Use for first-touch on unfamiliar files. |
| `resolve` | `resolve:SYMBOL` | Smart-glob resolver: PHP FQN (`\`-separated), Python dotted import, JS/TS relative path (`./`) → file on disk. Returns `external` for npm/pip packages, `not found` if no match. Used internally by workspace's Imports section. |

**LLM onboarding in one call:** `./supertool 'introduction' 'output-format' 'ops'`
