# Reads

File reading and directory listing ops. Reach for these when you know the path and want content — a single file, a directory listing, or metadata without searching.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `read` | `read:PATH` or `read:PATH:OFFSET:LIMIT` | 300 lines / 20KB cap. With `read.abstract` on, a file over the threshold comes back as its symbol map instead — see [Abstract read](#abstract-read). **OFFSET is a skip count**, so `:19:1` renders line 20 — see [The window line](#the-window-line) |
| `read` (range) | `read:PATH:START-END` | Explicit inclusive line range. Prefer over `:OFFSET:LIMIT` when you know the lines — the offset form reads like a range but is not. Composes with `:full` and `:grep=PATTERN`. This is also where `between:PATH:START:END` **and** `between:PATH:START-END` are redirected: `between` is SYMBOL:PATH and does not take ranges, in either spelling. |
| `read` (filter) | `read:PATH:OFFSET:LIMIT:grep=PATTERN` | Only show lines matching PATTERN (original line numbers preserved). Use `read:PATH:::grep=PATTERN` to search the **whole file** — the 300-line default bounds what a read emits, not what a filter searches. Give an OFFSET/LIMIT and the filter is bounded by it, and its zero says which lines it did not look at — see [When the filter finds nothing](#when-the-filter-finds-nothing). |
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

## Eliding a repeat read

A second `read:PATH` of a **byte-identical** file inside 15 minutes returns one line instead of the content — **provided both reads share the same parent process.** Under Claude Code that means the same Bash tool call, and only that; see "Where it actually fires" below before you count on it.

```
[read elided — presets/gh/job.py is byte-identical to your read at 07:14:22 (sha256 3f9a1c2d8b04, 4,812 bytes on disk), so this would return what you already have. If you no longer have it: read:presets/gh/job.py:full]
```

**The op cannot know whether you still hold the first copy, and that is the whole design problem.** A re-read after a context compaction is the normal case, not the edge case: the earlier result was evicted, the model is asking again precisely *because* it no longer has it, and a line saying "unchanged since 07:14" hands back nothing. Nothing observable inside the process distinguishes that from a redundant second ask. So the feature does not try to guess. Every rule below is a bound on the damage when the guess would have been wrong:

- **The elision is always one round-trip from the bytes**, and the command that returns them is in the line itself, not in this document. That is the real bound: the worst case is a wasted call, never lost information.
- **A recency window, measured from the last read that actually returned content** — never bumped by an elision. A file polled every minute would otherwise be elided forever. Default 900s (`read.elide_window_seconds`).
- **A file whose bytes changed is never elided, at any age.** They are the repeat reads that carry information, and they are most of them: `claude-log:cost` (#1252) measured repeat reads at **37.8% of result bytes** on the dvsi corpus, of which only 8.7 points were byte-identical — so roughly three quarters of that traffic is a file that moved.
- **A cache that cannot answer returns the content.** An unreadable or unwritable state file is `skipped`, not silence — the three-state rule in [validators.md](../validators.md), applied to the op's own bookkeeping.
- **`read:PATH:full` never elides** and always re-arms the window, because after a forced read you demonstrably hold the bytes again.
- **The byte count is the file's size on disk, and says so.** A file over `read.max_bytes` is capped on the way out even under `:full`, so the size of the file and the bytes the first read handed over are different numbers; naming the former as "withheld" would overstate it on exactly the files where the cap bites.
- **Only a bare `read:PATH` participates.** An explicit offset, limit, `START-END` range or `grep=` filter is never elided, and never arms an elision of the whole file — a recorded whole-file read says nothing about a slice request.

**Keyed on `(session, realpath, sha256)`** — not mtime, which changes on a no-op rewrite and misses a same-second edit. The session component is `USER | PPID | realpath(cwd)`: PPID is the session proxy supertool already uses for its call log, and the resolved cwd is mixed in because nine worktrees were live on one machine on 2026-08-11 and one agent's read must never suppress another's. The two failure directions are not symmetric — over-keying costs one file returned again, under-keying withholds content the caller never saw — so the key is deliberately the narrower one. State lives in one small sidecar per `(session, file)` under `~/.cache/supertool/read-elide/`, so concurrent supertool processes never read-modify-write a shared index; `gc` reaps the kind after a day.

**Where it actually fires — measured 2026-08-11, and it is narrower than the paragraph above sounds.** `os.getppid()` for `supertool` is the process that *ran* it, which is the shell, and Claude Code starts a new shell per Bash tool call. So the key changes on every turn:

```
# two bare reads of one unchanged file, separate Bash calls, ~60s apart
call 1 -> 3,840 bytes of content
call 2 -> 3,840 bytes of content
~/.cache/supertool/read-elide: two keys, one file

# the same two reads on ONE command line
supertool 'read:PATH' >/dev/null; supertool 'read:PATH' >second.txt
wc -c second.txt -> 276          # the elision line
```

**It therefore fires for two invocations inside one shell — the case batching already covers — and never for the repeat read after a compaction it was built for.** A human at an interactive prompt keeps one shell and does get it; an agent driving `supertool` through per-call shells does not. Filed as [#1352](https://github.com/Digital-Process-Tools/claude-supertool/issues/1352), open at the time of writing: the fix is not obvious, because Claude Code exposes `session_id` only to hook stdin, and the two failure directions above still bind. `tests/test_read_elide_session_boundary_1352.py` pins both arms, so whoever changes the key finds out from a red test rather than from this paragraph.

**What it is worth here: nothing, and that was true before the key was.** Measured with `claude-log:cost` over real transcripts, unchanged re-reads are **0.0% of result bytes on the supertool corpus** — batching already prevents the pattern. That is a property of the corpus, not of the key, so it survives #1352 either way. The 8.7% figure people quote is the `Read`-tool-driven corpus (252,851 B, one `portfolio.json` read sixteen times) and is not ours. This exists for what it prevents.

Turn it off with `read.elide: 0` in `.supertool.json`, or `SUPERTOOL_READ_NO_ELIDE=1` for one call.

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

## When the filter finds nothing

`grep=` with no OFFSET and no LIMIT searches the whole file. Until #1052 it did
not: `op_read` applied the 300-line default before the filter ran, so on a
351-line file the filter looked at lines 1-300, missed a match at line 328, and
answered `(no lines matching 'X')` — a confident negative about the file,
produced by a default the caller never typed.

A zero from this filter now always says what it searched:

```
(no lines matching 'thing' in any of 120 lines)
(no lines matching 'thing' in lines 1-10 of 340 — the other 330 lines were NOT searched, so this is not an answer about the whole file)
```

The second form appears whenever the scan was bounded — by an explicit LIMIT or
by an OFFSET. (A zero is never byte-capped: the cap only trips after a line has
been emitted, so a scan that found nothing had nothing to truncate.)

A filter that *did* match but stopped short says so on its own line, including
when what stopped it was the byte cap — the cap breaks the scan, not just the
output, so the lines past it were never searched rather than searched and
rejected:

```
(the grep= filter searched lines 1-92 of 399 and stopped there — the output reached the 20000-byte cap, so the other 307 lines were NOT searched and this is not an answer about the whole file — continue with read:PATH:92:LIMIT:grep=PATTERN)
```

Three states, not two: found, not found, did not look.

All of these notes render directly under the count header, above the first
matched line — the same position the windowed-read disclosure has occupied
since #955. A note that arrives after you have already read the wrong answer
is barely a note.

## When the filter matches everything

`grep=` goes through the same pattern gate as `grep`, `grep_around` and
`around` (#1344). Two things follow, and until #1344 neither did — the gate was
wired route by route and `read`'s filter, reached through a different parser
branch, had been wired to neither:

- **bash-grep BRE alternation is rewritten.** `read:PATH:::grep=alpha\|gamma`
  filters on either branch, as it does everywhere else, instead of searching
  for the literal string `alpha|gamma`. The rewrite is never silent: the
  receipt names the pattern that ran.
- **A saturating pattern is refused, not filtered.** A top-level alternation
  branch matching the empty string matches every line, so the filter returns
  the file — indistinguishable from `read:PATH`, except that the caller now
  believes every line matched what they typed. That is the same false belief
  the `grep` refusal exists to prevent, arrived at more quietly.

```
./supertool 'read:probe.txt:::grep=^|x'
ERROR: pattern `^|x` has an alternation branch `^` that matches the empty string, so the whole pattern matches every line of every file scanned. …
```

Refusing rather than disclosing is the decision #1344 asked for. The whole file
already has a spelling — `read:PATH` — so the refusal removes no call anyone
meant, and the predicate is narrow: it needs a top-level alternation with a
branch that matches every probe, so `^$|alpha` (blank lines or alpha) and
`colo(u|)r` still filter. Both the rewrite and the refusal live in one function,
so a fifth route cannot arrive with a third behaviour.

A `grep=` value that is not a usable regex is still searched for as a literal
string — an unusable pattern should not fail a read — and the receipt names the
`re.error` and says the search was literal, so a rejected pattern's zero is not
spelled like a real absence.

Check size before deciding to read:

```bash
./supertool 'stat:src/app/BigFile.py' 'head:src/app/BigFile.py:50'
```

## Line numbering

`read` numbers lines by LF, CR and CRLF, and so does every op that edits *by*
line number. Before #1060 they did not agree: `replace_lines` split the decoded
string, and `str.splitlines()` also breaks on U+000B, U+000C, U+001C, U+001D,
U+001E, U+0085, U+2028 and U+2029 — so on a file holding one of those, the line
you read at N was not the line `replace_lines:PATH:N:N:...` wrote to, and
nothing said so.

One function owns that definition now. Where it differs from the one another
tool would use, the read says so under its header:

```
(412 lines, 18022 bytes)
note: contains U+2028 — supertool numbers lines by LF / CRLF / CR only, so a tool that also breaks on these (Python's str.splitlines, some editors) numbers this file differently. supertool's reads and its line-addressed edits agree with each other.
```

An ordinary file says nothing — the note fires only when the disagreement is
real. See [edits.md](edits.md#what-counts-as-a-line).

## See also

- [search.md](search.md) — when you don't know the path yet (`grep`, `between`, `around`)
- [map.md](map.md) — when you want the symbol skeleton of a file/directory without reading every line
- [docs/validators.md](../validators.md) — validators run automatically on mutating ops
