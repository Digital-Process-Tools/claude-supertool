# Input forms

Three ways to pass arguments to supertool ops.

## Which form? {#which-form}

Pick by the shape of your content, not by habit:

| Use | When | Why |
| --- | --- | --- |
| **Colon-CLI** | Short, single-line content with no `:` / quotes / newlines | Zero ceremony — it's just the op string |
| **`@-` heredoc** | Multi-line / quoted / colon-bearing content, **one** op, nothing to keep | No file written; the command line starts with `./supertool`, so it survives enforced or autonomous runs that block bare shell builtins (`cat`, `echo`, …) |
| **`@file`** | Same messy content, but you want to **re-run, batch, or diff** it | The payload is a durable artifact. Write it with an editor or your harness's file-write tool — **not** `cat > file`, which puts a blocked builtin at the start of the line under enforcement |

The trap to avoid: writing a payload with `cat > .max/x.json <<'EOF'` and then reading it back. That line *starts* with `cat`, so an enforced/autonomous run flags it — and it forces hand-escaped `\n` JSON. If you need a single edit, pipe the heredoc straight into `@-`; if you need the file, write it with a tool, not `cat`.

## Colon-CLI (default)

Most ops take arguments via `:` — `read:PATH:OFFSET:LIMIT`, `grep:PATTERN:PATH:LIMIT`. When content itself contains colons (code, SQL, timestamps), switch to `:::` triple-colon separators: `edit:::OLD:::NEW:::PATH`.

`read` also accepts an explicit line range, `read:PATH:START-END`, inclusive on both ends. Prefer it whenever you know the lines you want: `:OFFSET:LIMIT` reads like `start:end` but is not, and the overshoot is silent — `read:file.py:352:372` returns line 353 onward for 372 lines, not lines 352 to 372.

### When the *pattern* contains a colon

`:::` separates the fields of a mutating op. It does not help a read op, whose problem is the opposite: `grep:PATTERN:PATH:LIMIT` has to work out where PATTERN stops, and PATTERN is exactly the argument most likely to contain a `:` — PHP `Class::CONST`, log prefixes (`ERROR: …`), assertion messages, timestamps, alternations whose last branch ends in one.

The parsers do try. `grep` and `around` peel the trailing integers, take the **last** token as the path, and rejoin everything before it — so the common shape works today and keeps working:

```bash
./supertool 'grep:Element: <:traces.txt:8:0'      # pattern 'Element: <'
./supertool 'grep:passed|failed|Error:log.txt:15' # pattern 'passed|failed|Error'
```

Where it cannot work is when there is nothing on the right to anchor against — omit the path and the pattern's own tail is taken for one:

```bash
./supertool 'grep:A::CONST'     # path 'CONST' -> ERROR, naming the split
```

`between` is worse: `between:re:START:END:PATH` rejoins **rightward**, so a `:` in START or END steals from the path instead.

None of these guess silently — a mis-tokenized read op fails with `path not found` and, when the split is the likely cause, prints how it read your argument and how to escape it. **There is no backslash escape.** `grep:Element\: <:…` appears to work only because the backslash survives into the regex, where `\:` and `:` mean the same thing; it breaks the moment the literal fallback kicks in. Use the payload route instead.

## `@file` route — long or structured payloads

Mutating ops (`edit`, `replace`, `replace_lines`, `paste`, `append`, `vim`) accept a payload file instead of inline args. Pass the path with an `@` prefix; use `@-` for stdin.

Read ops (`grep`, `around`, `grep_around`, `between`, `read`) accept the same route, for the reason above — a payload never has to guess where the pattern ends:

```bash
./supertool 'grep:@-' <<'EOF'
pattern = '''Element: <'''
path = "traces.txt"
limit = 8
context = 0
EOF
```

| Op | Payload fields |
| --- | --- |
| `grep` | `pattern` (required), `path`, `limit`, `context`, `count`, `no_auto_read` |
| `grep_around` | `pattern` (required), `path`, `n`, `limit` |
| `around` | `pattern` (required), `path`, `n` |
| `between` | `symbol` **or** `start` + `end`, plus `path` |
| `read` | `path` (required), `offset`, `limit`, `grep`, `full` |

A read-op argument beginning with `@` is only treated as a payload when it could be one — `@-`, a file that exists, or a lone `@….toml` / `@….json`. `grep:@Override:src/` still searches for `@Override`.

### Where a relative `@payload` path resolves from

**Against the directory the call was made from — never against `cwd:`.**

Two different kinds of path meet in one call, and only one of them belongs to the repo being operated on:

| Path | Resolves against | Why |
| --- | --- | --- |
| the `@reference` itself | the invocation directory | it is an argument you typed, and you wrote the payload where you were standing |
| `path = ` *inside* the payload | the working directory (`cwd:` target) | it is repo content — this is what makes `cwd:` useful |

This matters because `cwd:` exists precisely *because* the target repo has no `./supertool` wrapper. The call is made from a directory that has one, so the payload lands there too — one side of `cwd:`, with the repo on the other:

```bash
./supertool 'cwd:~/other-repo' 'batch:@.max/edits.toml'
#            └─ repo paths          └─ resolved here, next to the call
```

The same rule covers the auto-resolved project root ([#363](https://github.com/Digital-Process-Tools/claude-supertool/issues/363)): whatever moves the working directory, it does not move the `@reference`.

**There is no fallback.** A payload that is not at the invocation root is an error even when a file of that name sits under the `cwd:` target — reading whichever one happens to exist is how a tool starts opening a file the caller did not mean. The error names both roots and says which is which ([#672](https://github.com/Digital-Process-Tools/claude-supertool/issues/672)):

```
ERROR: @file not found: .max/edits.toml
  ↳ @payload paths resolve against the invocation directory: /Users/…/dvsi
    It does exist under the cwd: target /Users/…/other-repo, and is not read from there:
    the @reference is an argument you typed, not repo content. Only `path =` inside the
    payload follows the working directory.
    Pass an absolute path (@/Users/…/other-repo/.max/edits.toml), or write the payload
    next to the call.
```

To drive a repo from a payload stored *inside* it, pass an absolute path — that has always worked and is unaffected.

```bash
./supertool 'edit:@.max/my-edit.json'
./supertool 'paste:@-' < my-paste.toml
```

**Only one `@-` per call.** stdin is a single stream: two `@-` ops both read it — the first drains it, the second reads empty and fails. supertool rejects this up front (`only one '@-' op is allowed per call`) rather than letting it surface as an opaque parse error. For several payload edits in one call, give all but one a real `@file` path, or fold them into a single `batch:@-` ops array.

The payload holds the fields that would otherwise go after `:::`. Format auto-detected from the first non-whitespace character — `{` or `[` → **JSON**, anything else → **TOML**. One exception: a leading `[[` is a TOML table-array header (never valid JSON), so it's read as **TOML** — this is what lets a `[[ops]]` batch payload parse instead of being misread as a JSON array.

### JSON — concise, machine-friendly

```json
{ "old": "return false;", "new": "return true;", "path": "src/app/Foo.py" }
```

Best for CI scripts and sub-agent output (every JSON encoder produces it). Drawback: every backslash and newline in content needs double-escaping (`\\n`, `\\\\`), which compounds fast for code blocks.

### TOML — human-friendly for code-block content

```bash
./supertool 'paste:@-' <<'PAYLOAD'
path = "scripts/deploy.sh"
content = '''
#!/usr/bin/env bash
claude -p "..." --permission-mode bypassPermissions \
  --disallowedTools "Grep,Glob,LS"
'''
PAYLOAD
```

Use TOML's **triple-single-quote literal strings** (`'''...'''`) when content has backslashes, quotes, or `\e` (vim's ESC) — they're preserved byte-for-byte, no escaping. Triple-double-quote (`"""..."""`) supports basic escapes (`\n`, `\t`, `\\`) if you want them.

| TOML form         | When to use                                    |
| ----------------- | ---------------------------------------------- |
| `"basic"`         | Single-line strings with standard escapes      |
| `'literal'`       | Single-line strings, no escape processing (vim scripts: `'/foo\eciw...\e'`) |
| `"""multi\nline"""` | Multi-line with escapes processed             |
| `'''multi\nline'''` | Multi-line, literal — **the default for code blocks** |

### When the content itself contains `'''`

A literal block cannot carry a *run of three* of its own delimiter, and Python
source that inspects Python source hits this immediately:

```
new = '''    if stripped.startswith(("#", "'''", "*")):'''
```

That closes the block at the inner `'''`, and the parse error points at the
column where it closed rather than at what closed it. Two ways out, both of
which have always worked:

| Content contains | Use |
| ---------------- | --- |
| `'''`            | a `"""basic"""` block — escapes apply, so backslashes double |
| `'''` **and** `"""` | the JSON payload form, which needs no delimiter at all |

Since [#394](https://github.com/Digital-Process-Tools/claude-supertool/issues/394)
the parse error names both, and fires the hint on an odd number of `'''` runs —
every literal block opens and closes, so a stray one means the content carried
its own.

### Ending a block with a quote

One or two apostrophes are a different matter, and they are legal. A closing
run may be **four or five** quotes, and the surplus one or two belong to the
content — which is the only way a literal block can end with its own delimiter
character:

```
new = '''    kind = 'mr''''      # -> `    kind = 'mr'`
new = '''ends in two'''''        # -> `ends in two''`
```

There is no escape inside a literal block, so that spelling is the only one.
Reaching for a backslash is the reflex every other language rewards and here it
is inert: `'''    kind = 'mr\''''` parses without complaint and hands the
op `    kind = 'mr\'` — not the line that was typed, and a syntax error in
most languages that receive it. The write went through, the validators agreed,
and the breakage surfaced a language away from its cause.

Since [#834](https://github.com/Digital-Process-Tools/claude-supertool/issues/834)
a backslash immediately before a closing run is **refused**, and the message
spells the caller's own line back both ways — without the backslash, and as a
`"""basic"""` block, where a genuinely wanted backslash doubles.

The issue was filed as *"a payload string ending in `'` writes broken code"*, with
the proposed guard *refuse content ending in `'`*. That is not what shipped, because
the premise is wrong: a value ending in an apostrophe is exactly what the correct
spelling above produces, so the guard would have refused its own fix. The
backslash is the detectable mistake, and it is a **refusal** rather than a warning
for one reason — both readings of it have another spelling, so refusing leaves
nothing unwritable. See `docs/validators.md`, "Declining instead of guessing".

The same reasoning reaches one line up. A literal block writing a **shell file** whose
line ends with `\\` is refused too
([#835](https://github.com/Digital-Process-Tools/claude-supertool/issues/835)): the
block preserves both backslashes, and in bash an even run at end of line is an escaped
backslash rather than a line continuation, so the script parses and runs differently.
Both intents have a spelling — write **one** backslash to continue the line (a literal
block will not eat it), or spell the pair in a `"""basic"""` block, where each doubles
to four. No `allow_literal_backslash` field exists, deliberately: the basic block is
the opt-out, and it says *which* intent was meant instead of only silencing the
question. Outside a literal block the same bytes are a **warning** and still write —
there the caller has no second spelling, and refusing would strand them.

The same run rule applies to `"""` blocks, and both parsers now agree about it: the
fallback used for Python <3.11 closed at the first three quotes and choked on the
surplus, so the spelling this section recommends parsed on 3.11+ and failed below
it — the [#684](https://github.com/Digital-Process-Tools/claude-supertool/issues/684)
rule, one delimiter over.

### An invalid escape is an error, on every Python

A basic string (`"..."` or `"""..."""`) processes escapes, and an escape TOML
does not recognise is a **parse error** — not a character that quietly loses its
backslash. A Windows path is how most people meet this:

```
path = "C:\Users\dev\notes.txt"     # parse error: invalid escape \U
path = "C:\\Users\\dev\\notes.txt"  # fine — doubled
path = 'C:\Users\dev\notes.txt'     # fine — a literal string, kept as typed
```

Before [#684](https://github.com/Digital-Process-Tools/claude-supertool/issues/684)
that first line behaved differently depending on the interpreter: Python 3.11+
(stdlib `tomllib`) raised, while the fallback parser below it dropped the
backslashes and handed the op `C:Usersdevnotes.txt`, which then failed with
`path not found` at an address nobody had typed. Both parsers now agree, and
`\u` / `\U` escapes (`"\u00e9"`) work on every version.

Other TOML primitives supported in payloads: integers (`start = 42`), booleans (`replace_all = true`), `# comments`. Arrays, tables, dotted keys, and dates aren't needed — payloads are flat key/value maps.

### Implementation note

TOML parsing uses stdlib `tomllib` on Python 3.11+; a minimal built-in parser handles 3.9 / 3.10 fallback (bare keys, strings, ints, bools, comments — nothing else).

## `batch:@file` — mixed ops in one round-trip

`batch` runs any combination of read and write ops from a single JSON file:

```bash
./supertool 'batch:@.max/ops.json'
```

Payload — a bare array of op objects, or a wrapper with options:

```json
[
  { "op": "read", "path": "src/app/Config.py" },
  { "op": "edit", "old": "DEBUG = False", "new": "DEBUG = True", "path": "src/app/Config.py" },
  { "op": "read", "path": "src/app/Config.py" }
]
```

Or with explicit options:

```json
{
  "continue_on_error": true,
  "ops": [
    { "op": "read", "path": "src/app/Config.py" },
    { "op": "edit", "old": "DEBUG = False", "new": "DEBUG = True", "path": "src/app/Config.py" }
  ]
}
```

In TOML the ops array is a `[[ops]]` table array — which is what lets a payload full of code blocks skip JSON's double-escaping:

```bash
./supertool 'batch:@-' <<'PAYLOAD'
[[ops]]
op = "edit"
path = "src/app/Config.py"
old = "DEBUG = False"
new = "DEBUG = True"

[[ops]]
op = "read"
path = "src/app/Config.py"
PAYLOAD
```

**Every entry needs its own `op` key**, including the common case where all of them are edits — there is no default. Omitting it fails with `batch op missing 'op' field` rather than guessing, since a batch is routinely mixed (`read` + `edit` + `replace`) and a guess would be wrong as often as right.

**Re-running a payload is not free of consequence.** An edit whose `new` contains its `old` keeps its anchor alive, so a second run applies it a second time — legitimately, and by design. The `[result]` footer names it (`1 op run, 1 write, 1 re-applied`) and the op receipt adds a `↳ re-applied:` line, so a re-run reads differently from a first run instead of identically; see [operations/edits.md](operations/edits.md). It discloses, it does not refuse: appending a repeated element has the same shape and stays allowed.

`continue_on_error` defaults to `true` — a failed op is reported but the rest of the batch continues. Set to `false` to abort on first error. **A batch is not atomic:** under the default, ops that ran before a failure stay applied. The `[result]` footer names the shortfall (`3 ops run, 2 writes, 1 skipped`) and the call exits non-zero whenever anything was skipped, so `&&` chains stop — see [operations/edits.md](operations/edits.md). Validators (phplint, xmllint, etc.) fire per mutating op, same as inline edits. Use `@-` to pipe the payload from stdin.
### What the sub-op headers say

Each sub-op prints a header. A sub-op that ran from the payload is labelled by **route and target**, not re-serialized onto a colon CLI:

```
--- batch:@.max/ops.toml ---
--- replace:@payload → src/app/Config.py ---
--- read:@payload → src/app/Config.py ---
```

**This header is deliberately not re-runnable, and that is the point.** The payload route exists *because* the content contains `:`; flattening those fields back into `replace:OLD:NEW:PATH` does not merely lose information, it produces a string that parses as a **different op**. A `replace` of `time: 10:30` used to render as `--- replace:time: 10:30:time: 11:45:/tmp/h.txt ---`, and pasting that sent the dispatcher looking for a file named `30`. A header is what a reader trusts to reconstruct a step — in a bug report it is often the only surviving record — so it must never be a runnable string that runs something other than what ran. Where no faithful one-line rendering exists, it does not fake one.

Pasting `replace:@payload → …` back is a loud, self-explaining refusal rather than a silent misfire:

```
ERROR: '@payload' is a header placeholder, not a reference. This op ran from an
@payload whose fields no single-colon header can reproduce (#644) — re-run it
from the original payload file or stdin.
```

To re-run the step, re-run the payload — `./supertool 'batch:@.max/ops.toml'` — which is the artifact that holds the fields.

A sub-op typed on the colon CLI is unaffected: its header stays verbatim, because there it genuinely *is* re-runnable.

### Field order in a batch sub-op

For an op with an `@payload` route of its own — every mutating op, plus `grep` / `around` / `grep_around` / `between` / `read` — fields are named and order does not matter.

For the remaining colon-only ops, the payload's fields are placed by the op's **declared** argument order (`head`/`tail`: `path`, `n` · `tree`: `path`, `depth` · `around_line`: `path`, `line`, `n` · `diff`: `path1`, `path2`). An op with more than one field and no declared order **declines** rather than picking one:

```
ERROR: batch sub-op 'glob' takes its arguments positionally and has no declared
payload field order, so limit, pattern cannot be placed. Ordering them
alphabetically is a guess, and a wrong guess dispatches a different op — so this
declines instead.
```

Colon arguments cannot be sparse either: a payload that sets a later positional field without the earlier ones is refused for the same reason.
