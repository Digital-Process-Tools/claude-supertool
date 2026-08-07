# Reads

File reading and directory listing ops. Reach for these when you know the path and want content — a single file, a directory listing, or metadata without searching.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `read` | `read:PATH` or `read:PATH:OFFSET:LIMIT` | 300 lines / 20KB cap. With `read.abstract` on, a file over the threshold comes back as its symbol map instead — see [Abstract read](#abstract-read). **OFFSET is a skip count**, so `:19:1` renders line 20 — see [The window line](#the-window-line) |
| `read` (range) | `read:PATH:START-END` | Explicit inclusive line range. Prefer over `:OFFSET:LIMIT` when you know the lines — the offset form reads like a range but is not. Composes with `:full` and `:grep=PATTERN`. This is also where `between:PATH:START:END` is redirected: `between` is SYMBOL:PATH and does not take ranges. |
| `read` (filter) | `read:PATH:OFFSET:LIMIT:grep=PATTERN` | Only show lines matching PATTERN (original line numbers preserved). Use `read:PATH:::grep=PATTERN` for defaults. |
| `head` | `head:PATH:N` | First N lines (default 20). Minified single-line files return a char-window peek instead of the whole giant line; window size via `builtin-ops.head.char_window` (default 1000, or env `SUPERTOOL_HEAD_CHAR_WINDOW`). |
| `tail` | `tail:PATH:N` | Last N lines (default 20). Minified single-line files return a char-window peek; window size via `builtin-ops.tail.char_window` (default 1000, or env `SUPERTOOL_TAIL_CHAR_WINDOW`). |
| `wc` | `wc:PATH` | Line/word/char count (like unix `wc`). Output: `LINES WORDS CHARS PATH`. Flags single-line/minified files where the line count is degenerate. |
| `stat` | `stat:PATH` | File/directory metadata: size (bytes), last modified (ISO datetime), type (file/dir). |
| `ls` | `ls:PATH` | Directory listing. Trailing `/` on subdirs. |
| `glob` | `glob:PATTERN` | `**` supported. Patterns resolve from the repo root; an unanchored pattern that matches nothing is retried once as `**/PATTERN`, so `glob:SiBrief/**/*.php` also finds a `SiBrief/` nested deeper (the retry prints a `[mid-path retry: …]` line — a genuine zero stays zero). **Auto-reads** if PATTERN is a concrete file path (no wildcards). |
| `tree` | `tree:PATH` or `tree:PATH:DEPTH` | Directory structure with depth limit (default 3). Hides dotfiles. Files listed before subdirectories. |
| `diff` | `diff:PATH1:PATH2` | Unified diff between two files. |

The window line is not shown as a correction when the range form was used: a
`read:PATH:120-124` states the window it returned, and stops there. It used to
add "OFFSET is a skip count, not a start line — for lines 119-123 use
read:PATH:119-123", which corrects a form the caller did not type and names a
span one line off from the one they asked for ([#983](https://github.com/Digital-Process-Tools/claude-supertool/issues/983)).

## The window line

`read:PATH:OFFSET:LIMIT` takes OFFSET as a **skip count**, not a start line. So
`read:file.py:19:1` skips nineteen lines and renders line **20** — the line the
caller named is not in the output at all. That was silent: a shifted window, a
window clamped short by EOF, and a window past EOF that returned nothing all
rendered as well-formed output with nothing to distinguish them from a correct
one, and the last of the three signed off with `[complete file — no more lines]`.

Any read with a non-zero OFFSET now carries one line, immediately after the
count header and **before the content**, naming the window asked for and the
window returned:

```
(403 lines, 18211 bytes)
window: offset 19 + limit 1 = lines 20-20; returning lines 20-20 of 403, stopping at line 20: the limit was reached; OFFSET is a skip count, not a start line — for lines 19-19 use read:file.py:19-19
    20→...
```

Clamped short:

```
window: offset 8 + limit 5 = lines 9-13; returning lines 9-10 of 10, stopping at line 10: the end of the file; OFFSET is a skip count, not a start line — for lines 8-12 use read:ten.txt:8-12
```

Past EOF — no content, and the render says so instead of claiming the file was
shown in full:

```
window: offset 99 + limit 3 = lines 100-102; returning nothing — the file has 10 lines
```

A windowed read that reaches EOF closes with `[end of file — lines 1-OFFSET not
shown]`. `[complete file — no more lines]` is reserved for a read that started
at line 1, where it is true.

### Why the window ended — three reasons, never two

The clause after `stopping at line N` names which of three things ended the
window:

| Clause                              | What happened                                          |
| ----------------------------------- | ------------------------------------------------------ |
| `the limit was reached`             | LIMIT lines were read; the file continues              |
| `the end of the file`               | there was nothing after line N                         |
| `cut short by the 20000-byte cap`   | the render hit its byte budget; the file continues     |

The first version of this note knew only two of them and treated any shortfall
against the requested end as EOF, so a capped read opened with `stopping at line
54, the end of the file` above a body whose own footer, 20 KB further down, said
146 lines remained. The false claim came first, and it is the one a caller
quotes.

When two of them land on the same line — LIMIT reached exactly at EOF — the note
says so rather than choosing:

```
window: offset 40 + limit 60 = lines 41-100; returning lines 41-100 of 100, stopping at line 100: the end of the file and the limit was reached coincide here — which one ended the window cannot be told apart
```

### Compact mode

Compact mode drops blanks and comments. The lines it drops are read but not
printed, so the span the note names is wider than the count of lines in the
body, and the note says how many were suppressed:

```
window: offset 10 + limit 50 = lines 11-60; returning lines 11-60 of 200, 25 of those 50 lines emitted (compact mode skipped 25), stopping at line 60: the limit was reached
```

Every number in the render — the window line, the `... (N more lines)` footer and
the byte-cap footer — is taken from the last line the read **looked at**, not
from the count of lines it printed. Those two differ under compact mode and
under a `grep=` filter, and while they differed the same render carried two
counts that disagreed.

The semantics were not changed. `read:PATH:START-END` already spells 1-based
inclusive addressing and is the form to prefer; re-basing OFFSET would have
broken every caller who had it right, trading a visible wrong answer for an
invisible one.

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

**C# was in the language table and did not produce a map — fixed in [#790](https://github.com/Digital-Process-Tools/claude-supertool/issues/790).** `.cs` mapped to the grammar name `c_sharp`, the name the older `tree-sitter-languages` package used; the installed `tree-sitter-language-pack` calls it `csharp` and raised `LookupError`, which a bare `except` swallowed into an empty symbol list — indistinguishable from a file with genuinely no definitions. Every `.cs` file yielded zero symbols in `map` and `between:` as well as here, on 20 of 20 real files in the corpus below.

Grammar resolution now tries both spellings (`_ts_get_parser` in `_supertool.py`) before giving up, so C# produces real maps again. A grammar that still can't be loaded — for C# or any other language — no longer degrades silently: the failure is cached and surfaces as an explicit note (`grammar unavailable for .ext: <reason>`) in `map`, and as a distinct `ERROR: tree-sitter grammar for '.ext' failed to load` in `between:`, rather than either reading as "this file has no symbols".

**Two ways it declines.** A symbol map that is empty, or one that is no smaller than the bytes this read would otherwise emit, is a worse answer than the source. Either way the read returns the source and prints which happened — an absence produced by the tool must never read as an absence in the world (see [validators.md](../validators.md#declining-instead-of-guessing)):

```
[abstract read skipped — no symbols found in src/rows.ts (typescript); showing raw source]
[abstract read skipped — no symbols found in app.swift (swift) — tree-sitter is not installed, so only the regex tier ran; showing raw source]
[abstract read skipped — symbol map for gen.cpp (cpp) is 37412 bytes, not smaller than the 20000 bytes this read emits; showing raw source]
```

Across the corpus above, 29 of 263 files declined — 20 of them C#, before #790. With the grammar fix, those 20 now produce maps like every other language; the corpus has not been re-measured for C#'s row in the table above, so its median/worst columns are not yet in it.

A successful abstract read is labelled, and says how to get the source anyway:

```
[abstract read — typescript, 1204 lines, 43001 bytes raw — use read:src/Big.ts:full for content or read:src/Big.ts:::grep=PATTERN to filter]
```

### Markdown is exempt

`.md` and `.markdown` have a tree-sitter grammar (#887), so `map:` renders their
heading tree. Abstract read skips them anyway. The trade abstract read makes is
that a signature stands in for the body it heads; a heading does not stand in
for the prose it heads, and a reader who asked for a document and got its table
of contents has been given the shape of an answer rather than one.

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
