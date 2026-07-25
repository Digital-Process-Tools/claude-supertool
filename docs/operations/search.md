# Search

Pattern-based ops for finding content across files or zooming into a known location. Reach for these when you don't know the exact file yet, or when you want context around a match.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `grep` | `grep:PATTERN:PATH` or `grep:PATTERN:PATH:LIMIT` | 10 results default, code + doc extensions only. Each output line is capped at 500 chars (`grep.max_line_chars`) with a `… (+N chars)` marker — see [per-line cap](#per-line-cap). **Auto-reads** full file if PATH is a concrete file under **both** the byte cap (< 20KB) **and** the line cap (≤ 60 lines, `MAX_AUTOREAD_LINES`) with a match; over the line cap it prints a `[auto-read skipped: > N lines — read:PATH:full to see it]` note instead of dumping the file. Append `:no-auto-read` to suppress entirely (parity with `glob`). |
| `grep` (context) | `grep:PATTERN:PATH:LIMIT:CONTEXT` | Show CONTEXT lines before/after each match (like `grep -C`). Match lines: `path:lineno:content`. Context lines: `path-lineno-content`. Non-adjacent groups separated by `--`. |
| `grep` (count) | `grep:PATTERN:PATH:LIMIT:CONTEXT:count` | Return match counts per file instead of content. Output: `filepath:COUNT` per line. |
| `grep_around` | `grep_around:PATTERN:PATH` or `grep_around:PATTERN:PATH:N:LIMIT` | Every match across files with N lines context (default N=3, LIMIT=10). Alias for `grep:PATTERN:PATH:LIMIT:CONTEXT` with sane defaults — useful for "show me how everyone uses this". Output capped at ~16KB (see below). |
| `around` | `around:PATTERN:PATH` or `around:PATTERN:PATH:N` | Show N lines (default 10) before and after the **first** match of PATTERN in a single file. Uses line-numbered output like `read`. Output capped at ~16KB (see below). |
| `around_line` | `around_line:PATH:LINE` or `around_line:PATH:LINE:N` | Show N lines (default 10) of context around a specific line number. Target line marked with `→`. |
| `between` | `between:SYMBOL:PATH` or `between:re:START:END:PATH` | Return a chunk of a file. **Symbol mode (default):** full body of a named function/method/class via tree-sitter (PHP, Python, JS, TS, Go, Rust, Java, Ruby — symbols with `::` like PHP `Foo::bar` work). SYMBOL may be written the way it reads in source — leading modifiers (`async`, `function`, `def`, `class`, `public static function`, …) and a trailing `(params)` are stripped and retried after the exact match fails, so `between:async function fillAndSubmit:helpers.js` resolves. **Pattern mode (`re:` prefix):** inclusive line slice from first line matching START regex to first line after matching END regex (language-agnostic). |

## Output cap

A large `:N` context on a file of long (e.g. minified) lines can over-fetch — one op can dump hundreds of KB and blow your context budget. `around:` and `grep_around:` cap their output at **~16KB**, truncating at a line boundary with a footer that points at the narrower tools (smaller `:N`, or `between:` for a whole symbol). Tune via `builtin-ops.around.max_bytes` / `builtin-ops.grep_around.max_bytes` in `.supertool.json` (or `SUPERTOOL_AROUND_MAX_BYTES` / `SUPERTOOL_GREP_AROUND_MAX_BYTES`). See [configuration.md](../configuration.md#builtin-ops).

## Per-line cap

A single pathological line — a minified bundle, a 7KB one-line `@extends Foo<array{…}>` PHPDoc — used to turn one match into a screenful. Every `grep` output line (match **and** context) is truncated at `grep.max_line_chars` (default 500, or `SUPERTOOL_GREP_MAX_LINE_CHARS`) with a `… (+N chars)` marker naming what was dropped, so widening is a deliberate choice. Normal-width lines are untouched. This is orthogonal to the byte cap below: the byte cap bounds the whole window, the per-line cap bounds one line.

`grep:` with an explicit `CONTEXT` argument (`grep:PATTERN:PATH:LIMIT:CONTEXT`) shares the `grep_around:` code path, so it is capped under the same `grep_around.max_bytes` budget. Plain `grep:` (no context) is unaffected — it has its own `LIMIT`/`max_results` bound.

## Common patterns

Find all usages of a function across a codebase, with 2 lines of context:

```bash
./supertool 'grep:handle_request:src/:20:2'
```

Count which files reference a symbol most:

```bash
./supertool 'grep:MyClass:src/:50:0:count'
```

Show how a pattern is used everywhere ("show me how everyone uses this"):

```bash
./supertool 'grep_around:def handle:src/:5:15'
```

Jump to a known line and see surrounding context:

```bash
./supertool 'around_line:src/app/Module.py:142:8'
```

Extract a full function body by name:

```bash
./supertool 'between:handle_request:src/app/Module.py'
./supertool 'between:MyClass::handle:src/app/Module.php'
```

Extract a block by regex anchors (language-agnostic):

```bash
./supertool 'between:re:^def handle_request:^def :src/app/Module.py'
```

## See also

- [reads.md](reads.md) — when you know the path and want raw content
- [map.md](map.md) — when you want all symbols in a file/directory at once (faster than grep for orientation)
- [docs/validators.md](../validators.md) — validators fire on edits triggered after search-guided changes
