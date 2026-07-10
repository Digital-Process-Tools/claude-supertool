# Edits

Mutating ops for modifying files. Every edit op runs matching validators on the result — syntax failure triggers atomic rollback before the receipt reaches the model.

**Default edit op:** `vim`. Use `edit` for single known snippets, `replace` for cross-file renames, `paste` only for full rewrites or new files.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `vim` | `vim:::PATH:::SCRIPT` | vim-flavored cursor-based multi-action edit. SCRIPT is parsed like a real vim macro: chars are verbs in NORMAL mode; insert verbs (`i`/`a`/`A`/`I`/`o`/`O`), search (`/`/`?`), and ex (`:...`) enter "greedy" modes where all following chars are TEXT/PAT until `\e` (ESC, U+001B) returns to NORMAL. **No separator chars** — `;`, `{`, `}`, newlines etc. are literal data, never special. **Cursor:** `gg`/`G` (BOF/EOF), `nG` (goto line), bare `:N`/`:$`/`:.` (line goto), `0`/`$` (BOL/EOL), `/PAT`/`?PAT` (search fwd/bwd), `nh`/`nl`/`nj`/`nk` (move). **Inserts** (TEXT runs until `\e` or EOS, `\n`/`\t` decoded): `iTEXT`/`aTEXT`/`ITEXT`/`ATEXT`/`oTEXT`/`OTEXT`. `o`/`O` AUTO-INDENT first line to current line's indent. **Deletes:** `x`/`nx`, `dd`/`ndd`, `D`. **Ex:** `:s/PAT/REPL/[gid]` (literal-fallback on unescaped parens), `:%s`, `:Nd`/`:N,Md`/`:.,/PAT/d`, `:g/PAT/d`/`:v/PAT/d`, `:r FILE`/`:r -`/`:Nr FILE`, `:Na\nBODY\n.` (ex append after line N), `:w`/`:wq` (no-op — supertool writes atomically). **DEFAULT EDIT OP** for any pattern-based edit. |
| `edit` | `edit:::OLD:::NEW:::PATH` | Single-file, single-occurrence edit (mirrors native Edit). Errors if 0 or >1 matches. **Bypasses native Edit must-Read state** — saves a round-trip when you already know the unique snippet. Use `:::` separator so content with `:` works. |
| `replace` | `replace:::OLD:::NEW:::PATH` | Recursive find/replace across PATH. Use `:::` separator when content has `:`. |
| `replace_dry` | `replace_dry:::OLD:::NEW:::PATH` | Preview of `replace` — shows what would change without writing. |
| `replace_lines` | `replace_lines:::PATH:::START:::END:::CONTENT` | Swap lines `[START, END]` (1-indexed, inclusive) with CONTENT. `END < START` = pure insert before line START. Empty CONTENT = delete. Receipt shows new line numbers + ±2 context. |
| `paste` | `paste:::PATH:::CONTENT` | **NARROW USE:** replace ENTIRE file. Only for creating a new file or fully rewriting one. NOT for partial edits — `vim` is the default for those. Atomic, creates file + parent dirs if missing. CONTENT via triple-colon → holds any chars (`:`, quotes, braces, newlines). |

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
