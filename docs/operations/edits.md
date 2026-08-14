# Edits

Mutating ops for modifying files. Every edit op runs matching validators on the result — syntax failure triggers atomic rollback before the receipt reaches the model.

Rollback is unconditional for `.py` (built-in parse check, no config needed) and configuration-dependent for everything else — see [validators.md](../validators.md#the-built-in-syntax-backstop).

**Default edit op:** `vim`. Use `edit` for single known snippets, `replace` for cross-file renames, `paste` only for full rewrites or new files.

A `vim` receipt also carries a post-edit syntax lint (`php -l`, `xmllint`, `py_compile`, JSON parse). An empty lint section means the file is clean, and only that: a lint that times out says `POST-EDIT LINT TIMED OUT` instead of falling silent, since silence would read as a pass. The 5s budget is raised with `SUPERTOOL_LINT_TIMEOUT=<seconds>` when a slow runner needs room.

Every mutating call closes with two footer lines, in this order:

```
[result] 3 ops run, 2 writes, 1 skipped
[branch: my-feature]
```

`[result]` is the authoritative outcome and is safe to read with `| tail -2`. It exists because the per-op receipt sits **above** the `[validators]` block, and a long validators block is exactly when you reach for `tail` — so the last line on screen used to be `git-status : ok`, which describes the validators and reads as though it described the edit (#621). `N ops run` counts mutating ops attempted; `M writes` counts writes that landed *and stuck*, so a validator rollback reports `0`. When `M` is `0` the line ends `— nothing changed on disk`. Exactly one footer per call, never one per sub-op.

`K skipped` is the third state (#680), and it is the field to read in a batch. `N` vs `M` always carried the same information, but only as a subtraction you had to perform while already suspicious — a batch reporting `6 ops run, 4 writes` had silently dropped two edits, and the branch reached CI with `use` imports removed and their users left behind. A skip is an op that **ran and deliberately left the disk alone**: `edit` whose `old` did not match (or matched more than once, which is refused as ambiguous), `replace` that found zero occurrences, `vim` whose pattern missed — vim ops are atomic, so none of its actions applied. The failing entry still prints its own `ERROR:` receipt with the nearest-match hint; `K` is what survives a `tail`.

**A skip makes the call exit non-zero**, so `&&` chains stop rather than committing a half-applied set. This is the one behaviour change: `replace` finding nothing used to exit `0`, because its receipt says `(0 occurrences of 'x' found)` rather than `ERROR`, and the exit code is derived from the op's own return value (since [#1291](https://github.com/Digital-Process-Tools/claude-supertool/issues/1291); from the first line of the rendered receipt before it). A skip is now counted where the decline is *decided*, never inferred from `N - M` — that subtraction is wrong for a multi-file `replace` (more writes than attempts), for `replace_dry` (a preview writes nothing by design), and for a validator rollback (a write genuinely made, then retracted).

The field is **omitted** when `K` is `0`. `#680` asked for `0 skipped` on every line; a zero on the green path is the kind of number a reader learns to stop seeing, which is exactly how `4 writes` failed. A word that appears only when it means something is the stronger signal.

`K re-applied` is the fourth state (#701), and it is the one to read after **re-running a payload**. Some edits keep their own anchor — `old = "def f():"`, `new = "@decorated<newline>def f():"` — so `old` is still in the file after the edit and matches again on a second run. That is correct find-and-replace behaviour, and it applies the edit twice. What was wrong is that the two runs printed the same thing: `edited a.py (line 1-2)` then `edited a.py (line 2-3)`, both `[result] 1 op run, 1 write`, both exit 0. An identical receipt reads as "the same thing happened" when what happened is a second mutation — and the caller who re-runs a payload is by definition the caller who was already unsure whether it landed, which is how this composes with #680: the first defect makes you doubt, the second punishes checking.

A re-apply is **not** a decline and **not** a failure. It wrote, the write stuck, and the call still exits `0` — an edit that legitimately applies twice exists (appending a second repeated element has exactly this shape) and refusing it would be guessing at intent. `skipped` and `re-applied` are separate words for that reason: a skipped op left the disk alone, a re-applied one did not. When `K` is non-zero and something was written, the line ends `— an edit already present in the file was applied again`.

`K rolled back` is the fifth state (#952): the op matched, wrote, failed a validator with `rollback_on_fail`, and the previous content was restored. `M writes` already excluded it, and in the single-op case that rendered as a sentence — `0 writes — nothing changed on disk`. In a **batch where other ops did write** it rendered as `3 ops run, 2 writes`, which is an arithmetic mismatch you have to notice before you can explain it, and the reverted edit had no word anywhere below the `[validators]` block.

```
[result] 3 ops run, 2 writes, 1 rolled back — 1 edit was reverted after validation and did NOT land
```

It is **not** `skipped`: a skipped op declined and left the disk alone, a rolled-back one wrote and had the write undone, and the remedies differ — a new anchor versus a fix to the code. It is not folded into `— nothing changed on disk` either, because a plain no-match prints exactly that too, and those are the two most confusable outcomes on this path. **A rollback makes the call exit non-zero**, on the same grounds as a skip: `batch:@ops && git commit` used to commit the set without that edit and exit `0`.

The claim above it is retracted rather than deleted, because deleting it would make "written, then reverted" indistinguishable from "never ran":

```
edited src/Foo.php (line 40-44)
...
[rolled back] phplint regressed; src/Foo.php restored — retracts "edited src/Foo.php (line 40-44)"; the file was NOT edited
```

The retraction quotes the retracted line back and names the file, so `grep -E 'edited|ERROR'` — the filtered read that reported a rolled-back batch as landed — returns the undo next to the claim instead of the claim alone.

The op receipt carries the same signal next to the claim it qualifies, because `edited a.py (line 2-3)` on its own is a true sentence that reads as a first application:

```
edited a.py (line 2-3)
  ↳ re-applied: the text this edit produces was already present around the anchor — this is a SECOND application, not a repeat of the first
```

**The test is positional, not "does the file already contain `new`".** The signal fires only when the occurrence of `old` about to be replaced is *contained inside* an existing occurrence of `new` — the literal statement "this edit's result is already here, around this anchor". A `new` that happens to exist elsewhere in the file (inserting `return None` into a file that already has one in another function) is a first application and says nothing. A signal that fires on first applications is noise, and noise is how a footer field stops being read. It applies to `edit` (colon CLI and payload route); `replace` is replace-all and already reports occurrence counts, and `vim` is scripted rather than anchored, so neither is instrumented.

Interdependent edits are the normal case for a batch, and a half-applied set is rarely wanted — but `batch:` is **not** atomic by default, and this change does not make it so. Set `continue_on_error: false` in the payload wrapper to stop at the first failure. Preview ops (`replace_dry`) and read-only ops get no `[result]` — a read op's own count line is already the last thing printed, so nothing intervenes to misread.

The per-op receipt has **not** moved: it is still printed above `[validators]`, so anything parsing output positionally is unaffected.

`[branch: X]` stays the final line — right file, wrong branch is otherwise silent until commit time. A failed `edit` also reports why the anchor probably missed: **the replacement text already being in the file**, doubled backslashes (TOML literal strings don't process escapes), a whitespace-only difference with its line number, or the nearest match by similarity.

The first of those is the other half of the re-run problem (#984). `re-applied` covers a payload whose `new` contains its `old`; when it does not, re-running an applied payload reports `ERROR: old string not found`, which is character-for-character what a genuinely wrong anchor prints — and the two have opposite remedies. So when the replacement text is present, the receipt says where:

```
ERROR: old string not found in a.py
  ↳ the replacement text is ALREADY present at line 12 — this looks like a re-run of an edit that already applied, not a broken anchor
```

A located fact, not a verdict. The `ERROR` stands, the op is still counted in `K skipped`, and the call still exits non-zero — downgrading a failure because it is probably benign is how a loud bug becomes a quiet one.

### The nearest match, and when it declines to name one

A multi-line anchor is scored on the **whole block**, and it names the window rather than a line ([#1489](https://github.com/Digital-Process-Tools/claude-supertool/issues/1489)):

```
ERROR: old string not found in app.py
  ↳ nearest match at lines 804-806 (91%): read:app.py:804-806
```

Until #1489 the score was `SequenceMatcher.ratio()` of the **first non-blank line of `old`** alone. For a multi-line anchor that line is usually boilerplate — a `def`, a `}`, an import — so the run that filed the issue was pointed at a line ~800 away from the right one, at an identical 68%, twice. The number was not too low: it was a fact about one line of an anchor the caller had written four lines of, and raising the floor would only have withheld the good hints alongside the bad. Two passes replace it — a sliding line-multiset over every window of the anchor's height to locate candidates, then the character ratio on the top ones, so the percentage printed still means what it used to.

Where several places score the same, the hint says so instead of resolving the tie by file order:

```
ERROR: old string not found in app.py
  ↳ cannot suggest a nearest match: 2 places score the same (91%) — lines 1, 44. The anchor does not tell them apart; re-anchor on a longer or more distinctive block
```

The count is a **floor** when more places tied than the scan char-scored — at most 20 windows, in file order:

```
ERROR: old string not found in app.py
  ↳ cannot suggest a nearest match: at least 34 places score the same (98%) — lines 1 and 33 more. The anchor does not tell them apart; re-anchor on a longer or more distinctive block
```

Until [#1614](https://github.com/Digital-Process-Tools/claude-supertool/issues/1614) that floor was unreachable for any anchor over 20 lines. Rival candidates within the anchor's own height of the leader are one neighbourhood rather than two answers, and for a tall anchor all 20 sampled windows sit inside one such neighbourhood by arithmetic — so the tie was consulted only on a list that was empty by construction, and a 30-line anchor over 34 tied windows got `nearest match at lines 1-30 (98%)`: one sample of 20, named as a fact, ~470 lines from the block the caller wrote. More tied than scored is now its own reason to decline.

That third state exists because a confidently wrong line number costs more than none: the caller reads the wrong 30 lines and re-anchors against them. It also fires when the scan's cost budget runs out before the file does — a best-so-far over a prefix is not a best, and reporting it as one is the defect this whole hint is about. The budget is what keeps the diagnostic off the critical path on a file the line-count guard cannot see: 60 lines of a 40 KB minified bundle took over 30 minutes before it existed. A percentage computed over a clipped line says so in the same breath: `(100%, scored on the first 1000 characters)`.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `vim` | `vim:::PATH:::SCRIPT` | vim-flavored cursor-based multi-action edit. SCRIPT is parsed like a real vim macro: chars are verbs in NORMAL mode; insert verbs (`i`/`a`/`A`/`I`/`o`/`O`), search (`/`/`?`), and ex (`:...`) enter "greedy" modes where all following chars are TEXT/PAT until `\e` (ESC, U+001B) returns to NORMAL. **No separator chars** — `;`, `{`, `}`, newlines etc. are literal data, never special. **Cursor:** `gg`/`G` (BOF/EOF), `nG` (goto line), bare `:N`/`:$`/`:.` (line goto), `0`/`$` (BOL/EOL), `/PAT`/`?PAT` (search fwd/bwd), `nh`/`nl`/`nj`/`nk` (move). **Inserts** (TEXT runs until `\e` or EOS, `\n`/`\t` decoded): `iTEXT`/`aTEXT`/`ITEXT`/`ATEXT`/`oTEXT`/`OTEXT`. `o`/`O` AUTO-INDENT first line to current line's indent. **Deletes:** `x`/`nx`, `dd`/`ndd`, `D`. **Ex:** `:s/PAT/REPL/[gid]` (literal-fallback on unescaped parens), `:%s`, `:Nd`/`:N,Md`/`:.,/PAT/d`, `:g/PAT/d`/`:v/PAT/d`, `:r FILE`/`:r -`/`:Nr FILE`, `:Na\nBODY\n.` (ex append after line N), `:w`/`:wq` (no-op — supertool writes atomically). **DEFAULT EDIT OP** for any pattern-based edit. |
| `edit` | `edit:::OLD:::NEW:::PATH` | Single-file, single-occurrence edit (mirrors native Edit). Errors if 0 or >1 matches. **Bypasses native Edit must-Read state** — saves a round-trip when you already know the unique snippet. Use `:::` separator so content with `:` works. |
| `replace` | `replace:::OLD:::NEW:::PATH` | Recursive find/replace across PATH. Use `:::` separator when content has `:`. |
| `replace_dry` | `replace_dry:::OLD:::NEW:::PATH` | Preview of `replace` — shows what would change without writing. |
| `replace_lines` | `replace_lines:::PATH:::START:::END:::CONTENT` | Swap lines `[START, END]` (1-indexed, inclusive) with CONTENT. `END < START` = pure insert before line START. Empty CONTENT = delete. Receipt shows new line numbers + ±2 context. |
| `paste` | `paste:::PATH:::CONTENT` | **NARROW USE:** replace ENTIRE file. Only for creating a new file or fully rewriting one. NOT for partial edits — `vim` is the default for those. Atomic, creates file + parent dirs if missing. CONTENT via triple-colon → holds any chars (`:`, quotes, braces, newlines). Overwriting an existing file copies its outgoing bytes to `~/.cache/supertool/paste-backup/` first and the receipt names the copy and its mode, which is the overwritten file's own; a *created* file lands at `0666 & ~umask` and the receipt states the mode — both below. |
| `append` | `append:::PATH:::CONTENT` | Append CONTENT to the end of a file, creating it if missing — a file it creates lands at `0666 & ~umask` and the receipt states the mode, same as `paste` below. No `wc` round-trip, no inverted-range `replace_lines` trick. Adds a missing trailing newline first so the block starts on its own line. |

### A `paste` over an existing file keeps the bytes it displaces

Every other mutating op fails on a path that is not there: `edit` and `replace`
match a string, and `vim` and `replace_lines` both return `file not found`.
Only `paste` succeeds either way. That is the whole of the claim — not that
nothing else destroys bytes, because plenty does: `vim` empties a file with
`ggdG`, and `replace_lines` *clamps* an `END` of `total + 1` rather than
refusing it. What none of them can do is destroy a file the caller believes is
not there, which is how an agent creating what it believed was a new note
replaced 8922 bytes it did not know existed
([#1650](https://github.com/Digital-Process-Tools/claude-supertool/issues/1650)).
The path was gitignored, so there was no git copy, and the receipt — honest and
complete — printed one step after the only moment it could have helped.

So the outgoing bytes are copied aside **before** the write, and the receipt
says where:

```
rewrote notes/fix-1598-1584.md (24 lines, 8922 → 1990 bytes)
  ↳ previous contents kept at ~/.cache/supertool/paste-backup/3f43a7b64beb777c-1786684186291800000.bak (mode 0644, the file's own)
```

**The copy inherits the overwritten file's mode**
([#1685](https://github.com/Digital-Process-Tools/claude-supertool/issues/1685)).
It used to be written at `0666 & ~umask` whatever the source was, so a `paste`
over a `0600` `.env`, `id_rsa` or `.netrc` left the secret group- and
world-readable under `~/.cache/supertool` for the seven-day retention window.
The snapshot is not redacted — it is a backup, and a redacted backup is not one
— so what has to hold instead is that reading the copy is never easier than
reading the original. A mode that cannot be read falls back to `0600`, never to
the umask default. Nothing is refused on account of the mode: declining to
snapshot a mode-restricted file would close the disclosure by deleting the
data-loss net this whole section is about.

Not stated on Windows, where `os.chmod` honours only the read-only flag and
`st_mode` reads back `0o666` whatever was asked for — a mode line there would
be a claim about access that does not hold.

**Nothing is refused, and that is the design.** A guard that blocked the
overwrite would have to offer a `force` token, and a `force` token typed by
reflex is the guard deleting itself. A copy has no case it was not written for:
the write the caller asked for happens either way, and a false positive costs
one cache file.

**There is no size or shrink threshold.** A rewrite that *grows* the file loses
the old bytes just as completely as one that shrinks it, so the trigger is the
op's own semantics — `paste` replaces the whole file — and not a number.

Three states, never silence:

| Receipt | Meaning |
|---------|---------|
| `↳ previous contents kept at PATH (mode NNNN, the file's own)` | the displaced bytes are at PATH, readable by no more people than the file was |
| *(no line)* | nothing was displaced — the file was created, or already held exactly what was written |
| `↳ no backup of the previous contents — WHY` | the copy could not be made. The write still happened; WHY names the reason (unreadable file, unwritable cache, a `paste-backup` that is a symlink rather than a directory, or over the 8 MB copy limit) |

**A write that a validator rolls back still leaves a copy.** The snapshot is
taken before the write, and whether the write survives validation is not known
until after it — so a rolled-back `paste` prints `previous contents kept at
PATH` above `[rolled back] … the file was NOT edited`. Both are true: the file
on disk is the original, and PATH holds those same bytes. The copy is redundant
rather than wrong, and `gc` reaps it with the rest.

### A file `paste` or `append` creates lands at the umask, and the receipt states the mode

A created file gets `0666 & ~umask` — `0644` under the common `umask 022` —
the same mode `>`, `tee`, `cp` and every editor produce. It used to get `0600`,
which was never a chosen default: `_atomic_write` renames a `mkstemp` temp file
over the target and mkstemp creates at `0600`
([#1275](https://github.com/Digital-Process-Tools/claude-supertool/issues/1275)).
An **overwrite** is unchanged and still keeps the target's own mode
([#259](https://github.com/Digital-Process-Tools/claude-supertool/issues/259)),
so a file that is already `0600` stays `0600` through any number of pastes.
For owner-only creates, set `umask 077` — the mask is now honoured rather than
overridden.

The mode is stated on every create, because a mode nobody is told about is a
fact the reader has no reason to check:

```
created scripts/deploy.sh (12 lines, 0 → 240 bytes)
  ↳ mode 0644, from the process umask
  ↳ starts with `#!` but is not executable — `chmod +x scripts/deploy.sh` to run it
```

**`paste` never infers the executable bit** — not from the shebang, not from
the modes of the file's neighbours, not from a flag. The second line above is a
statement about what is on disk, not a decision about it: the mode is identical
whether or not the content starts with `#!`, so a wrong reading costs one line
of prose instead of a permission nobody asked for. Matching the neighbours is
the most likely to be right and the most surprising when it is wrong, which is
why it is disclosure and a `chmod` command rather than a guess.

**On Windows neither line is printed.** There is no executable bit there and
`os.chmod` honours only the read-only flag, so `st_mode` reads back `0o666`
whatever was asked for and a mode line would be a false statement about access.

Copies are reaped with the rest of the cache: `gc:dry` previews, `gc:run`
deletes, retention 7 days, overridable per kind under `gc.retention_days` in
`.supertool.json`.

## Common patterns

Pattern-based edit at a searched location (default case):

```bash
./supertool 'vim:::src/app/Config.py:::/DEBUG\e:s/False/True/'
```

Single known unique snippet — skip the Read round-trip:

```bash
./supertool 'edit:::return False;:::return True;:::src/app/Module.py'
```

Preview a cross-file rename before committing:

```bash
./supertool 'replace_dry:::OldClassName:::NewClassName:::src/'
```

Then apply:

```bash
./supertool 'replace:::OldClassName:::NewClassName:::src/'
```

Insert a line before line 42, delete lines 10–12:

```bash
./supertool 'replace_lines:::src/app/Module.py:::42:::41:::    new_line_here' \
            'replace_lines:::src/app/Module.py:::10:::12:::'
```

Create a new file (or fully rewrite one):

```bash
./supertool 'paste:::src/app/NewModule.py:::class NewModule:\n    pass'
```

## Input forms

### Triple-colon separators (`:::`)

All edit ops use `:::` as the field separator so content with `:` (SQL, URLs, timestamps, code) works unambiguously.

### `@file` route — long or structured payloads

When `old` or `new` spans multiple lines or contains characters that clash with shell quoting, write a JSON file and pass it with `@`:

```bash
./supertool 'edit:@.max/my-edit.json'
```

```json
{ "old": "return false;", "new": "return true;", "path": "src/app/Foo.py" }
```

Use `@-` to pipe from stdin instead of a file — a heredoc keeps the whole edit in one command with nothing written to disk, and a TOML body takes raw multi-line code with no escaping:

```bash
./supertool 'edit:@-' <<'EOF'
path = "src/app/Foo.py"
old = '''return false;'''
new = '''return true;'''
EOF
```

Because the line starts with `./supertool` (not `cat`/`echo`), this is the form to reach for under enforced or autonomous runs that block bare shell builtins. See [Which form?](../input-forms.md#which-form) for choosing between colon-CLI, `@-`, and `@file`.

#### Backslashes in a literal block

A TOML triple-single-quoted block processes **no** escapes. Whatever you type is what lands, so a doubled backslash typed out of escape reflex is two characters in the file:

```toml
new = '''PAT = re.compile("\\d+")'''
```

writes `\\d+`, not `\d+`. Same for a newline: `\n` inside a literal block is the two characters `\` and `n`, never a line break — type a real newline instead. Nothing on this route evaluates an expression, so a function call written into payload content lands as its own characters; when you need an escape sequence processed, use a basic block, where TOML escapes do apply. An ESC is spelled `\u001b` there; `\x1b` is not a TOML escape at all, so it is refused at parse time rather than landing wrong.

The safe half is `old`: a doubled anchor cannot match, the op declines, and the skip is counted. The half that is not is `new` — the anchor matches, the bytes land, the receipt says `edited`, and the validators agree, because two backslashes are legal in nearly every language this repo edits. So a payload whose literal block carries a lone `\\` is named before any op runs:

```text
⚠ payload: a ''' literal block carries `\\`. A literal block processes NO escapes, so each pair reaches the file as TWO backslashes -- if you meant one, write one.
  ↳ `new` (1 occurrence): PAT = re.compile("\\d+")
  ↳ this is a note, NOT a correction -- nothing was rewritten, because a pair is sometimes exactly what was meant and guessing in the write path is worse than the bug.
```

It is a **note, not a correction**, and never a refusal. Some payloads genuinely want two characters, and collapsing them would guess at intent in the write path, where a wrong guess costs more than the bug it replaces. Two things are deliberately not flagged, because a warning that fires on the correct spelling is one authors stop reading:

- a `"""basic"""` block, where `\\` *is* one backslash and is the right spelling;
- a run of three or more, which was counted rather than produced by reflex.

To write one backslash, write one. To write a pair deliberately, keep the literal block — the note is only a note — or say it in a basic block, where each backslash doubles.

### `batch:@file` — mixed ops in one round-trip

Read, edit, and re-read in a single call:

```bash
./supertool 'batch:@.max/ops.json'
```

```json
[
  { "op": "read", "path": "src/app/Config.py" },
  { "op": "edit", "old": "DEBUG = False", "new": "DEBUG = True", "path": "src/app/Config.py" },
  { "op": "read", "path": "src/app/Config.py" }
]
```

`batch:` prints `[result] N ops run, M writes, K skipped` **twice** — once above the first op and once at the very bottom. The footer is the canonical one and `[branch: X]` still ends the output, but that footer sits below a validators block long enough that `| tail` lands on `git-status : ok` and reads as success. The leading copy is what makes a non-zero `K` visible without a filter. A single op keeps one count: its receipt is three lines with the footer already adjacent to them.

A **nested** `batch` takes its inner payload in the `path` field, and the `@` is
required there exactly as it is on the command line:
`{ "op": "batch", "path": "@inner.toml" }`. Any other key is refused by name.
Before [#1417](https://github.com/Digital-Process-Tools/claude-supertool/issues/1417)
a single unrecognised key was accepted as an unnamed positional, so
`"file": "inner.toml"` ran as `batch:inner.toml` and was refused for the missing
`@` — a message in command-line grammar, about a field the caller had not typed.

Each write sub-op's fields (`old`/`new`/`content`/…) are taken **literally** — the structured payload bypasses the `:::` tokenizer and the shell-escape decoder, so content that itself contains `:::` or backslashes survives byte-for-byte, exactly as a standalone `edit:@file` call behaves. You never re-escape payload content for batch.

## Line endings

`edit`, `replace`, `replace_lines` and `append` read the target without
universal-newline translation, so a CRLF or CR file keeps its endings on every
line the op did not name. Before #1049 the first three did not: a one-line edit
to a Windows-authored file rewrote every line to LF, under a receipt naming one
line.

You still write payloads in LF. If an `old` string does not match a CRLF file
byte for byte, `edit` and `replace` retry it re-terminated to the file's
convention and re-terminate the replacement to match — a file that matches
exactly is never reinterpreted.

`replace_lines` passes your content through **verbatim**, mixed endings and all:
the endings inside a block you typed are your choice, and rewriting an explicit
choice is the same silent normalisation pointed the other way. The one ending it
invents is the trailing newline, when your block does not end a line at all;
that takes the ending of the line it lands on rather than a file-wide majority,
which on a mixed file would rewrite your own line to the other convention.

The receipt speaks only where the ending used had more than one defensible
answer — a mixed file, or your own text re-terminated to match. **Mixed means
mixed after the write**, and the census counts the file you are about to open:
asking the pre-write bytes meant a write that *created* the mixedness — an LF
block into an all-CRLF file — could never reach the branch that exists for it,
and shipped under a receipt that said nothing. That holds whether or not the op
had to invent a trailing ending: a block that already ends a line invents
nothing and discloses just the same. For `edit` and `replace_lines`, one file,
one line:

```
edited src/main.rs (line 12-13)
  ↳ line endings: file is CRLF, so the text you wrote with LF was re-terminated to CRLF to match — every untouched line is unchanged
  ↳ line endings: file is mixed (2 CRLF / 2 LF / 0 CR) — every line this op did not touch kept its own; text this op supplied uses LF
```

`replace` is multi-file and the answer differs per file, so it marks the
convention on each file's own line and says the sentence once. A file that
matched byte for byte is not marked — one line of the receipt below is a
statement about `src/win.rs` and not about `src/nix.rs`:

```
(2 replacements in 2 files)
  src/nix.rs (1)
  src/win.rs (1) [CRLF]
  ↳ line endings: the text you supplied did not match 1 of these files byte for byte and was re-terminated to the convention marked above to make it match — every untouched line is unchanged
```

A clean edit or replace of a uniform CRLF file says nothing: nothing was
decided, and on Windows every file is CRLF, so a note there would fire on every
call ever made.

`edit` re-terminates the text you supply to match a file that uses one
convention throughout, whether or not your `old` needed re-terminating to
match. The two are one rule, not two: an `old` spanning a line boundary and a
single-line `old` must not write different bytes for the same `new`. So
replacing `beta` with `BETA\nGAMMA` in a CRLF file inserts two CRLF-terminated
lines, and says so. On a *mixed* file there is no convention to match, your
bytes go in exactly as typed, and the mixed note says that instead.

`replace` resolves the convention once, in the pass that scans for matches, and
carries it to the pass that writes. The two used to read the same file with
different newline settings: on a CRLF file the scan found an LF `old` and the
write did not, so the write was a no-op and the receipt reported the scan's
number. Its count now comes from the bytes it is about to write, and a file that
matched during the scan but no longer matches at write time is named as not
modified rather than counted or dropped.

`vim` is the exception and does not yet preserve endings — its line model
assumes `\n` throughout, and half-adopting this would produce scattered mixed
endings rather than a clean whole-file one.

## Bytes that are not valid UTF-8

Every edit op — `edit`, `replace`, `replace_lines`, `append` and, since #1059,
`vim` — reads with `errors="surrogateescape"`, so a byte that is not valid UTF-8
comes back out of `_atomic_write` as the same byte. A stray latin-1 byte in a
comment, a file with mixed encodings, partial binary content: the bytes your op
never named are the bytes that were there before.

`vim` alone read with `errors="replace"` until #1059. That turns every such byte
into U+FFFD **in memory**, and `vim` writes the whole buffer back — so a single
`cc` on line 1 destroyed a byte on line 40, permanently, under a receipt that
named line 1 and reported nothing wrong. The same read mode was live on `:r FILE`
(which splices a second file into the buffer) and on the two re-reads inside
`:norm`. All four now use `surrogateescape`.

The refusal-shaped fix — decline to open a file holding non-UTF-8 bytes — was
considered and rejected: it trades destruction for an outage on files that work
today, which is the loud-for-quiet trade pointed the wrong way.

Receipts echo the buffer (context lines, a diff hunk), and a lone surrogate
cannot be encoded to a UTF-8 stream. Those are rendered as U+FFFD **in the
receipt only** — mojibake on screen, exact bytes on disk. Ops that turn decoded
text back into bytes or into a path still refuse rather than guess.

`vim` still does not take `newline=""`, deliberately, for the reason above.

## What counts as a line

`read` and `replace_lines` used to disagree. `read` split the file's bytes
(LF, CR, CRLF); `replace_lines` split the decoded string, and `str.splitlines()`
also breaks on U+000B, U+000C, U+001C, U+001D, U+001E, U+0085, U+2028 and
U+2029. A file holding any of those had two line numberings, and the ops that
read sat on the opposite side from the ops that write: you read your target at
line N, asked for line N, and the write landed somewhere else. Nothing reported
a problem (#1060).

One function owns the definition now and every line-numbering op uses it. The
definition is the **conservative** one — LF, CR and CRLF only — because that is
what a caller counting lines in an editor, in `wc -l`, or in any line-oriented
CLI will have counted. This is a contract change for `replace_lines` on files
containing those eight characters, and it is stated rather than picked by
whichever function was easier to edit.

Where the two definitions genuinely differ, a `read` says so rather than
silently choosing:

```
(412 lines, 18022 bytes)
note: contains U+2028 — supertool numbers lines by LF / CRLF / CR only, so a tool that also breaks on these (Python's str.splitlines, some editors) numbers this file differently. supertool's reads and its line-addressed edits agree with each other.
```

An ordinary file says nothing.

## See also

- [docs/validators.md](../validators.md) — full validator reference: bundled list, rollback behavior, adding your own
- [index.md](index.md) — full op table with all categories
