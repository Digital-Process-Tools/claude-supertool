# Search

Pattern-based ops for finding content across files or zooming into a known location. Reach for these when you don't know the exact file yet, or when you want context around a match.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `grep` | `grep:PATTERN:PATH` or `grep:PATTERN:PATH:LIMIT` | 10 results default, code + doc extensions only. **Auto-reads** full file if PATH is a concrete file < 20KB with a match. |
| `grep` (context) | `grep:PATTERN:PATH:LIMIT:CONTEXT` | Show CONTEXT lines before/after each match (like `grep -C`). Match lines: `path:lineno:content`. Context lines: `path-lineno-content`. Non-adjacent groups separated by `--`. |
| `grep` (count) | `grep:PATTERN:PATH:LIMIT:CONTEXT:count` | Return match counts per file instead of content. Output: `filepath:COUNT` per line. |
| `grep_around` | `grep_around:PATTERN:PATH` or `grep_around:PATTERN:PATH:N:LIMIT` | Every match across files with N lines context (default N=3, LIMIT=10). Alias for `grep:PATTERN:PATH:LIMIT:CONTEXT` with sane defaults — useful for "show me how everyone uses this". |
| `around` | `around:PATTERN:PATH` or `around:PATTERN:PATH:N` | Show N lines (default 10) before and after the **first** match of PATTERN in a single file. Uses line-numbered output like `read`. |
| `around_line` | `around_line:PATH:LINE` or `around_line:PATH:LINE:N` | Show N lines (default 10) of context around a specific line number. Target line marked with `→`. |
| `between` | `between:SYMBOL:PATH` or `between:re:START:END:PATH` | Return a chunk of a file. **Symbol mode (default):** full body of a named function/method/class via tree-sitter (PHP, Python, JS, TS, Go, Rust, Java, Ruby — symbols with `::` like PHP `Foo::bar` work). **Pattern mode (`re:` prefix):** inclusive line slice from first line matching START regex to first line after matching END regex (language-agnostic). |

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
