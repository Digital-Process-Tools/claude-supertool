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

Three tiers — supertool picks the best available at first `map` call (result cached for the session):

| Tier | Trigger | What you get |
|------|---------|--------------|
| 1. tree-sitter | `tree_sitter_language_pack` or `tree_sitter_languages` importable | Full AST: accurate nesting, signatures, all node types |
| 2. ctags | `ctags` on PATH (universal-ctags) | JSON tags: class/method/function/constant with scope |
| 3. regex | Always available | Pattern matching on `class`, `function`, `def`, `interface`, `trait`, `enum`, `const`, `struct`, `impl` |

Fallback between tiers is automatic and needs no configuration. Falling off the
end of the chain, however, is stated out loud — see "When no tier can look".

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

The report line reads `tier: none` when nothing in the run was parseable —
naming a tier that never had a pattern to try would repeat the same mistake one
line higher. A mixed run keeps the tier that actually produced symbols.

Directory walks only visit extensions some tier claims, so this disclosure is
reached by naming a file directly. That is exactly the case the issue was
filed from.

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

**"Give me the shape of this module"**

```bash
./supertool 'map:src/SiProject/'
```

Costs ~20 tokens instead of reading every file. Use it before deciding which file to `read` next.

**"Find all classes that implement X"**

```bash
./supertool 'map:src/' | grep -i "implements MyInterface"
# or pipe through supertool's own grep:
./supertool 'map:src/' 'grep:implements MyInterface:src/'
```

**"Compare structure before/after a refactor"**

```bash
# Before (on a git revision via temp checkout or diff)
./supertool 'map:src/OldModule/'

# After
./supertool 'map:src/NewModule/'
```

**"Orient fast, then read the interesting part"**

```bash
./supertool 'map:src/app/' 'between:handle_request:src/app/Module.py'
```

`map` tells you what exists and where; `between` extracts the body once you know the symbol name.

## See also

- [search.md](search.md) — `between` extracts a full symbol body once you know its name from `map`
- [reads.md](reads.md) — `read` for full file content after `map` narrows the target
- [index.md](index.md) — full op table

## tree-sitter integration

When [`tree-sitter-language-pack`](https://pypi.org/project/tree-sitter-language-pack/) (Python 3.10+) or [`tree-sitter-languages`](https://pypi.org/project/tree-sitter-languages/) (Python 3.8–3.12) is installed, `map` uses tree-sitter for AST-based symbol extraction instead of ctags or regex.

- Detects installed package at first `map` call (cached for session)
- Prefers `tree-sitter-language-pack` over `tree-sitter-languages` when both are present
- Falls back to ctags → regex when neither is installed
- No configuration needed — pure detection

tree-sitter is optional. The `map` op works without it — tree-sitter just gives more accurate nesting and signature details.
