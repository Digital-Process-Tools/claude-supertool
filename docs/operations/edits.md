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

**A skip makes the call exit non-zero**, so `&&` chains stop rather than committing a half-applied set. This is the one behaviour change: `replace` finding nothing used to exit `0`, because its receipt says `(0 occurrences of 'x' found)` rather than `ERROR`, and the exit code is derived from the first line of a receipt. A skip is now counted where the decline is *decided*, never inferred from `N - M` — that subtraction is wrong for a multi-file `replace` (more writes than attempts), for `replace_dry` (a preview writes nothing by design), and for a validator rollback (a write genuinely made, then retracted).

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

`[branch: X]` stays the final line — right file, wrong branch is otherwise silent until commit time. A failed `edit` also reports why the anchor probably missed: **the replacement text already being in the file**, doubled backslashes (TOML literal strings don't process escapes), a whitespace-only difference with its line number, or the nearest line by similarity.

The first of those is the other half of the re-run problem (#984). `re-applied` covers a payload whose `new` contains its `old`; when it does not, re-running an applied payload reports `ERROR: old string not found`, which is character-for-character what a genuinely wrong anchor prints — and the two have opposite remedies. So when the replacement text is present, the receipt says where:

```
ERROR: old string not found in a.py
  ↳ the replacement text is ALREADY present at line 12 — this looks like a re-run of an edit that already applied, not a broken anchor
```

A located fact, not a verdict. The `ERROR` stands, the op is still counted in `K skipped`, and the call still exits non-zero — downgrading a failure because it is probably benign is how a loud bug becomes a quiet one.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `vim` | `vim:::PATH:::SCRIPT` | vim-flavored cursor-based multi-action edit. SCRIPT is parsed like a real vim macro: chars are verbs in NORMAL mode; insert verbs (`i`/`a`/`A`/`I`/`o`/`O`), search (`/`/`?`), and ex (`:...`) enter "greedy" modes where all following chars are TEXT/PAT until `\e` (ESC, U+001B) returns to NORMAL. **No separator chars** — `;`, `{`, `}`, newlines etc. are literal data, never special. **Cursor:** `gg`/`G` (BOF/EOF), `nG` (goto line), bare `:N`/`:$`/`:.` (line goto), `0`/`$` (BOL/EOL), `/PAT`/`?PAT` (search fwd/bwd), `nh`/`nl`/`nj`/`nk` (move). **Inserts** (TEXT runs until `\e` or EOS, `\n`/`\t` decoded): `iTEXT`/`aTEXT`/`ITEXT`/`ATEXT`/`oTEXT`/`OTEXT`. `o`/`O` AUTO-INDENT first line to current line's indent. **Deletes:** `x`/`nx`, `dd`/`ndd`, `D`. **Ex:** `:s/PAT/REPL/[gid]` (literal-fallback on unescaped parens), `:%s`, `:Nd`/`:N,Md`/`:.,/PAT/d`, `:g/PAT/d`/`:v/PAT/d`, `:r FILE`/`:r -`/`:Nr FILE`, `:Na\nBODY\n.` (ex append after line N), `:w`/`:wq` (no-op — supertool writes atomically). **DEFAULT EDIT OP** for any pattern-based edit. |
| `edit` | `edit:::OLD:::NEW:::PATH` | Single-file, single-occurrence edit (mirrors native Edit). Errors if 0 or >1 matches. **Bypasses native Edit must-Read state** — saves a round-trip when you already know the unique snippet. Use `:::` separator so content with `:` works. |
| `replace` | `replace:::OLD:::NEW:::PATH` | Recursive find/replace across PATH. Use `:::` separator when content has `:`. |
| `replace_dry` | `replace_dry:::OLD:::NEW:::PATH` | Preview of `replace` — shows what would change without writing. |
| `replace_lines` | `replace_lines:::PATH:::START:::END:::CONTENT` | Swap lines `[START, END]` (1-indexed, inclusive) with CONTENT. `END < START` = pure insert before line START. Empty CONTENT = delete. Receipt shows new line numbers + ±2 context. |
| `paste` | `paste:::PATH:::CONTENT` | **NARROW USE:** replace ENTIRE file. Only for creating a new file or fully rewriting one. NOT for partial edits — `vim` is the default for those. Atomic, creates file + parent dirs if missing. CONTENT via triple-colon → holds any chars (`:`, quotes, braces, newlines). |
| `append` | `append:::PATH:::CONTENT` | Append CONTENT to the end of a file, creating it if missing. No `wc` round-trip, no inverted-range `replace_lines` trick. Adds a missing trailing newline first so the block starts on its own line. |

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

Each write sub-op's fields (`old`/`new`/`content`/…) are taken **literally** — the structured payload bypasses the `:::` tokenizer and the shell-escape decoder, so content that itself contains `:::` or backslashes survives byte-for-byte, exactly as a standalone `edit:@file` call behaves. You never re-escape payload content for batch.

## See also

- [docs/validators.md](../validators.md) — full validator reference: bundled list, rollback behavior, adding your own
- [index.md](index.md) — full op table with all categories
