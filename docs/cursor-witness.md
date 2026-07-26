# Cursor Witness

Watch your supertool agent work in real time, inside Cursor (or VSCode).

When supertool **edits** a file in your workspace → Cursor opens a side-by-side **diff view** showing what changed.
When supertool **reads** a range → Cursor opens the file, scrolls to the range, highlights it in blue, and fades the highlight after a few seconds.

The agent stops being an opaque process. Its work surfaces in your editor as it happens.

## What you get

| Op | What you see |
|---|---|
| `edit`, `replace`, `paste`, `append`, `vim`, `replace_lines` | Side-by-side diff (before vs after). Status bar: `$(edit) Max: edit foo.php` |
| `around_line:F:L:N` | File opens, lines L-N to L+N highlighted, centered |
| `between:SYMBOL:F` | File opens, symbol's body lines highlighted (tree-sitter resolution) |
| `read:F:OFFSET:LIMIT` | File opens, range highlighted |
| `read:F`, `map`, `tail`, `head`, `wc`, `stat`, `blame` | File opens and focuses (no range available) |

Highlights fade after **4 seconds** so they don't pile up.

## Architecture

```
supertool 'between:Foo:bar.php'
  └─ runs the op, returns text
  └─ _notify_read_op fires the cursor-witness notifier  (fire-and-forget)
       └─ notify.py writes JSON line to /tmp/supertool-witness-<sha1(cwd)[:12]>.sock
            └─ Cursor extension reads the line
                 └─ vscode.window.showTextDocument(uri, {selection})
                 └─ setDecorations(range, blue tint) + 4s timer
```

- **One socket per workspace** — the path hash includes the workspace folder so multiple Cursor windows on different repos don't collide.
- **Multi-host safe** — Cursor spawns several extension hosts (main, agent-worker, shadow-workspace). They all activate the extension; only one wins the socket bind. The rest stay dormant.
- **Fully silent when extension is off** — the notifier writes to the socket; if no one's listening, the connect fails fast and supertool keeps moving.

## Install

### 1. Build the extension

```bash
cd notifiers/cursor-witness/extension
npm install
npm run compile
```

### 2. Symlink into Cursor's extensions directory

```bash
ln -s "$(pwd)" ~/.cursor/extensions/digital-process-tools.cursor-witness-0.1.0
```

(For VSCode: `~/.vscode/extensions/digital-process-tools.cursor-witness-0.1.0`)

### 3. Reload Cursor

`Cmd+Shift+P` → `Developer: Reload Window`

You should see `$(eye) Max: idle` in the status bar bottom-right.

### 4. Wire the notifier in `.supertool.json`

```json
{
  "notifiers": {
    "cursor-witness": {
      "cmd": "python3 /absolute/path/to/claude-supertool/notifiers/cursor-witness/notify.py {op} {file} {line} {line_end} {before_file}",
      "match": "*",
      "hooks_into": [
        "edit", "replace", "replace_lines", "paste", "vim",
        "read", "around_line", "between", "map"
      ]
    }
  }
}
```

Drop the read ops from `hooks_into` if you only want to see edits.

## Settings

In Cursor: `Cmd+,` → search "cursorWitness".

| Setting | Default | Effect |
|---|---|---|
| `cursorWitness.socketPath` | `""` (auto-derive) | Override the UDS socket path |
| `cursorWitness.openOnRead` | `true` | Focus files on read ops. Set to `false` for edit-only |

## How edits show as diffs

When supertool runs a mutating op:
1. Before the rewrite, supertool reads the file's current bytes
2. Writes them to a temp file `/tmp/supertool-before-XXXX.<ext>`
3. Fires the notifier with `{before_file}` set to that path
4. Extension opens `vscode.diff(before_uri, after_uri)` — side-by-side
5. Extension deletes the temp file after 60s

If the file didn't exist before (new file from `paste`), `{before_file}` is empty and the extension falls back to plain open.

## Status bar

- `$(eye) Max: idle` — extension running, nothing happening
- `$(edit) Max: <op> <filename>` — recent mutation (3s)
- `$(eye) Max: <op> <filename>` — recent read (3s)

Tooltip shows the full path + line.

## Troubleshooting

### Extension activates but socket isn't bound

Cursor spawns multiple extension hosts. The first to bind wins; others stay dormant. If you see `socket already in use by another extension host` in `/tmp/cursor-witness.log`, that's expected — one host is serving.

### Nothing happens when I run a supertool op

Check the log:
```bash
tail -f /tmp/cursor-witness.log
```

You should see `LISTENING on /tmp/supertool-witness-<hash>.sock`. Verify the socket is alive:

```bash
lsof -U | grep supertool-witness
```

### Wrong file opens (or none)

Check the workspace folder Cursor opened matches the cwd you're running supertool from. The socket path hash is derived from the workspace folder. If they disagree, the notifier writes to a socket no one is listening on.

```bash
# Expected socket path:
python3 -c "import hashlib; print('/tmp/supertool-witness-' + hashlib.sha1(b'$(pwd)').hexdigest()[:12] + '.sock')"
```

### Edits don't show a diff

Either `{before_file}` placeholder isn't in your `cmd`, or the file is new (no pre-content to diff). Add `{before_file}` as the 5th argument to `notify.py` in your `.supertool.json`.

## Disable temporarily

Just close Cursor. The notifier connects → no listener → silent exit. Supertool keeps working.

To uninstall: remove the symlink and the `notifiers.cursor-witness` block from `.supertool.json`.
