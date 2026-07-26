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

## `@file` route — long or structured payloads

Mutating ops (`edit`, `replace`, `replace_lines`, `paste`, `vim`) accept a payload file instead of inline args. Pass the path with an `@` prefix; use `@-` for stdin.

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

`continue_on_error` defaults to `true` — a failed op is reported but the rest of the batch continues. Set to `false` to abort on first error. Validators (phplint, xmllint, etc.) fire per mutating op, same as inline edits. Use `@-` to pipe the payload from stdin.
