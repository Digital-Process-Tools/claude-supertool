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
| `glob` | `glob:PATTERN` | `**` supported. Patterns resolve from the **cwd**, not the repo root — from a subdirectory `glob:*.txt` sees that subdirectory (this row said "repo root" until #1366, and the containment gate added there is a cwd boundary for the same reason). An unanchored pattern that matches nothing is retried once as `**/PATTERN`, so `glob:SiBrief/**/*.php` also finds a `SiBrief/` nested deeper (the retry prints a `[mid-path retry: …]` line — a genuine zero stays zero). **Auto-reads** if PATTERN is a concrete file path (no wildcards). Contained: the pattern's reach is checked before the walk and the matches after it, and either refuses with `path escapes cwd` rather than an empty list (#1366) — see [Path arguments](index.md#path-arguments). |
| `tree` | `tree:PATH` or `tree:PATH:DEPTH` | Directory structure with depth limit (default 3). Hides dotfiles. Files listed before subdirectories. |
| `diff` | `diff:PATH1:PATH2` | Unified diff between two files. |

### A token these ops do not read is refused, not dropped

Every op in the table above takes a fixed number of colon slots. A **non-empty**
token past the last one it reads is refused, naming the token and quoting the
op's own syntax line:

```
$ supertool 'read:CLAUDE.md:::lines=66-76'
ERROR: read: 1 argument past the last slot read reads — 'lines=66-76'.
  Dropped rather than refused before #1582, so the call ran without it: a narrowing that was ignored returns MORE than was asked for, and nothing in the result says so.
  Syntax: read:PATH[:OFFSET:LIMIT|:START-END|:full]
  For a line range use the syntax form: read:CLAUDE.md:66-76
  A value that contains ':' cannot be spelled on the colon CLI — use read:@- (fields: path, offset, limit, grep, full).
```

That call used to return all 102 lines of the file. This is the worst shape the
drop has, and the reason
[#1582](https://github.com/Digital-Process-Tools/claude-supertool/issues/1582)
was filed rather than shrugged at: **an ignored narrowing returns MORE than was
asked for, and more always reads as a superset of correct.** The lines you wanted
are present, in order, under a plausible header, alongside the 91 you did not.
Measured before the fix, twelve read ops were probed with one junk token appended
and twelve answered and dropped it.

`read`'s tail is the one exception to a plain slot count, because the filter can
land in any trailing slot: empty tokens (the `:::` in `read:PATH:::grep=P` yields
two) and **one** `grep=` are accepted, and nothing else. A second `grep=` is an
extra too — the scan takes the first, so the second was silently the one that did
not apply.

`grep` has the same rule expressed on its own peel: nothing follows CONTEXT, so
`grep:PAT:PATH:5:3:2` is refused rather than run as `5:3`
([#1345](https://github.com/Digital-Process-Tools/claude-supertool/issues/1345)).
`glob`'s flag slot is refused the same way when it holds anything but
`no-auto-read`. `around` and `between` are unaffected: neither keeps its
arguments in fixed slots, and both already fail loudly by absorbing the extra
token into the PATH slot.

The window line names which of the two grammars ran — `(OFFSET:LIMIT form)` or
`(START-END form)` — because they are one character apart and return different
windows, and until
[#1417](https://github.com/Digital-Process-Tools/claude-supertool/issues/1417)
the line described both of them in OFFSET/LIMIT terms, reporting a range call in
the vocabulary of the form it did not use.

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
count header and **before the content**, naming the grammar it took, the window
asked for and the window returned:

```
(403 lines, 18211 bytes)
window: offset 19 + limit 1 (OFFSET:LIMIT form) = lines 20-20; returning lines 20-20 of 403, stopping at line 20: the limit was reached — the window ends here because it was asked to, nothing was cut; OFFSET is a skip count, so 19 lines were skipped: this window is read:file.py:20-20 — for lines 19-19 use read:file.py:19-19
    20→...
```

Clamped short:

```
window: offset 8 + limit 5 (OFFSET:LIMIT form) = lines 9-13; returning lines 9-10 of 10, stopping at line 10: the end of the file; OFFSET is a skip count, so 8 lines were skipped: this window is read:ten.txt:9-10 — for lines 8-12 use read:ten.txt:8-12
```

**Two ranges are named, and they mean different things.** `this window is
read:PATH:A-B` is the fact: the span that came back, spelled in the unambiguous
form — note the clamped example, where it ends at 10 and not at the 13 that was
asked for. `for lines C-D use read:PATH:C-D` is the other reading, what a caller
who read `:A:B` as `START:END` was after. Seeing the first number one higher
than the OFFSET typed is what makes the off-by-one visible.

The fact is stated on every non-range read with a non-zero OFFSET. **The guess
is withheld when the misread-range note below is speaking**, because that note
answers the same question from the same numbers and lands somewhere else — on
`read:PATH:20:25` the two of them proposed `20-44` and `20-25`, one call and two
answers to one question.

Until [#1417](https://github.com/Digital-Process-Tools/claude-supertool/issues/1417)
this fired only when `LIMIT <= OFFSET`, deferring the rest to the misread-range
note below — which is itself gated on overshoot-or-`LIMIT < 2*OFFSET`. So
`read:PATH:1:40` on a long file tripped neither gate and got no hint at all, and
that is the commonest shape of the mistake
([#1138](https://github.com/Digital-Process-Tools/claude-supertool/issues/1138)):
measured over 5,598 real `read:PATH:N:M` calls, 1,877 of them (34%) sat in that
hole. A deferral to a speaker who is silent is the house defect one level up.

The parse is deliberately unchanged. #1138 proposed refusing `:N:M`, or reading
it as `START-END` when `N < M` — measured against the same 5,598 calls those
would refuse or silently re-aim 5,549 and 3,208 of them respectively, and a
refusal that breaks a form callers legitimately use is a regression dressed as a
fix.

Past EOF — no content, and the render says so instead of claiming the file was
shown in full:

```
window: offset 99 + limit 3 (OFFSET:LIMIT form) = lines 100-102; returning nothing — the file has 10 lines
```

A windowed read that reaches EOF closes with `[end of file — lines 1-OFFSET not
shown]`. `[complete file — no more lines]` is reserved for a read that started
at line 1, where it is true.

### Why the window ended — four bounds, never two

The clause after `stopping at line N` names which bound ended the window. It was
two ([#945](https://github.com/Digital-Process-Tools/claude-supertool/issues/945)),
then three, and is four since
[#1820](https://github.com/Digital-Process-Tools/claude-supertool/issues/1820)
split the one word that was covering two of them:

| Clause                                        | What happened                                                    |
| --------------------------------------------- | ---------------------------------------------------------------- |
| `the limit was reached`                       | the LIMIT **you typed** was read; the file continues             |
| `the read.max_lines default of N lines was reached` | you named no LIMIT — the bound is the op's, not yours      |
| `the end of the file`                         | there was nothing after line N                                   |
| `cut short by the 20000-byte cap`             | the render hit its byte budget; the file continues               |

**"Limit" was one word for two bounds until
[#1820](https://github.com/Digital-Process-Tools/claude-supertool/issues/1820).**
`read:PATH:10:20` ends at a window the caller closed themselves; `read:PATH:10`
ends at `read.max_lines`, the op's own cap on output standing in for a bound
nobody named. The two want opposite responses — nothing, and a wider read — and
both said `the limit was reached`. The second now names the setting and counts
the lines below it.

### Did the window come back whole?

Naming the bound is not the same as saying whether it cost anything, and the
note only did the first. A window returned **whole** and a window the byte cap
**cut short** both opened with `stopping at line N: ...`, so the only way to tell
them apart was to read again, wider, and compare — which is what
[#1820](https://github.com/Digital-Process-Tools/claude-supertool/issues/1820)
was filed for, one extra read spent to learn the first had already answered.

A window that ended at the bound **the caller set**, with nothing dropped, now
says so:

```
window: range 40-72 (START-END form); returning lines 40-72 of 1974, stopping at line 72: the limit was reached — the window ends here because it was asked to, nothing was cut
```

The clause is deliberately absent from the other three states, and each absence
is a different fact rather than an oversight:

- **EOF** already settles it in its own words (`nothing follows line N`,
  [#1342](https://github.com/Digital-Process-Tools/claude-supertool/issues/1342)).
  A second verdict beside it is two speakers on one question.
- **Cut short by the cap** is the state this distinguishes from, and saying
  `nothing was cut` there would be false.
- **The `read.max_lines` default** was not asked for, so nothing about it "ends
  where it was asked to". Its own clause counts what is below it instead.

The claim is about the **window**, not the lines: under `grep=` the emitted count
is smaller than the scanned count, and the `N of those M lines emitted` clause is
what speaks to that. Under **compact mode** the footer form below drops the
`nothing was cut` half outright — blanks and comments really were dropped, and
unlike the window note that footer has no `held` clause beside it to say so.

#### At offset 0 the same distinction lives in the footer

The window note is emitted only for a read with a **non-zero OFFSET**, and a
range starting at line 1 has an offset of zero. So `read:PATH:1-50` never
reached any of the above, and closed with a bare `... (150 more lines)` — byte
for byte what a plain `read:PATH` cut short by `read.max_lines` printed. One
caller had everything they asked for; the other was missing 150 lines to a bound
they never set. The footer now carries the same two verdicts:

```
... (150 more lines — lines 1-50 are the whole window asked for, nothing was cut; those 150 are simply below it)
... (150 more lines — the read.max_lines default of 50 stopped the read here, not the file)
```

The first version of this note knew only two of them and treated any shortfall
against the requested end as EOF, so a capped read opened with `stopping at line
54, the end of the file` above a body whose own footer, 20 KB further down, said
146 lines remained. The false claim came first, and it is the one a caller
quotes.

When two of them land on the same line, the note names both — and says which one
decides, whenever it can. **EOF decides.** `last_scanned >= line_count` is a fact
the note already prints in its own text (`of 100`), so a window ending on the
last line is not a tie: the caller has everything from the start of the window,
and nothing follows.

```
window: offset 40 + limit 60 (OFFSET:LIMIT form) = lines 41-100; returning lines 41-100 of 100, stopping at line 100: the end of the file, and the limit was reached at the same line — the file ending settles it, nothing follows line 100; OFFSET is a skip count, so 40 lines were skipped: this window is read:hundred.txt:41-100 — this asked for 60 lines from offset 40, which is OFFSET:LIMIT, not START:END; for lines 40-60 (21 lines) use read:hundred.txt:40-60
```

That last clause used to be a second line, printed as `note:` **below** the body
([#1489](https://github.com/Digital-Process-Tools/claude-supertool/issues/1489)).
It said what the line above it had already said — OFFSET:LIMIT, not START:END —
and it said it after the reader had paid for the 60-line window it was warning
them about. [#1432](https://github.com/Digital-Process-Tools/claude-supertool/issues/1432)
moved this disclosure above the body; the trailing copy stayed behind. One
question, one answer, above the cost.

Until [#1342](https://github.com/Digital-Process-Tools/claude-supertool/issues/1342)
this read `coincide here — which one ended the window cannot be told apart`. The
three-state contract exists so a checker that *cannot* answer says so; a decline
emitted where the answer is on hand is that contract used as a shrug, and it
costs a round-trip to establish something already on screen. Left alone it also
erodes the real declines.

**The byte cap and the limit cannot coincide at all**
([#1616](https://github.com/Digital-Process-Tools/claude-supertool/issues/1616)).
The cap is tested *after* a whole line has been emitted, so it never truncates a
line — it drops the ones that would have followed. A window that reached its own
limit, or the end of the file, had none to drop, and the cap cost it nothing.
`read:mid.txt:2-2` on a 25 KB line used to open with `cut short by the
20000-byte cap and the limit was reached coincide here`, having returned every
byte — while `grep`'s own `… (+N chars)` note points callers at that exact read
*promising* byte-exactness.

Reaching the cap and being cut by it are separate facts and the note says which:

```
window: range 2-2 (START-END form); returning lines 2-2 of 4, stopping at line 2: the limit was reached — the window ends here because it was asked to, nothing was cut — the 20000-byte cap was reached on that line and dropped nothing; it stops whole lines and never truncates one, so these bytes are complete
```

Silence would be the other half of the same defect — the output *is* at the cap,
so a window one line wider will lose lines, and a caller planning the next read
needs to know. The `... (truncated at 20000 bytes …)` footer is gated on the same
fact: it counted lines the caller had not asked for as ones it had withheld.

### Compact mode

Compact mode drops blanks and comments. The lines it drops are read but not
printed, so the span the note names is wider than the count of lines in the
body, and the note says how many were suppressed:

```
window: offset 10 + limit 50 (OFFSET:LIMIT form) = lines 11-60; returning lines 11-60 of 200, 25 of those 50 lines emitted (compact mode skipped 25), stopping at line 60: the limit was reached — the window ends here because it was asked to, nothing was cut; OFFSET is a skip count, so 10 lines were skipped: this window is read:file.py:11-60 — for lines 10-59 use read:file.py:10-59
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

### A whole-file read that did not come back whole says how to narrow it

`read:PATH` on a large file renders the file's **head** and stops. The head is
the region least likely to be why the file was read — a whole-file read of a
long file is rarely about its first few hundred lines — so the preview is often
paid for and answers nothing, and the caller issues a second, narrower call
anyway
([#1811](https://github.com/Digital-Process-Tools/claude-supertool/issues/1811)).

The stop was already disclosed; the way out was not. `... (N more lines)` named
no remedy at all, and the byte-cap footer named only `read:PATH:OFFSET:LIMIT` —
the one spelling this repo has three issues of evidence that callers read as
START:END ([#382](https://github.com/Digital-Process-Tools/claude-supertool/issues/382),
[#1417](https://github.com/Digital-Process-Tools/claude-supertool/issues/1417),
[#1489](https://github.com/Digital-Process-Tools/claude-supertool/issues/1489)).
Both footers now close with the two forms that narrow:

```
... (900 more lines)
    ↳ that is the head of the file — for a region, read:big.py:START-END; to find one, read:big.py:::grep=PATTERN
```

This is what `between:`'s refusal already does — suggest its own narrowing form
at the moment it declines, rather than after the reader has paid.

**The preview is kept.** Dropping it for a structure block was the other
candidate and loses more than it saves: the head is what tells the caller what
kind of file this is, which is the input to writing the narrowing call. Guessing
a region to preview instead was the third, and `read:PATH` implies none — that
is the shape the issue is about.

**It fires only where it is actionable**: a read that returned the file has
nothing to narrow, a caller who already typed a window has demonstrated they
know the forms, and `:full` asked for the whole thing. Advice printed on every
read is advice nobody reads.

Where `rtk` renders the read instead, the line is appended to rtk's own output —
rtk owns the preview and its footer and knows nothing about supertool's call
forms, the same argument the line-numbering disclosure above is here for. The
gate there is supertool's own caps rather than a parse of rtk's text, which
under-fires where rtk compresses a file supertool would have returned intact.
That is the safe direction.

**A malformed rtk render is discarded, not trusted** (#1786). `rtk read` can
answer a full-file `read:PATH` with a real content line sandwiched between two
contradicting elision-style footers — reproduced directly against `rtk`
0.35.0, a bug in its own compression, not in anything this repo computes.
`_rtk_output_looks_malformed` treats two elision markers in one render as that
signature — a genuine truncation cuts once — and falls back to the native
renderer below, disclosing the fallback (`rtk's compressed read looked
malformed for this file -- falling back to the built-in renderer; #1786`)
rather than silently swapping it in. `read:PATH:START-END` never goes through
RTK delegation at all, which is why a ranged read of the same file was
already clean.

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

## Anti-patterns the tool catches

Moved from `README.md` by [#2142](https://github.com/Digital-Process-Tools/claude-supertool/issues/2142). The tool **auto-promotes** these wasted patterns silently, but recognise them and batch up front rather than relying on the rescue:

- `glob:concrete/path.xml` followed by `read:concrete/path.xml` — glob on a path with no wildcards is useless; just `read:`. Auto-read handles it.
- `grep:FOO:single_file.py` followed by `read:single_file.py` — same file, two turns. Auto-read fires if the file is < 20KB with a match.
- A second call whose ops could have fit in the first.

**Self-check:** if the output contains `[auto-read: ...]`, the tool just salvaged a wasted turn you asked for. Tighten your next prompt to batch up front.

## See also

- [search.md](search.md) — when you don't know the path yet (`grep`, `between`, `around`)
- [map.md](map.md) — when you want the symbol skeleton of a file/directory without reading every line
- [docs/validators.md](../validators.md) — validators run automatically on mutating ops
