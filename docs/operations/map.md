# map — symbol tree extraction

`map` gives you the skeleton of a file or directory: every class, function, method, constant, and struct — as an indented tree with line numbers, without reading the full source. It's the fastest way to understand the shape of an unfamiliar codebase or verify a refactor didn't move something unexpected.

Use it when you want orientation, not content. Reading a 500-line file costs 500 lines of tokens; `map` costs ~20.

## Syntax

```
map:PATH
```

- `PATH` can be a single file or a directory (recursive).
- Directories skip `vendor/`, `.git/`, `Generated/`, `node_modules/` automatically.

```bash
# Single file
./supertool 'map:src/Module.php'

# Directory (recursive)
./supertool 'map:src/SiProject/'

# Batch with reads
./supertool 'map:src/' 'read:src/Module.py'
```

## Output shape

Indented tree. Each entry shows the symbol name and line number in brackets.

```
src/SiProject/SiProjectModule.class.php (55 lines)
  class SiProjectModule  [31]
    const TYPE_PRIMARY  [39]
    const MENU_ITEM  [42]
    method init  [48]

src/SiProject/SiProjectPermissions.class.php (120 lines)
  class SiProjectPermissions  [12]
    const CAN_VIEW  [14]
    const CAN_EDIT  [15]
    method getPermissions  [20]
    method checkAccess  [45]
```

## Backend chain

Three tiers, applied **per file**:

| Tier | Trigger | What you get |
|------|---------|--------------|
| 1. tree-sitter | `tree_sitter_language_pack` or `tree_sitter_languages` importable | Full AST: accurate nesting, signatures, all node types |
| 2. ctags | `ctags` on PATH (universal-ctags) | JSON tags: class/method/function/constant with scope |
| 3. regex | Always available | Pattern matching on `class`, `function`, `def`, `interface`, `trait`, `enum`, `const`, `struct`, `impl` |

Fallback between tiers is automatic and needs no configuration. Falling off the
end of the chain, however, is stated out loud — see "When no tier can look".

**A file falls to the next tier when the one above cannot look at it, not when
it looks and finds nothing.** tree-sitter is consulted for an extension it has
a grammar for; if that grammar is missing or fails to load, the file drops to
ctags, and then to regex. A file tree-sitter *parsed* and found no definitions
in does not drop — the answer is already known, and re-reading it in a ctags
subprocess would cost ~40ms per file to arrive at the same empty list. On this
repository's own tree, 14 of 100 mapped files are that case and none of them
is one ctags could help with, which is why the gate is on blindness rather
than on emptiness ([#913](https://github.com/Digital-Process-Tools/claude-supertool/issues/913)).

Until #913 tier 2 was skipped for the whole run whenever tree-sitter merely
*imported*, so the chain was really tree-sitter XOR ctags, then regex — and
installing universal-ctags to cover a language tree-sitter lacks did nothing
at all on any machine with the language pack present.

### Installing for richer output

```bash
# Tier 1 — tree-sitter (best; requires Python 3.10+)
pip install tree-sitter-language-pack

# Tier 1 alternative — works on Python 3.8–3.12
pip install tree-sitter-languages

# Tier 2 — ctags (good; works everywhere)
brew install universal-ctags   # macOS
apt install universal-ctags    # Linux
```

Without either, the regex fallback works for all supported languages — nesting detection is limited (except Python, which uses indentation).

## Language support

| Language | tree-sitter | ctags | regex |
|----------|-------------|-------|-------|
| PHP | Full AST | classes, methods, consts | class, function, interface, trait, enum, const |
| Python | Full AST | classes, functions | class, def |
| JavaScript | Full AST | functions, classes | function, class, const |
| TypeScript (+ JSX/TSX) | Full AST | functions, classes | function, class, const |
| Go | Full AST | functions, types | func, type, struct, interface |
| Rust | Full AST | functions, structs, impls | fn, struct, impl, enum, trait |
| Java | Full AST | classes, methods | class, interface, enum |
| Ruby | Full AST | classes, methods | class, def, module |
| Markdown (`.md`, `.markdown`) | Heading tree (`h1`..`h6`) | headings | — |

Markdown maps its headings rather than its code: `# Title` becomes `h1 Title`,
and each level indents one step further. `read:` is deliberately not affected —
a heading list is orientation, not a substitute for the prose beneath it, so a
large `.md` still reads as its source. See [reads](reads.md#abstract-read).

## When no tier can look

`map` has three answers, not two, and the difference matters:

| Render | Means |
|--------|-------|
| `class Foo  [12]` … | symbols found |
| `(no symbols)` | a tier parsed the file and it defines nothing |
| `(no symbol parser for .ext - …)` | **no tier has a grammar or pattern set for this extension** |

The third used to render as the second, so `map:CHANGELOG.md` on a file with
200 headings reported `(no symbols)` — the tool's blind spot stated as a
property of the document (#887). It now says which it is, and names what was
available when it declined:

```
$ ./supertool 'map:notes.rst'
(1 files, tier: none)
notes.rst (240 lines)
  (no symbol parser for .rst - tree-sitter and the regex tier have no .rst grammar)
```

With `ctags` on PATH that same call now reaches tier 2 first, and the note
records that it ran rather than that it exists:

```
  (no symbol parser for .rst - tree-sitter and the regex tier have no .rst grammar; ctags found nothing)
```

`; ctags found nothing` means ctags was invoked on this file and returned no
tags. Its absence means ctags was not asked — either no binary on PATH, or a
tier above had a grammar and answered.

The report line reads `tier: none` when nothing in the run was parseable —
naming a tier that never had a pattern to try would repeat the same mistake one
line higher. A mixed run keeps the tier that actually produced symbols.

Directory walks only visit extensions some tier claims, so this disclosure is
reached by naming a file directly. That is exactly the case the issue was
filed from.

## When the file is a facade

`map:supertool.py` is correct and misleading at once. `supertool.py` is a
~170-line entry-point shim; the tool is `_supertool.py` beside it ([#931](https://github.com/Digital-Process-Tools/claude-supertool/issues/931)).
The map returns the shim's two real symbols, so the reader gets an answer —
and concludes they have seen the module's surface:

```
$ ./supertool 'map:supertool.py'
(1 files, tier: tree-sitter)
supertool.py (171 lines)
  def _marker_lines  [68-80]
  def _refuse_unimportable_core  [83-145]
(note: supertool.py is only the entry point — supertool's implementation lives in _supertool.py beside it (#931). The symbols above are the shim's own and this map is complete for that file; supertool's own surface is next door. Add map:_supertool.py to see it.)
```

[#1259](https://github.com/Digital-Process-Tools/claude-supertool/issues/1259)
disclosed the same pair under `grep`, `around` and `between`, but only where the
result was **empty** — an absence read as an absence in the world. A non-empty
result is the harder half ([#1272](https://github.com/Digital-Process-Tools/claude-supertool/issues/1272)):
an absence at least looks like nothing, whereas an answer-shaped answer gives
nobody a reason to make the second call.

So the note is worded as disclosure, never as a correction. The symbols listed
really are the shim's and the map really is complete for that file, so it
affirms the result and offers `map:_supertool.py` as a call to **add** — not a
re-run to replace, which is what #1259's empty-case wording says.

Two things it does not do. It does not scan `_supertool.py` for you: answering a
question nobody asked, and reporting it as the answer to the one they did, is a
quieter wrong answer than the one it replaces. And it stays silent on
`map:<dir>`, because a directory walk already enumerates `_supertool.py` on the
next line — there is nothing left to disclose. The gate is the named pair with
both files on disk side by side, so a lone `supertool.py` in an unrelated tree
is an ordinary file and says nothing.

## Examples per language

**PHP — class with methods and constants:**

```
src/Module.class.php (80 lines)
  class SiProjectModule  [10]
    const TYPE_PRIMARY  [12]
    method init  [20]
    method getDependencies  [35]
```

**Python — module with classes and functions:**

```
src/app/auth.py (120 lines)
  class AuthService  [8]
    method __init__  [10]
    method verify_token  [22]
    method refresh  [45]
  function create_session  [90]
```

**Markdown — heading tree:**

```
CHANGELOG.md (2696 lines)
  h1 Changelog  [1]
    h2 [Unreleased]  [8]
      h3 Fixed  [10]
      h3 Changed  [26]
    h2 [0.25.0] - 2026-08-06  [60]
```

Pair it with `between:` to pull one section out once you know the exact
spelling of its heading — the workflow #887 was filed after guessing wrong at.

**TypeScript — interfaces and classes:**

```
src/api/Client.ts (95 lines)
  interface ClientConfig  [3]
  class ApiClient  [12]
    method constructor  [14]
    method get  [28]
    method post  [44]
```

## Common workflows

**"Give me the shape of this module":**

```bash
./supertool 'map:src/SiProject/'
```

Costs ~20 tokens instead of reading every file. Use it before deciding which file to `read` next.

**"Find all classes that implement X":**

```bash
./supertool 'map:src/' | grep -i "implements MyInterface"
# or pipe through supertool's own grep:
./supertool 'map:src/' 'grep:implements MyInterface:src/'
```

**"Compare structure before/after a refactor":**

```bash
# Before (on a git revision via temp checkout or diff)
./supertool 'map:src/OldModule/'

# After
./supertool 'map:src/NewModule/'
```

**"Orient fast, then read the interesting part":**

```bash
./supertool 'map:src/app/' 'between:handle_request:src/app/Module.py'
```

`map` tells you what exists and where; `between` extracts the body once you know the symbol name.

## See also

- [search.md](search.md) — `between` extracts a full symbol body once you know its name from `map`
- [reads.md](reads.md) — `read` for full file content after `map` narrows the target
- [index.md](index.md) — full op table

## tree-sitter integration

When [`tree-sitter-language-pack`](https://pypi.org/project/tree-sitter-language-pack/) (Python 3.10+) or [`tree-sitter-languages`](https://pypi.org/project/tree-sitter-languages/) (Python 3.8–3.12) is installed, `map` uses tree-sitter for AST-based symbol extraction on every file it has a grammar for.

- Detects installed package at first `map` call (cached for session)
- Prefers `tree-sitter-language-pack` over `tree-sitter-languages` when both are present
- Installing it does not switch the lower tiers off. A file it has no grammar
  for, or one whose grammar fails to load, still falls to ctags and then to
  regex — see [Backend chain](#backend-chain) for why an *empty* parse is not
  a fall-through
- No configuration needed — pure detection

tree-sitter is optional. The `map` op works without it — tree-sitter just gives more accurate nesting and signature details.
