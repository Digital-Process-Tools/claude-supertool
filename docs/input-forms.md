# Input forms

Three ways to pass arguments to supertool ops.

## Colon-CLI (default)

Most ops take arguments via `:` — `read:PATH:OFFSET:LIMIT`, `grep:PATTERN:PATH:LIMIT`. When content itself contains colons (code, SQL, timestamps), switch to `:::` triple-colon separators: `edit:::OLD:::NEW:::PATH`.

## `@file` route — long or structured payloads

Mutating ops (`edit`, `replace`, `replace_lines`, `paste`, `vim`) accept a payload file instead of inline args. Pass the path with an `@` prefix; use `@-` for stdin.

```bash
./supertool 'edit:@.max/my-edit.json'
./supertool 'paste:@-' < my-paste.toml
```

The payload holds the fields that would otherwise go after `:::`. Format auto-detected from the first non-whitespace character — `{` or `[` → **JSON**, anything else → **TOML**.

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
