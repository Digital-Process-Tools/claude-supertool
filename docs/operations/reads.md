# Reads

File reading and directory listing ops. Reach for these when you know the path and want content — a single file, a directory listing, or metadata without searching.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `read` | `read:PATH` or `read:PATH:OFFSET:LIMIT` | 300 lines / 20KB cap. With `read.abstract` on, a file over the threshold comes back as its symbol map instead — see [Abstract read](#abstract-read) |
| `read` (range) | `read:PATH:START-END` | Explicit inclusive line range. Prefer over `:OFFSET:LIMIT` when you know the lines — the offset form reads like a range but is not. Composes with `:full` and `:grep=PATTERN`. |
| `read` (filter) | `read:PATH:OFFSET:LIMIT:grep=PATTERN` | Only show lines matching PATTERN (original line numbers preserved). Use `read:PATH:::grep=PATTERN` for defaults. |
| `head` | `head:PATH:N` | First N lines (default 20). Minified single-line files return a char-window peek instead of the whole giant line; window size via `builtin-ops.head.char_window` (default 1000, or env `SUPERTOOL_HEAD_CHAR_WINDOW`). |
| `tail` | `tail:PATH:N` | Last N lines (default 20). Minified single-line files return a char-window peek; window size via `builtin-ops.tail.char_window` (default 1000, or env `SUPERTOOL_TAIL_CHAR_WINDOW`). |
| `wc` | `wc:PATH` | Line/word/char count (like unix `wc`). Output: `LINES WORDS CHARS PATH`. Flags single-line/minified files where the line count is degenerate. |
| `stat` | `stat:PATH` | File/directory metadata: size (bytes), last modified (ISO datetime), type (file/dir). |
| `ls` | `ls:PATH` | Directory listing. Trailing `/` on subdirs. |
| `glob` | `glob:PATTERN` | `**` supported. Patterns resolve from the repo root; an unanchored pattern that matches nothing is retried once as `**/PATTERN`, so `glob:SiBrief/**/*.php` also finds a `SiBrief/` nested deeper (the retry prints a `[mid-path retry: …]` line — a genuine zero stays zero). **Auto-reads** if PATTERN is a concrete file path (no wildcards). |
| `tree` | `tree:PATH` or `tree:PATH:DEPTH` | Directory structure with depth limit (default 3). Hides dotfiles. Files listed before subdirectories. |
| `diff` | `diff:PATH1:PATH2` | Unified diff between two files. |

## Abstract read

`read` on a large file can return the file's **symbol map** instead of its source: every class, function and method with its line number, for the whole file, rather than the first 300 lines of text. Off by default; enable per project in `.supertool.json`:

```json
{ "builtin-ops": { "read": { "abstract": 1, "abstract_threshold_bytes": 20000 } } }
```

`abstract_threshold_bytes` defaults to `read.max_bytes` (20 KB) and can be overridden for one call with `SUPERTOOL_READ_ABSTRACT_THRESHOLD_BYTES=N`. `read.php_abstract` is the former name of the switch — from when it only applied to `.php` — and still enables it.

**What it applies to.** Any extension in supertool's language table: `.php .py .js .jsx .ts .tsx .go .rs .java .rb .c .h .cpp .hpp .swift .kt .scala .lua .sh .bash`. It never applies to `read:PATH:full`, to a read with an explicit offset/limit or `grep=` filter, or to a file at or below the threshold.

**What it costs.** Measured on 263 real files over the 20 KB threshold, sampled from Hugo, cobra, ripgrep, pdf.js, lodash, Vue core, React Router, gson, RuboCop, Sinatra, curl, nlohmann/json, Alamofire, OkHttp, os-lib, plenary.nvim, nvm and CPython's `site-packages`, plus a PHP codebase:

| language | files | median source | median map | median map/source | worst |
|----------|------:|--------------:|-----------:|------------------:|------:|
| typescript | 20 | 43.0 KB | 1.0 KB | 2.3% | 16% |
| ruby | 7 | 22.7 KB | 2.6 KB | 10.7% | 15% |
| tsx | 20 | 37.0 KB | 1.1 KB | 2.6% | 6% |
| lua | 3 | 23.6 KB | 1.0 KB | 2.6% | 10% |
| cpp | 16 | 41.8 KB | 1.6 KB | 2.5% | 7% |
| c | 20 | 36.3 KB | 1.0 KB | 2.7% | 8% |
| bash | 1 | 168.7 KB | 5.3 KB | 3.1% | 3% |
| python | 20 | 38.8 KB | 1.4 KB | 4.3% | 11% |
| java | 8 | 33.5 KB | 1.5 KB | 4.7% | 8% |
| go | 20 | 26.9 KB | 1.4 KB | 4.8% | 13% |
| php | 20 | 24.4 KB | 1.4 KB | 5.4% | 25% |
| swift | 20 | 37.6 KB | 2.4 KB | 5.4% | 19% |
| javascript | 20 | 40.2 KB | 2.8 KB | 5.7% | 38% |
| rust | 20 | 37.9 KB | 2.0 KB | 6.0% | 9% |
| kotlin | 20 | 32.9 KB | 2.8 KB | 6.8% | 13% |
| scala | 3 | 24.0 KB | 3.7 KB | 17.5% | 19% |

Scala, lua and bash are thin samples — the per-file guard below is what protects them, not the median.

**C# is in the language table and does not currently produce a map.** `.cs` maps to the grammar name `c_sharp`, which `tree-sitter-language-pack` does not answer to (it calls it `csharp`), so every `.cs` file yields zero symbols — in `map` and `between:` as well as here. The guard below catches it: a `.cs` read falls back to source with the reason stated, rather than returning an empty map.

**Two ways it declines.** A symbol map that is empty, or one that is no smaller than the bytes this read would otherwise emit, is a worse answer than the source. Either way the read returns the source and prints which happened — an absence produced by the tool must never read as an absence in the world (see [validators.md](../validators.md#declining-instead-of-guessing)):

```
[abstract read skipped — no symbols found in src/rows.ts (typescript); showing raw source]
[abstract read skipped — no symbols found in app.swift (swift) — tree-sitter is not installed, so only the regex tier ran; showing raw source]
[abstract read skipped — symbol map for gen.cpp (cpp) is 37412 bytes, not smaller than the 20000 bytes this read emits; showing raw source]
```

Across the corpus above, 29 of 263 files declined — 20 of them C#.

A successful abstract read is labelled, and says how to get the source anyway:

```
[abstract read — typescript, 1204 lines, 43001 bytes raw — use read:src/Big.ts:full for content or read:src/Big.ts:::grep=PATTERN to filter]
```

## Common patterns

Read three files in one round-trip — parallel where safe:

```bash
./supertool 'read:src/Module.py' 'read:src/Config.py' 'read:src/Auth.py'
```

Survey a directory then read the interesting file:

```bash
./supertool 'tree:src/app/' 'glob:src/app/**/*.py' 'read:src/app/Module.py'
```

Read only matching lines from a large file (line numbers preserved):

```bash
./supertool 'read:src/app/Config.py:::grep=DEBUG'
```

Check size before deciding to read:

```bash
./supertool 'stat:src/app/BigFile.py' 'head:src/app/BigFile.py:50'
```

## See also

- [search.md](search.md) — when you don't know the path yet (`grep`, `between`, `around`)
- [map.md](map.md) — when you want the symbol skeleton of a file/directory without reading every line
- [docs/validators.md](../validators.md) — validators run automatically on mutating ops
