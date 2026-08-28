# Operations Reference

45 ops across seven categories. Use this page for a quick "all ops at a glance" lookup, then follow the per-category links for patterns and recipes.

Both listings below are hand-written prose over a machine-readable fact, so `tests/test_ops_index_complete_1371.py` holds them to it: every name the dispatcher accepts must appear in the Categories table *and* in the full op table, and the count above must be the real one. `registry` shipped in #1363 and reached neither list; enumerating from the product rather than from the bug report found six more in the same state.

## Path arguments

A path argument may be relative to the cwd, absolute, or start with `~` / `~user`. All three resolve the same way and are checked against the same [cwd containment boundary](../../README.md#security--cwd-containment) — `~` is expanded *before* the check, so it grants nothing an absolute path would not (#1300).

`~` that names no such user is left exactly as typed, and the refusal says so instead of blaming the working directory. Every `not found` receipt prints the string the op actually stat-ed under `tried:`, so an absolute path in that line is one the tool really opened.

**A glob pattern is gated too, and by its own rule (#1366).** Until v0.34.0 `glob` was outside the boundary entirely: `glob:/tmp/x/*.txt` listed what `read:/tmp/x/f.txt` refused. Filenames rather than bytes, so what crossed was an existence oracle — layout, naming, whether a path is there at all. A pattern is not a path, so the check is in two halves and neither of them narrows a result set:

- **Before disk is touched**, on the pattern's *reach* — every magic component read as one ordinary name. No glob metacharacter can invent a `/`, so only the separators and `..` you wrote move the cursor, which is why `*/../../etc/*` is refused although the literal text before its first `*` is empty. Brace groups are expanded first and each branch is checked on its own.
- **After expansion**, on the matches, for the one case a pattern cannot predict: a wildcard landing on a symlink that points out of the tree. That refuses the **whole call** and names no file — dropping the entries would hand back a shorter list under an honest-looking `(N files)` header, and printing them would disclose the paths the refusal is about.

Both say `path escapes cwd` and carry the same opt-out as every other refusal. **Neither ever renders as `(0 files)`** — a manufactured zero reads exactly like an empty directory, which is what `glob` used to say about `~`.

## Categories

| Category | Ops | Page |
|----------|-----|------|
| **Reads** | `read`, `head`, `tail`, `wc`, `stat`, `ls`, `glob`, `tree`, `diff` | [reads.md](reads.md) |
| **Search** | `grep`, `grep_around`, `around`, `around_line`, `between` | [search.md](search.md) |
| **Symbol map** | `map`, `workspace`, `resolve` | [map.md](map.md) |
| **Edits** | `edit`, `replace`, `replace_dry`, `replace_lines`, `paste`, `append`, `vim`, `batch` | [edits.md](edits.md) |
| **Validate / Format** | `validate`, `format`, `validate_staged`, `format_staged`, `check` | — |
| **LSP-backed** | `diag`, `hover`, `rename` | [mcp-integration.md](../mcp-integration.md) |
| **Meta** | `cwd`, `repo`, `introduction`, `output-format`, `ops`, `ops:roster`, `ops-compact`, `registry`, `guard`, `help`, `version`, `doctor`, `gc` | [meta.md](meta.md) |

## Full op table

| Op | Syntax | What it does |
|----|--------|--------------|
| `read` | `read:PATH` or `read:PATH:OFFSET:LIMIT` | 300 lines / 20KB cap. OFFSET is a skip count, not a start line — `:19:1` renders line 20. The window line names which grammar ran (`OFFSET:LIMIT form` / `START-END form`), the window actually returned, and the `START-END` spelling of it |
| `read` (range) | `read:PATH:START-END` | Explicit inclusive line range. Prefer over `:OFFSET:LIMIT` when you know the lines — the offset form reads like a range but is not. Composes with `:full` and `:grep=PATTERN`. A line range handed to `between` is redirected here; `around:PATH:LINE` is answered by `around_line` instead. |
| `read` (filter) | `read:PATH:OFFSET:LIMIT:grep=PATTERN` | Only show lines matching PATTERN (original line numbers preserved). `read:PATH:::grep=PATTERN` searches the whole file, not the first 300 lines. Same pattern gate as `grep`: BRE alternation is rewritten and disclosed, and a pattern that matches every line is refused (#1344). |
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
| `ops` | `ops` | Every op's **signature** — built-in, custom and aliases — and nothing else, at ~4.1KB. Descriptions and examples moved to `ops:full` in #1774; `help:OP` carries one op's in full. An unrecognised argument is refused, not dropped. |
| `ops:roster` | `ops:roster` | Every op name plus a safety class, nothing else (~2.0KB) — the only listing form that fits the 10,000-byte SessionStart cap, so it is what the hook prints. Unmarked = read-only, `*` = writes in this tree, `!` = acts outside it or outlives the call. |
| `diff` | `diff:PATH1:PATH2` | Unified diff between two files. |
| `stat` | `stat:PATH` | File/directory metadata: size (bytes), last modified (ISO datetime), type (file/dir). |
| `around_line` | `around_line:PATH:LINE` or `around_line:PATH:LINE:N` | Show N lines (default 10) of context around a specific line number. Target line marked with `→`. |
| `between` | `between:SYMBOL:PATH` or `between:re:START:END:PATH` | Return a chunk of a file. **Symbol mode (default):** full body of a named function/method/class via tree-sitter (PHP, Python, JS, TS, Go, Rust, Java, Ruby — a `::`-qualified query stays one symbol rather than re-reading the call as `re:` mode, but it is matched literally against the definition's own name node — PHP `Foo::bar` does not resolve, pass the bare `bar`). SYMBOL tolerates source-shaped input (`async function foo`, `public static function bar`, `foo(...)`). **Pattern mode (`re:` prefix):** inclusive line slice from first line matching START regex to first line after matching END regex (language-agnostic). |
| `tree` | `tree:PATH` or `tree:PATH:DEPTH` | Directory structure with depth limit (default 3). Hides dotfiles. Files listed before subdirectories. |
| `git-blame` | `git-blame:PATH:LINE[:N]` | Blame for N lines (default 5) around a line. **A preset op, not a builtin** — it ships with the `git` preset and is absent where that preset is not loaded. This row said `blame` until #1371; there is no such op, and `blame:PATH:LINE` answers `unknown operation`. Full reference: [presets/git.md](../presets/git.md). |
| `help` | `help:OP` | Full reference for one op — the same `.supertool.json` metadata `ops` lists, scoped and never compacted, **plus the `@-` payload route and the field names derived for it**. An op the dispatcher accepts but no config describes says so, rather than reporting itself unknown. |
| `version` | `version` | Show supertool version. |
| `doctor` | `doctor` or `doctor:probe` | Environment supertool runs in (interpreter, architecture/Rosetta, CPU topology, symlink health) plus, per configured validator, whether the toolchain it dispatches to resolves here. Bare `doctor` reports scope only; `doctor:probe` invokes each in-scope validator to sort it into resolves/absent/could-not-tell. See [meta.md](meta.md#doctor--the-environment-and-toolchain-supertool-runs-in). |
| `gc` | `gc`, `gc:dry`, `gc:run`, `gc:run:KIND` | Prune supertool's own caches under `~/.cache/supertool`. Bare `gc` and `gc:dry` **preview** — per-kind counts and bytes, nothing deleted. `gc:run` deletes. Optional KIND scopes to `vim-cursor`, `vim-undo`, `vi-cursor` or `validators`. See [meta.md](meta.md#gc--cache-retention). |
| `cwd` | `cwd:PATH` | Set the working dir for the whole call. **Must be the first op** — chdir's once before any dispatch (so every following op resolves against `PATH`), then is stripped. Mirrors `cd PATH && …` without the `cd` (which trips the use-supertool hook and risks stale-cwd path poisoning). `~`/`$VAR` expanded; non-directory or non-first → error before any op runs. |
| `repo` | `repo:OWNER/NAME` | Name the repo a call is *about*, when it is not the one the cwd stands *in*. First op, or immediately after `cwd:`, once per call; resolved before the op loop and exported as `SUPERTOOL_REPO`. Honoured by the `gh-*` family and by `gl-issue`/`gl-mr`/`gl-pipeline`/`gl-job` — an op in the same call that cannot honour it refuses the whole call rather than half-applying the target. Not a `cwd:` substitute: presets still resolve from the cwd's project root. |
| `edit` | `edit:::OLD:::NEW:::PATH` | Single-file, single-occurrence edit (mirrors native Edit). Errors if 0 or >1 matches. **Bypasses native Edit must-Read state** — saves a round-trip when you already know the unique snippet. Use `:::` separator so content with `:` works. |
| `replace_lines` | `replace_lines:::PATH:::START:::END:::CONTENT` | Swap lines `[START, END]` (1-indexed, inclusive) with CONTENT. `END < START` = pure insert before line START. Empty CONTENT = delete. Receipt shows new line numbers + ±2 context. |
| `paste` | `paste:::PATH:::CONTENT` | **NARROW USE:** replace ENTIRE file. Only for creating a new file or fully rewriting one. NOT for partial edits — `vim` is the default for those. Atomic, creates file + parent dirs if missing. CONTENT via triple-colon → holds any chars (`:`, quotes, braces, newlines). |
| `append` | `append:::PATH:::CONTENT` | Append CONTENT to the end of a file, creating it if missing. No `wc` round-trip, no inverted-range `replace_lines` trick. Adds a missing trailing newline first so the block starts on its own line. |
| `vim` | `vim:::PATH:::SCRIPT` | vim-flavored cursor-based multi-action edit. SCRIPT is parsed like a real vim macro. **DEFAULT EDIT OP** for any pattern-based edit. See [edits.md](edits.md) for full syntax reference. |
| `replace` / `replace_dry` | `replace:::OLD:::NEW:::PATH` | Recursive find/replace across PATH (`replace_dry` = preview). Use `:::` separator when content has `:`. |
| `batch` | `batch:@FILE` or `batch:@-` | Run N ops from one payload — a TOML `[[ops]]` array or a JSON array of `{"op": …}` objects, each entry taking that op's own `@payload` fields. The only way to put several mutations in one call, since a call carries just one `@-` (#341). Reads and greps mix in freely. A nested `batch` entry takes its inner payload as `path` = `"@inner.toml"` — the `@` is required there too, and any other key is refused by name. Per-op validators, per-op rollback; **not** atomic by default — see [edits.md](edits.md#batchfile--mixed-ops-in-one-round-trip). |
| `validate` | `validate:PATH[:tool1,tool2][:verbose]` | Run registered validators matching PATH (by file extension). Optional `tool_filter` limits to named validators. Append `verbose` for uncapped errors + source context + raw stdout/stderr. Same validators that fire after every mutating op. |
| `format` | `format:PATH[:tool1,tool2][:verbose]` | Run registered formatters matching PATH (writes file in place). Optional `tool_filter`. Append `verbose` for full per-file details. |
| `validate_staged` | `validate_staged[::tool1,tool2][:verbose]` | Run validators on all files in `git diff --cached --name-only`. Optional `tool_filter`. Append `verbose` for full per-file details. Useful as a pre-commit check. |
| `format_staged` | `format_staged[::tool1,tool2][:verbose]` | Run formatters on all staged files. Optional `tool_filter`. Append `verbose` for full per-file details. Pair with `validate_staged` for a full normalize-then-check pass. |
| `workspace` | `workspace:PATH` | One-shot IDE-style view: file + symbols + validators + siblings + git + references + tests. Opt-in (heavy). Use for first-touch on unfamiliar files. |
| `resolve` | `resolve:SYMBOL` or `resolve:SYMBOL:FILE` | FILE is the file the import was written in, and only a Python relative import (`.`, `.utils`) needs it — resolved against that file's directory, and reported `external` without it. Smart-glob resolver: PHP FQN (`\`-separated), Python dotted import, JS/TS relative path (`./`) → file on disk. Returns `external` for npm/pip packages, `not found` if no match. Used internally by workspace's Imports section. |
| `ops-compact` | `ops-compact` | The descriptive listing with the per-op detail trimmed: ~16.5KB, against `ops:full`'s ~76.0KB. Still over the 10,000-byte SessionStart hook cap and says so in its first line rather than letting the tail be cut — `ops:roster` is the form that fits. (Bare `ops` has been signatures-only since #1774 and fits the cap on its own; the trimming is of the descriptions, which is why this is larger than `ops` rather than smaller.) || `registry` | `registry` or `registry:OP` | Which op definitions are loaded and **where each came from** — a shipped preset, this project's config, or a project entry merged over a preset. `ops` answers *what can I do*; this answers *whose definition is in effect*. `registry:OP` attributes every key of one entry. A registry that could not be fully enumerated says so instead of returning a short list. See [meta.md](meta.md#registry--which-ops-are-loaded-and-where-each-came-from). |
| `guard` | `guard:COMMAND` | What the op registry says replaces a raw shell command — asked directly, without running anything. The command is tokenised into argv rather than pattern-matched. Three verdicts: `BLOCKED`, `OK`, and `UNDECIDED` for a command the guard could not read, which is never rendered as `OK`. See [meta.md](meta.md#guard--what-the-registry-says-replaces-a-raw-command). |
| `check` | `check:PRESET:PATH` | Run one named op from `.supertool.json`'s `ops` section against PATH, as a standalone check. An unknown name is refused with the list of names that do exist — never answered as a check that found nothing. |
| `diag` | `diag:PATH` | LSP diagnostics (errors, warnings, hints) for a file. Needs an `mcp.<server>.tools.diag` mapping; without one it returns an error rather than an empty diagnostic list, which would read as a clean file. See [mcp-integration.md](../mcp-integration.md). |
| `hover` | `hover:SYMBOL:PATH` | Type signature and docs for SYMBOL. Two MCP calls per invocation — `resolve` to find the identifier position, then `hover` there — so both mappings are required. |
| `rename` | `rename:OLD:NEW:PATH` | Workspace-atomic symbol rename across every file that references it, via the LSP's own rename. cclsp writes `.bak` backups. Requires the `rename` tool mapping. |

**LLM onboarding in one call:** `./supertool 'introduction' 'output-format' 'ops'`
